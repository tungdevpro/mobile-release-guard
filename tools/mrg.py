#!/usr/bin/env python3
"""mrg - mobile release guard.

Pre-release checks for the "user updates from the store and the app breaks"
class of bugs:

  * builds that do not match git (dependency drift, wrong resolved file,
    project opened via .xcodeproj instead of .xcworkspace, dirty tree)
  * dependency changes in storage / auth / crypto libraries
  * code changes that make data written by the previous store version
    unreadable (storage keys, keychain attributes, DB schema, app identity)

Exit code 0 = no unacknowledged BLOCKER, 1 = blocked, 2 = usage/runtime error.
Python 3.8+, stdlib only, so it can be vendored into any app repo.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

VERSION = "0.1.0"

BLOCKER, WARN, INFO = "BLOCKER", "WARN", "INFO"
SEV_ORDER = {BLOCKER: 0, WARN: 1, INFO: 2}

# Findings that describe a broken build rather than a risky-but-intended
# change. They must be fixed; an acknowledgement cannot waive them.
NON_ACKABLE = {"R001", "R002", "R005", "R007", "R010"}

STATE_DIR = ".release-guard"
LOCAL_DIR = ".release-guard/local"  # gitignored, per machine

DEFAULT_CONFIG = {
    # Tag pattern used to find the previous release when --base is not given.
    # Use per-platform patterns (e.g. "ios/*") if platforms ship separately.
    "tag_pattern": "*",
    "base_ref": None,
    "release_configurations": ["Release"],
    # Regexes matched against lower-cased package names. A change to one of
    # these packages can change how persisted data is read.
    "critical_package_patterns": [
        r"keychain", r"secure", r"crypt", r"keystore", r"(?<![a-z0-9])tink",
        r"realm", r"sqlite", r"sqflite", r"sqlcipher", r"(?<![a-z0-9])grdb",
        r"(?<![a-z0-9])fmdb", r"(?<![a-z0-9])drift", r"(?<![a-z0-9])room(?![a-z])",
        r"(?<![a-z0-9])hive", r"(?<![a-z0-9])isar", r"objectbox", r"mmkv",
        r"storage", r"preference", r"datastore", r"defaults", r"persist",
        r"(?<![a-z0-9])auth", r"oauth", r"appauth", r"(?<![a-z0-9])jwt",
        r"session", r"credential", r"(?<![a-z0-9])msal", r"login",
        r"sign[-_]?in", r"couchbase", r"watermelon", r"core[-_]?data",
        r"(?<![a-z0-9])valet", r"locksmith",
    ],
    "critical_package_patterns_extra": [],
    # Exact names or fnmatch globs that should never count as critical.
    "critical_package_ignore": [],
    "ignore_paths": [
        "*/Pods/*", "*/node_modules/*", "*/build/*", "*/.build/*",
        "*/.dart_tool/*", "*/DerivedData/*", "*/.release-guard/*",
        "*/.gradle/*", "*.g.dart", "*.freezed.dart", "*.generated.*",
        "*Tests/*", "*/test/*", "*/tests/*", "*/__tests__/*", "*.test.*",
        "*.spec.*", "*/androidTest/*", "*/src/test/*", "*/integration_test/*",
    ],
    "ignore_paths_extra": [],
    # Source files whose name suggests persistence logic. Any change to them
    # is surfaced as WARN for manual / AI review.
    "storage_path_patterns": [
        r"keychain", r"secur", r"storage", r"store(?!kit)", r"preference",
        r"defaults", r"session", r"token", r"credential", r"auth", r"persist",
        r"database", r"(?<![a-z])db(?![a-z])", r"migrat", r"realm", r"coredata",
        r"cache", r"vault", r"crypt",
    ],
    # Platforms that need a recorded upgrade test before `gate` passes.
    # None = every detected app platform (ios / android).
    "require_upgrade_test": None,
    # Where `mrg archive` stores release artifacts + manifests (shared drive).
    "artifact_archive": None,
    # DerivedData folders older than this are reported as WARN, not BLOCKER.
    "derived_data_max_age_days": 14,
}


class MrgError(Exception):
    pass


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Finding:
    rule: str
    severity: str
    title: str
    file: str = ""
    line: int = 0
    evidence: str = ""
    detail: str = ""
    hint: str = ""
    fingerprint: str = ""
    acked_by: str = ""
    ack_reason: str = ""

    def finalize(self) -> "Finding":
        if not self.fingerprint:
            raw = f"{self.rule}|{self.file}|{normalize_ws(self.evidence)}"
            self.fingerprint = hashlib.sha1(raw.encode()).hexdigest()[:12]
        return self

    @property
    def blocking(self) -> bool:
        return self.severity == BLOCKER and not self.acked_by


def normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------
# Git / filesystem context
# --------------------------------------------------------------------------

def run(cmd, cwd, check=True) -> str:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise MrgError(f"`{' '.join(cmd)}` failed: {p.stderr.strip()}")
    return p.stdout


class Context:
    def __init__(self, repo: str, base: str | None = None, head: str = "HEAD"):
        try:
            top = run(["git", "rev-parse", "--show-toplevel"], cwd=repo).strip()
        except MrgError:
            raise MrgError(f"{repo} is not inside a git repository")
        self.root = top
        self.cfg = load_config(top)
        self.head = head
        self.head_sha = self.git("rev-parse", head).strip()
        self.tracked = [f for f in self.git("ls-files", "-z").split("\0") if f]
        self.tracked_set = set(self.tracked)
        self.base = resolve_base(self, base)
        self.base_sha = self.git("rev-parse", self.base).strip() if self.base else None
        self.findings: list[Finding] = []
        self._show_cache: dict = {}
        crit = self.cfg["critical_package_patterns"] + self.cfg["critical_package_patterns_extra"]
        self._crit = [re.compile(p) for p in crit]
        self._ignore = self.cfg["ignore_paths"] + self.cfg["ignore_paths_extra"]
        self.platforms = detect_platforms(self)

    def git(self, *args, check=True) -> str:
        return run(["git", "-c", "core.quotePath=false", *args], cwd=self.root, check=check)

    def show(self, ref: str | None, path: str) -> str | None:
        """File content at a ref, or the working tree when ref is None."""
        key = (ref, path)
        if key not in self._show_cache:
            if ref is None:
                p = Path(self.root, path)
                self._show_cache[key] = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
            else:
                p = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=self.root,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace")
                self._show_cache[key] = p.stdout if p.returncode == 0 else None
        return self._show_cache[key]

    def files_at(self, ref: str) -> list[str]:
        out = self.git("ls-tree", "-r", "-z", "--name-only", ref)
        return [f for f in out.split("\0") if f]

    def ignored(self, path: str) -> bool:
        p = "/" + path
        return any(fnmatch.fnmatch(p, pat) for pat in self._ignore)

    def is_critical(self, name: str) -> bool:
        n = name.lower()
        for ig in self.cfg["critical_package_ignore"]:
            if n == ig.lower() or fnmatch.fnmatch(n, ig.lower()):
                return False
        return any(r.search(n) for r in self._crit)

    def add(self, f: Finding):
        f.finalize()
        for g in self.findings:
            if g.fingerprint == f.fingerprint:
                loc = f"{f.file}:{f.line}" if f.line else f.file
                g.detail = (g.detail + "\n" if g.detail else "") + f"also at {loc}"
                return
        self.findings.append(f)


def load_config(root: str) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    path = Path(root, ".release-guard.json")
    if path.is_file():
        try:
            user = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise MrgError(f".release-guard.json is not valid JSON: {e}")
        unknown = set(user) - set(cfg) - {"$schema", "_comment"}
        if unknown:
            raise MrgError(f".release-guard.json has unknown keys: {sorted(unknown)}")
        cfg.update(user)
    return cfg


def resolve_base(ctx: Context, explicit: str | None) -> str | None:
    ref = explicit or ctx.cfg.get("base_ref")
    if ref:
        if ctx.git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).strip() == "":
            raise MrgError(f"base ref '{ref}' not found (did you `git fetch --tags`?)")
        return ref
    tags = ctx.git("tag", "--merged", ctx.head, "--sort=-creatordate",
                   "--list", ctx.cfg["tag_pattern"]).split()
    for t in tags:
        if ctx.git("rev-parse", f"{t}^{{commit}}").strip() != ctx.head_sha:
            return t
    return None


def detect_platforms(ctx: Context) -> list[str]:
    found = set()
    for f in ctx.tracked:
        if ctx.ignored(f):
            continue
        if f.endswith(".xcodeproj/project.pbxproj"):
            found.add("ios")
        elif re.search(r"(^|/)build\.gradle(\.kts)?$", f):
            if re.search(r"android[.\-_]application", ctx.show(None, f) or ""):
                found.add("android")
        elif f.endswith("pubspec.yaml") and "sdk: flutter" in (ctx.show(None, f) or ""):
            found.add("flutter")
        elif f.endswith("package.json") and '"react-native"' in (ctx.show(None, f) or ""):
            found.add("react-native")
    return sorted(found)


def app_platforms(ctx: Context) -> list[str]:
    return [p for p in ("ios", "android") if p in ctx.platforms]


# --------------------------------------------------------------------------
# Lockfile / manifest parsers
# --------------------------------------------------------------------------

def url_identity(url: str) -> str:
    last = url.rstrip("/").split("/")[-1]
    return re.sub(r"\.git$", "", last).lower()


def parse_package_resolved(text: str) -> dict:
    data = json.loads(text)
    pins = data.get("pins") or data.get("object", {}).get("pins", [])
    out = {}
    for p in pins:
        ident = p.get("identity") or url_identity(p.get("repositoryURL") or p.get("location") or p.get("package", ""))
        st = p.get("state", {})
        out[ident.lower()] = {"version": st.get("version"), "revision": st.get("revision"),
                              "branch": st.get("branch")}
    return out


def parse_workspace_state(text: str) -> dict:
    data = json.loads(text)
    out = {}
    for d in data.get("object", {}).get("dependencies", []):
        ident = d.get("packageRef", {}).get("identity", "").lower()
        state = d.get("state", {})
        cs = state.get("checkoutState") or {}
        if state.get("name") in ("fileSystem", "local", "edited") or not ident:
            continue
        out[ident] = {"version": cs.get("version") or state.get("version"),
                      "revision": cs.get("revision"), "branch": cs.get("branch")}
    return out


def pin_label(pin: dict) -> str:
    if pin.get("version"):
        return pin["version"]
    if pin.get("branch"):
        return f"{pin['branch']}@{(pin.get('revision') or '')[:10]}"
    return (pin.get("revision") or "?")[:10]


def parse_podfile_lock(text: str) -> dict:
    out, in_pods = {}, False
    for line in text.splitlines():
        if re.match(r"^\S", line):
            in_pods = line.startswith("PODS:")
            continue
        if in_pods:
            m = re.match(r'^  - "?([^\s"(]+) \(([^)]+)\)', line)
            if m:
                out.setdefault(m.group(1).split("/")[0], m.group(2))
    return out


def parse_pubspec_lock(text: str) -> dict:
    out, cur, in_pkgs = {}, None, False
    for line in text.splitlines():
        if re.match(r"^\S", line):
            in_pkgs = line.startswith("packages:")
            continue
        if not in_pkgs:
            continue
        m = re.match(r"^  ([A-Za-z0-9_]+):\s*$", line)
        if m:
            cur = m.group(1)
            continue
        m = re.match(r'^    version:\s*"?([^"\s]+)"?', line)
        if m and cur:
            out[cur] = m.group(1)
    return out


def parse_package_lock(text: str) -> dict:
    data = json.loads(text)
    out = {}
    if "packages" in data:
        for k, v in data["packages"].items():
            if k.startswith("node_modules/") and "/node_modules/" not in k[len("node_modules/"):]:
                out[k[len("node_modules/"):]] = v.get("version", "?")
    else:
        for k, v in data.get("dependencies", {}).items():
            out[k] = v.get("version", "?")
    return out


def parse_yarn_lock(text: str) -> dict:
    out, names = {}, []
    for line in text.splitlines():
        if line and not line.startswith((" ", "#")) and line.rstrip().endswith(":"):
            names = []
            for spec in line.rstrip()[:-1].split(","):
                spec = spec.strip().strip('"')
                at = spec.rfind("@")
                if at > 0:
                    names.append(spec[:at])
            continue
        m = re.match(r'^  version:?\s+"?([^"\s]+)"?', line)
        if m:
            for n in names:
                prev = out.get(n)
                out[n] = m.group(1) if not prev or prev == m.group(1) else f"{prev}|{m.group(1)}"
            names = []
    return out


def parse_gradle_lockfile(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([^#\s:]+):([^:\s]+):([^=\s]+)=", line)
        if m:
            out[f"{m.group(1)}:{m.group(2)}"] = m.group(3)
    return out


GRADLE_COORD = re.compile(r"""["']([A-Za-z0-9_.\-]+):([A-Za-z0-9_.\-]+):([^"'\s]+)["']""")


def parse_versions_toml(text: str) -> list[tuple]:
    """[(module, version, line_no)] from a Gradle version catalog."""
    section, versions, libs = None, {}, []
    for i, line in enumerate(text.splitlines(), 1):
        s = line.split("#")[0].strip()
        m = re.match(r"^\[([\w\-]+)\]$", s)
        if m:
            section = m.group(1)
            continue
        if "=" not in s:
            continue
        key, val = [x.strip() for x in s.split("=", 1)]
        if section == "versions":
            mm = re.match(r'^"([^"]+)"$', val) or re.search(r'(?:strictly|require|prefer)\s*=\s*"([^"]+)"', val)
            if mm:
                versions[key] = mm.group(1)
        elif section == "libraries":
            mm = re.match(r'^"([^":]+:[^":]+):([^"]+)"$', val)
            if mm:
                libs.append((mm.group(1), mm.group(2), i))
                continue
            mod = re.search(r'module\s*=\s*"([^"]+)"', val)
            grp = re.search(r'group\s*=\s*"([^"]+)"', val)
            nam = re.search(r'name\s*=\s*"([^"]+)"', val)
            module = mod.group(1) if mod else (f"{grp.group(1)}:{nam.group(1)}" if grp and nam else None)
            if not module:
                continue
            vref = re.search(r'version\.ref\s*=\s*"([^"]+)"', val)
            vlit = re.search(r'version\s*=\s*"([^"]+)"', val)
            ver = versions.get(vref.group(1), f"<missing {vref.group(1)}>") if vref else (vlit.group(1) if vlit else "")
            libs.append((module, ver, i))
    return libs


def gradle_declared(ctx: Context, ref: str | None, paths: list[str]) -> list[tuple]:
    """[(module, version, path, line)] declared in build scripts and catalogs."""
    out = []
    for path in paths:
        text = ctx.show(ref, path)
        if text is None:
            continue
        if path.endswith(".toml"):
            out += [(m, v, path, ln) for m, v, ln in parse_versions_toml(text)]
        else:
            for i, line in enumerate(text.splitlines(), 1):
                if line.strip().startswith("//"):
                    continue
                for m in GRADLE_COORD.finditer(line):
                    out.append((f"{m.group(1)}:{m.group(2)}", m.group(3), path, i))
    return out


def is_gradle_dynamic(v: str) -> bool:
    return "+" in v or v.startswith("latest.") or v[:1] in "[(" or v == ""


def pubspec_deps(text: str) -> list[tuple]:
    """[(name, constraint, line_no)] from dependencies / dependency_overrides."""
    res, section = [], None
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\S", line):
            section = line.split(":")[0].strip()
            continue
        if section not in ("dependencies", "dependency_overrides"):
            continue
        m = re.match(r"^  ([A-Za-z0-9_]+):\s*(.*?)\s*(#.*)?$", line)
        if not m:
            continue
        name, val = m.group(1), m.group(2).strip("'\"")
        if val == "":
            sub, j = {}, i + 1
            while j < len(lines) and (lines[j].startswith("    ") or not lines[j].strip()):
                mm = re.match(r"^\s+(version|path|sdk|git|ref|url):\s*(.*)$", lines[j])
                if mm:
                    sub[mm.group(1)] = mm.group(2).strip("'\" ")
                j += 1
            if "sdk" in sub or "path" in sub:
                continue
            if "git" in sub or "url" in sub:
                val = "git#" + sub.get("ref", "")
            else:
                val = sub.get("version", "any")
        res.append((name, val, i + 1))
    return res


EXACT_SEMVER = re.compile(r"^=?\s*v?\d+\.\d+\.\d+([+\-][\w.\-]+)?$")
GIT_PINNED_REF = re.compile(r"^(v?\d+[\w.\-]*|[0-9a-f]{7,40})$")


def major(v: str) -> int | None:
    m = re.match(r"^v?(\d+)", v or "")
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------
# iOS / SPM helpers
# --------------------------------------------------------------------------

@dataclass
class SpmProject:
    project: str           # path/to/App.xcodeproj
    workspace: str | None  # path/to/App.xcworkspace that references it
    canonical: str         # Package.resolved Xcode should use
    embedded: str          # project.xcworkspace's Package.resolved


def spm_projects(ctx: Context) -> list[SpmProject]:
    workspaces = sorted({f[: f.index(".xcworkspace") + len(".xcworkspace")]
                         for f in ctx.tracked
                         if f.endswith(".xcworkspace/contents.xcworkspacedata")
                         and "project.xcworkspace" not in f and not ctx.ignored(f)})
    out = []
    for f in ctx.tracked:
        if not f.endswith(".xcodeproj/project.pbxproj") or ctx.ignored(f):
            continue
        if "XCRemoteSwiftPackageReference" not in (ctx.show(None, f) or ""):
            continue
        proj = f[: -len("/project.pbxproj")]
        ws = None
        for w in workspaces:
            content = ctx.show(None, f"{w}/contents.xcworkspacedata") or ""
            if os.path.basename(proj) in content:
                ws = w
                break
        embedded = f"{proj}/project.xcworkspace/xcshareddata/swiftpm/Package.resolved"
        canonical = f"{ws}/xcshareddata/swiftpm/Package.resolved" if ws else embedded
        out.append(SpmProject(proj, ws, canonical, embedded))
    return out


def spm_requirements(pbx_text: str) -> list[tuple]:
    """[(name, url, kind, body)] for each remote package reference."""
    out = []
    pat = re.compile(r'/\* XCRemoteSwiftPackageReference "([^"]+)" \*/ = \{(.*?)\n\s*\};\n', re.S)
    for m in pat.finditer(pbx_text):
        body = m.group(2)
        url = re.search(r'repositoryURL = "?([^";]+)"?;', body)
        kind = re.search(r"kind = (\w+);", body)
        out.append((m.group(1), url.group(1) if url else "", kind.group(1) if kind else "?", body))
    return out


def compare_pins(built: dict, committed: dict, a: str = "built", b: str = "git has") -> list[str]:
    diffs = []
    for ident, pin in sorted(built.items()):
        c = committed.get(ident)
        if c is None:
            diffs.append(f"{ident}: {a} {pin_label(pin)}, missing on the other side")
        elif (pin.get("revision") and c.get("revision") and pin["revision"] != c["revision"]) \
                or (pin.get("version") or None) != (c.get("version") or None):
            diffs.append(f"{ident}: {a} {pin_label(pin)}, {b} {pin_label(c)}")
    for ident in sorted(set(committed) - set(built)):
        diffs.append(f"{ident}: {b} {pin_label(committed[ident])}, missing on the other side")
    return diffs


def derived_data_root() -> Path:
    return Path(os.environ.get("MRG_DERIVED_DATA")
                or Path.home() / "Library/Developer/Xcode/DerivedData")


# --------------------------------------------------------------------------
# Checks: build reproducibility (R*)
# --------------------------------------------------------------------------

LOCKFILE_NAMES = ("Package.resolved", "Podfile.lock", "pubspec.lock", "package-lock.json",
                  "yarn.lock", "pnpm-lock.yaml", ".lockfile", "libs.versions.toml")


def check_clean_tree(ctx: Context):
    out = ctx.git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = [e for e in out.split("\0") if e]
    dirty, untracked = [], []
    i = 0
    while i < len(entries):
        e = entries[i]
        code, path = e[:2], e[3:]
        if code[0] in "RC":
            i += 1  # skip the rename source
        i += 1
        if path.startswith(LOCAL_DIR) or path.startswith(f"{STATE_DIR}/last-check.json"):
            continue
        if code == "??":
            (dirty if path.endswith(LOCKFILE_NAMES) else untracked).append(path)
        else:
            dirty.append(path)
    if dirty:
        artifacts = [d for d in dirty if re.search(r"(^|/)(build|out|product|release)/|\.(aab|apk|ipa|dex|bin|class)$", d)]
        hint = "Commit or stash, then rebuild. A release must be reproducible from a commit."
        if artifacts:
            hint = (f"{len(artifacts)} of these are build outputs committed to git, so every build dirties the tree. "
                    "Untrack them once: `git rm -r --cached <dir>` + add to .gitignore. " + hint)
        ctx.add(Finding("R001", BLOCKER, "Working tree has uncommitted changes - the release would not match git",
                        evidence="\n".join(sorted(dirty)[:30]) + (f"\n... and {len(dirty) - 30} more" if len(dirty) > 30 else ""),
                        hint=hint))
    if untracked:
        ctx.add(Finding("R001u", WARN, f"{len(untracked)} untracked file(s) present",
                        evidence="\n".join(sorted(untracked)[:15]),
                        hint="Untracked files can leak into the build. Commit, ignore or delete them."))


def check_lockfiles(ctx: Context):
    tracked = ctx.tracked_set
    # SwiftPM
    for sp in spm_projects(ctx):
        if sp.canonical not in tracked:
            ctx.add(Finding("R002", BLOCKER, "Package.resolved is not committed - SPM versions float on every machine",
                            file=sp.canonical, evidence=sp.canonical,
                            hint="Resolve packages in Xcode and commit the file (remove it from .gitignore)."))
        if sp.workspace and sp.embedded in tracked:
            ctx.add(Finding("R006", WARN, "Both the .xcodeproj and the .xcworkspace Package.resolved are committed",
                            file=sp.embedded, evidence=sp.embedded,
                            hint=f"Keep only {sp.canonical}; delete and gitignore the .xcodeproj copy so opening "
                                 "the project directly cannot silently use different versions."))
        if sp.workspace:
            emb = ctx.show(None, sp.embedded)
            can = ctx.show(None, sp.canonical)
            if emb and can:
                try:
                    diffs = compare_pins(parse_package_resolved(emb), parse_package_resolved(can),
                                          ".xcodeproj has", ".xcworkspace has")
                except (json.JSONDecodeError, KeyError):
                    diffs = ["unparseable Package.resolved"]
                if diffs:
                    ctx.add(Finding(
                        "R005", BLOCKER,
                        "Two Package.resolved files disagree - opening the .xcodeproj instead of the "
                        ".xcworkspace builds different dependency versions",
                        file=sp.embedded, evidence="\n".join(diffs),
                        hint=f"Always open {sp.workspace}. Delete {sp.embedded} (and its DerivedData), "
                             "pin critical packages to exactVersion, and add the Xcode build guard."))
    # CocoaPods
    for f in ctx.tracked:
        if os.path.basename(f) != "Podfile" or ctx.ignored(f):
            continue
        d = os.path.dirname(f)
        lock = os.path.join(d, "Podfile.lock") if d else "Podfile.lock"
        if lock not in tracked:
            ctx.add(Finding("R002", BLOCKER, "Podfile.lock is not committed", file=lock, evidence=lock,
                            hint="Run `pod install` and commit Podfile.lock."))
            continue
        manifest = Path(ctx.root, d, "Pods", "Manifest.lock")
        if manifest.is_file() and manifest.read_text(errors="replace") != (ctx.show(None, lock) or ""):
            ctx.add(Finding("R007", BLOCKER, "Installed Pods do not match Podfile.lock",
                            file=lock, evidence=f"{d}/Pods/Manifest.lock != {lock}",
                            hint="Run `pod install` (not `pod update`) before building the release."))
    # Flutter
    for f in ctx.tracked:
        if os.path.basename(f) != "pubspec.yaml" or ctx.ignored(f):
            continue
        d = os.path.dirname(f)
        is_app = any(t.startswith(os.path.join(d, x) + "/") if d else t.startswith(x + "/")
                     for t in ctx.tracked for x in ("ios", "android"))
        lock = os.path.join(d, "pubspec.lock") if d else "pubspec.lock"
        if is_app and lock not in tracked:
            ctx.add(Finding("R002", BLOCKER, "pubspec.lock is not committed for a Flutter app", file=lock,
                            evidence=lock, hint="Commit pubspec.lock and build with `flutter pub get --enforce-lockfile`."))
    # npm / yarn (React Native)
    for f in ctx.tracked:
        if os.path.basename(f) != "package.json" or ctx.ignored(f):
            continue
        if '"react-native"' not in (ctx.show(None, f) or ""):
            continue
        d = os.path.dirname(f)
        locks = [os.path.join(d, n) if d else n for n in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml")]
        if not any(l in tracked for l in locks):
            ctx.add(Finding("R002", BLOCKER, "No JS lockfile committed for a React Native app", file=f, evidence=f,
                            hint="Commit package-lock.json / yarn.lock and install with `npm ci` / `yarn --frozen-lockfile`."))
        check_node_modules_drift(ctx, d, tracked)
    # Gradle locking
    if "android" in ctx.platforms:
        gradle_files = [f for f in ctx.tracked if re.search(r"\.gradle(\.kts)?$", f) and not ctx.ignored(f)]
        has_locking = any("dependencyLocking" in (ctx.show(None, f) or "") for f in gradle_files)
        if not has_locking and not any(f.endswith(".lockfile") for f in ctx.tracked):
            ctx.add(Finding("R008", WARN, "Gradle dependency locking is not enabled",
                            evidence="no dependencyLocking / *.lockfile",
                            hint="Add `dependencyLocking { lockAllConfigurations() }` and commit gradle.lockfile "
                                 "(see build-reproducibility skill)."))


def check_node_modules_drift(ctx: Context, d: str, tracked: set):
    lock_path = os.path.join(d, "package-lock.json") if d else "package-lock.json"
    yarn_path = os.path.join(d, "yarn.lock") if d else "yarn.lock"
    locked = {}
    if lock_path in tracked:
        try:
            locked = parse_package_lock(ctx.show(None, lock_path) or "{}")
        except json.JSONDecodeError:
            return
    elif yarn_path in tracked:
        locked = parse_yarn_lock(ctx.show(None, yarn_path) or "")
    for name, ver in locked.items():
        if not ctx.is_critical(name) or "|" in ver:
            continue
        pj = Path(ctx.root, d, "node_modules", name, "package.json")
        if not pj.is_file():
            continue
        try:
            installed = json.loads(pj.read_text()).get("version")
        except json.JSONDecodeError:
            continue
        if installed and installed != ver:
            ctx.add(Finding("R007", BLOCKER, f"Installed {name} differs from the lockfile",
                            file=str(Path(d, "node_modules", name)), evidence=f"{name}: installed {installed}, lockfile {ver}",
                            hint="Run `npm ci` / `yarn install --frozen-lockfile` before building."))


def check_critical_pinning(ctx: Context):
    hint_pin = "Pin storage/auth/crypto libraries to an exact version so they only change on purpose."
    # SwiftPM in Xcode projects
    for sp in spm_projects(ctx):
        pbx = f"{sp.project}/project.pbxproj"
        for name, url, kind, _ in spm_requirements(ctx.show(None, pbx) or ""):
            ident = url_identity(url) if url else name.lower()
            if kind not in ("exactVersion", "revision") and (ctx.is_critical(ident) or ctx.is_critical(name)):
                ctx.add(Finding("R004", BLOCKER, f"Critical package {name} is not pinned ({kind})",
                                file=pbx, evidence=f"{name} {url} kind={kind}",
                                hint=hint_pin + " In Xcode: Package Dependencies > Dependency Rule > Exact Version."))
    # Package.swift
    for f in ctx.tracked:
        if os.path.basename(f) != "Package.swift" or ctx.ignored(f):
            continue
        for i, line in enumerate((ctx.show(None, f) or "").splitlines(), 1):
            m = re.search(r'\.package\(\s*(?:name:\s*"[^"]+",\s*)?url:\s*"([^"]+)"\s*,\s*(\.?\w+)', line)
            if m and not m.group(2).lstrip(".").startswith(("exact", "revision")) and ctx.is_critical(url_identity(m.group(1))):
                ctx.add(Finding("R004", BLOCKER, f"Critical package {url_identity(m.group(1))} is not pinned",
                                file=f, line=i, evidence=line.strip(), hint=hint_pin + " Use `exact:`."))
    # Podfile
    for f in ctx.tracked:
        if os.path.basename(f) != "Podfile" or ctx.ignored(f):
            continue
        for i, line in enumerate((ctx.show(None, f) or "").splitlines(), 1):
            m = re.match(r"""^\s*pod\s+['"]([^'"]+)['"]\s*(?:,\s*(.*))?$""", line)
            if not m or not ctx.is_critical(m.group(1).split("/")[0]):
                continue
            rest = (m.group(2) or "").split("#")[0]
            if ":path" in rest or "path:" in rest:
                continue
            if ":git" in rest or "git:" in rest:
                pinned = re.search(r"(:tag|tag:|:commit|commit:)", rest) is not None
            else:
                v = re.match(r"""\s*['"]([^'"]+)['"]""", rest)
                pinned = bool(v and EXACT_SEMVER.match(v.group(1).strip()))
            if not pinned:
                ctx.add(Finding("R004", BLOCKER, f"Critical pod {m.group(1)} is not pinned", file=f, line=i,
                                evidence=line.strip(), hint=hint_pin + " e.g. pod 'X', '1.2.3'."))
    # pubspec.yaml
    for f in ctx.tracked:
        if os.path.basename(f) != "pubspec.yaml" or ctx.ignored(f):
            continue
        for name, val, ln in pubspec_deps(ctx.show(None, f) or ""):
            if not ctx.is_critical(name):
                continue
            pinned = EXACT_SEMVER.match(val) or (val.startswith("git#") and GIT_PINNED_REF.match(val[4:]))
            if not pinned:
                ctx.add(Finding("R004", BLOCKER, f"Critical Dart package {name} is not pinned ({val})", file=f, line=ln,
                                evidence=f"{name}: {val}", hint=hint_pin + " e.g. `flutter_secure_storage: 9.2.2`."))
    # package.json
    for f in ctx.tracked:
        if os.path.basename(f) != "package.json" or ctx.ignored(f):
            continue
        try:
            pj = json.loads(ctx.show(None, f) or "{}")
        except json.JSONDecodeError:
            continue
        if "react-native" not in pj.get("dependencies", {}):
            continue
        for name, val in pj.get("dependencies", {}).items():
            if not ctx.is_critical(name) or val.startswith(("file:", "link:", "workspace:", "portal:")):
                continue
            if "#" in val:
                pinned = bool(GIT_PINNED_REF.match(val.split("#", 1)[1]))
            else:
                pinned = bool(EXACT_SEMVER.match(val))
            if not pinned:
                ctx.add(Finding("R004", BLOCKER, f"Critical npm package {name} is not pinned ({val})", file=f,
                                evidence=f"{name}: {val}", hint=hint_pin + " Remove ^ / ~."))
    # Gradle
    gradle_paths = [f for f in ctx.tracked if (re.search(r"\.gradle(\.kts)?$", f) or f.endswith(".versions.toml"))
                    and not ctx.ignored(f)]
    for module, ver, path, ln in gradle_declared(ctx, None, gradle_paths):
        if "$" in ver or not is_gradle_dynamic(ver):
            continue
        crit = ctx.is_critical(module)
        ctx.add(Finding("R004", BLOCKER if crit else WARN,
                        f"{'Critical ' if crit else ''}Gradle dependency {module} uses a dynamic version ({ver or 'none'})",
                        file=path, line=ln, evidence=f"{module}:{ver}",
                        hint="Use a fixed version; dynamic versions resolve differently over time."))


def check_derived_data(ctx: Context):
    """Resolved-version drift detector: compare what Xcode actually resolved on
    this machine with what git says."""
    root = derived_data_root()
    projects = spm_projects(ctx)
    if not projects or not root.is_dir():
        return
    real_root = os.path.realpath(ctx.root)
    max_age = dt.timedelta(days=ctx.cfg["derived_data_max_age_days"])
    for d in sorted(root.iterdir()):
        info = d / "info.plist"
        state = d / "SourcePackages" / "workspace-state.json"
        if not info.is_file() or not state.is_file():
            continue
        try:
            with open(info, "rb") as fh:
                pl = plistlib.load(fh)
        except Exception:
            continue
        wp = pl.get("WorkspacePath")
        if not wp:
            continue
        rel = os.path.relpath(os.path.realpath(wp), real_root)
        if rel.startswith(".."):
            continue
        sp = next((p for p in projects if rel in (p.project, p.workspace)), None)
        if not sp:
            continue
        last = pl.get("LastAccessedDate")
        recent = True
        if isinstance(last, dt.datetime):
            recent = dt.datetime.utcnow() - last.replace(tzinfo=None) <= max_age
        committed_text = ctx.show(ctx.head, sp.canonical)
        if committed_text is None:
            continue
        try:
            diffs = compare_pins(parse_workspace_state(state.read_text()), parse_package_resolved(committed_text))
        except (json.JSONDecodeError, KeyError):
            continue
        opened_via_proj = rel.endswith(".xcodeproj") and sp.workspace
        if diffs:
            how = f" (opened via {rel} instead of {sp.workspace})" if opened_via_proj else f" (opened via {rel})"
            ctx.add(Finding(
                "R010" if recent else "R010s", BLOCKER if recent else WARN,
                "Xcode on this machine resolved different package versions than git" + how,
                file=str(state), evidence="\n".join(diffs),
                detail=f"DerivedData: {d.name}, last accessed {last}",
                hint=f"Close Xcode, delete {d}, open {sp.workspace or sp.project}, "
                     "File > Packages > Reset Package Caches, and verify again before archiving."))
        elif opened_via_proj and recent:
            ctx.add(Finding("R011", WARN, f"Project was opened via {rel} although {sp.workspace} exists",
                            file=rel, evidence=rel, hint=f"Open {sp.workspace} for release builds."))


# --------------------------------------------------------------------------
# Checks: dependency changes since the previous release (D*)
# --------------------------------------------------------------------------

LOCK_PARSERS = [
    (lambda p: p.endswith("Package.resolved"),
     lambda t: {k: pin_label(v) for k, v in parse_package_resolved(t).items()}),
    (lambda p: p.endswith("Podfile.lock"), parse_podfile_lock),
    (lambda p: p.endswith("pubspec.lock"), parse_pubspec_lock),
    (lambda p: p.endswith("package-lock.json"), parse_package_lock),
    (lambda p: p.endswith("yarn.lock"), parse_yarn_lock),
    (lambda p: p.endswith(".lockfile") and "gradle" in p.lower() or p.endswith("gradle.lockfile"), parse_gradle_lockfile),
]


def lock_snapshot(ctx: Context, ref: str) -> dict:
    """{path: {package: version}} for every lockfile at ref."""
    files = ctx.files_at(ref)
    snap = {}
    for f in files:
        if ctx.ignored(f) and not f.endswith("Package.resolved"):
            continue
        for match, parser in LOCK_PARSERS:
            if match(f):
                try:
                    snap[f] = parser(ctx.show(ref, f) or "")
                except (json.JSONDecodeError, KeyError, AttributeError):
                    snap[f] = {}
                break
    if not any(p.endswith(".lockfile") for p in snap):
        gpaths = [f for f in files if (re.search(r"\.gradle(\.kts)?$", f) or f.endswith(".versions.toml"))
                  and not ctx.ignored(f)]
        declared = {}
        for module, ver, _, _ in gradle_declared(ctx, ref, gpaths):
            if "$" not in ver:
                declared[module] = ver
        if declared:
            snap["gradle (declared versions)"] = declared
    return snap


def check_dependency_diff(ctx: Context):
    if not ctx.base:
        return
    old, new = lock_snapshot(ctx, ctx.base), lock_snapshot(ctx, ctx.head)
    for path in sorted(set(old) | set(new)):
        o, n = old.get(path, {}), new.get(path, {})
        minor_changes = []
        for name in sorted(set(o) | set(n)):
            ov, nv = o.get(name), n.get(name)
            if ov == nv:
                continue
            crit = ctx.is_critical(name)
            if ov and nv:
                ev = f"{name} {ov} -> {nv}"
                if crit:
                    ctx.add(Finding("D001", BLOCKER, f"Critical dependency changed: {ev}", file=path, evidence=ev,
                                    hint="Read the changelog between both versions for storage key / keychain / "
                                         "encryption / schema changes, and run the upgrade test."))
                elif major(ov) is not None and major(nv) is not None and major(nv) != major(ov):
                    ctx.add(Finding("D004", WARN, f"Major version change: {ev}", file=path, evidence=ev))
                else:
                    minor_changes.append(ev)
            elif ov and not nv:
                ev = f"{name} {ov} removed"
                if crit:
                    ctx.add(Finding("D002", BLOCKER, f"Critical dependency removed: {ev}", file=path, evidence=ev,
                                    hint="Data written through this library by the old version must still be "
                                         "read (or migrated) by the new one."))
                else:
                    minor_changes.append(ev)
            else:
                ev = f"{name} {nv} added"
                if crit:
                    ctx.add(Finding("D003", WARN, f"Critical dependency added: {ev}", file=path, evidence=ev,
                                    hint="If it replaces an existing storage mechanism, a migration is needed."))
                else:
                    minor_changes.append(ev)
        if minor_changes:
            ctx.add(Finding("D005", INFO, f"{len(minor_changes)} other dependency change(s)", file=path,
                            evidence="\n".join(minor_changes[:40])))


# --------------------------------------------------------------------------
# Checks: persistence-affecting code changes since the previous release (P*)
# --------------------------------------------------------------------------

SWIFT = (".swift", ".m", ".mm", ".h")
JVM = (".kt", ".java")
DART = (".dart",)
JS = (".js", ".jsx", ".ts", ".tsx")
SRC = SWIFT + JVM + DART + JS


@dataclass
class LineRule:
    id: str
    title: str
    exts: tuple
    pattern: str
    on_removed: str | None
    on_added: str | None
    file_contains: str | None = None
    exclude: str | None = None
    hint: str = ""


LINE_RULES = [
    # --- iOS -------------------------------------------------------------
    LineRule("P101", "Keychain query attributes changed", SWIFT,
             r"kSecAttr(Service|AccessGroup|Accessible\w*|Account|Synchronizable|Label)|kSecClass\w*|kSecUseDataProtectionKeychain",
             BLOCKER, WARN, hint="Old items are only found with the old service/account/access group/accessibility."),
    LineRule("P102", "Keychain wrapper configuration changed", SWIFT,
             r"Keychain\(\s*(service|accessGroup)|KeychainWrapper\(|KeychainSwift\(|keyPrefix|Valet\.|SecureDefaults|"
             r"\.accessibility\(|accessGroup\s*[:=]|serviceName\s*[:=]|service\s*:\s*\"",
             BLOCKER, WARN, hint="Wrapper libraries derive keychain item names from these values."),
    LineRule("P103", "UserDefaults key or suite changed", SWIFT,
             r"forKey:\s*\"|UserDefaults\(\s*suiteName|@AppStorage\(|\.(string|bool|integer|double|object|data|array|dictionary)\(\s*forKey",
             BLOCKER, INFO, hint="Values stored under the old key are no longer read."),
    LineRule("P104", "Core Data / SwiftData store configuration changed", SWIFT,
             r"NSPersistent(Cloud\w*)?Container\(|persistentStoreDescriptions|NSMigratePersistentStoresAutomaticallyOption|"
             r"(NSInferMappingModelAutomaticallyOption|shouldInferMappingModelAutomatically)|ModelContainer\(|"
             r"VersionedSchema|SchemaMigrationPlan|NSPersistentStoreFileProtectionKey|\.sqlite\"",
             BLOCKER, WARN),
    LineRule("P105", "Realm configuration changed", SWIFT + JVM + JS,
             r"schemaVersion|migrationBlock|Realm\.Configuration|RealmConfiguration|encryptionKey|fileURL\s*[:=]",
             BLOCKER, WARN),
    LineRule("P106", "Destructive migration enabled - existing user data will be deleted", SWIFT + JVM + JS + DART,
             r"deleteRealmIfMigrationNeeded\s*(=|:|\()\s*true|deleteRealmIfMigrationNeeded\(\)|fallbackToDestructiveMigration|"
             r"deleteIfMigrationNeeded|Hive\.deleteFromDisk|deleteBoxFromDisk|deleteDatabase\(",
             None, BLOCKER, hint="This silently wipes data (and often the session) for every upgrading user."),
    LineRule("P107", "Codable keys of a possibly persisted model changed", SWIFT,
             r"enum\s+CodingKeys|case\s+\w+\s*=\s*\"", WARN, None, file_contains=r"Codable|Decodable"),
    LineRule("P108", "Force-try / force-unwrap on decoding persisted data - old data can crash the app", SWIFT,
             r"try!\s*[\w.()]*(decode|unarchive|data\(contentsOf)|(decode|unarchive)\w*\([^)]*\)!",
             None, WARN),
    LineRule("P109", "App Group / container path changed", SWIFT,
             r"containerURL\(\s*forSecurityApplicationGroupIdentifier|(applicationSupport|document|caches|library)Directory",
             WARN, INFO),
    LineRule("P110", "Enum of storage keys changed", SWIFT + JVM + DART + JS,
             r"case\s+\w+\s*=\s*\"|^\s*\w+\s*\(\s*\"[^\"]+\"\s*\)\s*[,;]?$|^\s*\w+\s*=\s*['\"][^'\"]+['\"]\s*,?$",
             BLOCKER, None, file_contains=r"(?i)enum\s+\w*(key|default|storage|pref)"),
    # --- Android ---------------------------------------------------------
    LineRule("P201", "SharedPreferences / encrypted prefs configuration changed", JVM,
             r"getSharedPreferences\(|EncryptedSharedPreferences|MasterKeys?\b|KeyGenParameterSpec|PrefKeyEncryptionScheme|"
             r"PrefValueEncryptionScheme|PreferenceManager\.getDefault|setKeyScheme",
             BLOCKER, WARN),
    LineRule("P202", "SharedPreferences key changed", JVM,
             r"\.(get|put)(String|Boolean|Int|Long|Float|StringSet)\(\s*\"|\.(remove|contains)\(\s*\"",
             BLOCKER, INFO),
    LineRule("P203", "Android Keystore key configuration changed", JVM,
             r"KeyStore\.getInstance|AndroidKeyStore|setUserAuthenticationRequired|setInvalidatedByBiometricEnrollment|"
             r"keyAlias|KEY_ALIAS|setBlockModes|setEncryptionPaddings|setIsStrongBoxBacked",
             BLOCKER, WARN, hint="A different alias or spec means the old key (and data encrypted with it) is unusable."),
    LineRule("P204", "Room database definition changed", JVM,
             r"@Database\(|version\s*=\s*\d+|entities\s*=|@Entity|@ColumnInfo|@PrimaryKey|addMigrations|Migration\(\s*\d+|"
             r"AutoMigration|Room\.databaseBuilder\(",
             BLOCKER, WARN, file_contains=r"@Database|@Entity|Room\.databaseBuilder"),
    LineRule("P206", "DataStore name or key changed", JVM,
             r"preferencesDataStore\(|dataStore\(|(string|boolean|int|long|float|double|stringSet)PreferencesKey\(|"
             r"DataStoreFactory|SharedPreferencesMigration",
             BLOCKER, INFO),
    LineRule("P207", "Serialized field name changed", JVM,
             r"@SerializedName\(|@Json\(\s*name|@SerialName\(|@field:Json", WARN, None),
    # --- Flutter ---------------------------------------------------------
    LineRule("P301", "flutter_secure_storage options changed", DART,
             r"FlutterSecureStorage\(|IOSOptions\(|AndroidOptions\(|MacOsOptions\(|accessibility:|groupId:|accountName:|"
             r"encryptedSharedPreferences|sharedPreferencesName|preferencesKeyPrefix|KeychainAccessibility",
             BLOCKER, WARN, hint="Options define where items live; changing them hides existing items (logout)."),
    LineRule("P302", "resetOnError enabled - read errors will silently wipe secure storage", DART,
             r"resetOnError:\s*true", None, WARN),
    LineRule("P303", "Secure storage key changed", DART,
             r"\.(read|write|delete|containsKey)\(\s*key:\s*['\"]", BLOCKER, INFO),
    LineRule("P304", "shared_preferences key changed", DART,
             r"\.(get|set)(String|Bool|Int|Double|StringList)?\(\s*['\"]|\.(remove|containsKey)\(\s*['\"]|"
             r"SharedPreferences(Async|WithCache)?\b.*(prefix|allowList)",
             BLOCKER, INFO),
    LineRule("P305", "Hive type / box definition changed", DART,
             r"@HiveType\(|@HiveField\(|typeId:|Hive\.(openBox|openLazyBox|box)\(|registerAdapter|encryptionCipher",
             BLOCKER, WARN, hint="Changing typeId/field index or box name makes stored objects unreadable."),
    LineRule("P306", "Local database schema / version changed", DART,
             r"openDatabase\(|onUpgrade|onCreate|version:\s*\d+|schemaVersion|MigrationStrategy|@[cC]ollection|Isar\.open|"
             r"@DriftDatabase|CREATE TABLE|ALTER TABLE",
             BLOCKER, WARN, file_contains=r"openDatabase|sqflite|drift|isar|Database"),
    LineRule("P307", "JSON field name of a possibly persisted model changed", DART,
             r"@JsonKey\(\s*name|@JsonSerializable", WARN, None),
    # --- React Native ----------------------------------------------------
    LineRule("P401", "Async/secure storage key changed", JS,
             r"AsyncStorage\.(getItem|setItem|removeItem|multiGet|multiSet|mergeItem)\(|EncryptedStorage\.|"
             r"SecureStore\.(getItemAsync|setItemAsync|deleteItemAsync)\(|new MMKV\(|MMKV\(|"
             r"storage\.(getString|set|delete|getBoolean|getNumber|contains)\(\s*['\"`]",
             BLOCKER, INFO),
    LineRule("P402", "react-native-keychain options changed", JS,
             r"Keychain\.(set|get|reset)(Generic|Internet)\w*\(|service:\s*['\"`]|accessGroup|accessible:|ACCESSIBLE\.|"
             r"keychainService|requireAuthentication",
             BLOCKER, WARN),
    LineRule("P403", "Persisted state configuration changed", JS,
             r"persistReducer|persistStore|key:\s*['\"`]|version:\s*\d+|migrate:|createMigrate|createJSONStorage|"
             r"name:\s*['\"`]|partialize|whitelist|blacklist",
             BLOCKER, WARN, file_contains=r"redux-persist|zustand/middleware|persist\("),
    # --- Cross-platform generic -------------------------------------------
    LineRule("P001", "Storage key constant changed", SRC,
             r"(?i)\b\w*(key|pref|storage|default)s?\w*\s*(:\s*[\w?<>]+\s*)?=\s*[\"'][^\"'\n]+[\"']",
             BLOCKER, None,
             exclude=r"(?i)api_?key|secret|client_?id|dsn|google|maps|sentry|publishable|header|url|endpoint|(^|\W)test",
             hint="If this constant names a stored value, values written by the old version are orphaned."),
    # --- App identity / config --------------------------------------------
    LineRule("P501", "Bundle identifier / team / entitlements setting changed", (".pbxproj", ".xcconfig"),
             r"PRODUCT_BUNDLE_IDENTIFIER|DEVELOPMENT_TEAM|CODE_SIGN_ENTITLEMENTS|APP_GROUP|KEYCHAIN",
             BLOCKER, WARN, hint="Keychain access groups and app containers are tied to team + bundle id."),
    LineRule("P502", "Keychain / App Group entitlements changed", (".entitlements",),
             r"keychain-access-groups|application-groups|AppIdentifierPrefix|<string>",
             BLOCKER, WARN),
    LineRule("P503", "Android application identity / signing changed", (".gradle", ".gradle.kts"),
             r"applicationId|applicationIdSuffix|signingConfig|storeFile|keyAlias|sharedUserId",
             BLOCKER, WARN, hint="A different applicationId/signing key = different app: no data, no in-place update."),
    LineRule("P504", "Android backup / process configuration changed", ("AndroidManifest.xml",),
             r"allowBackup|fullBackupContent|dataExtractionRules|sharedUserId|android:process|hasFragileUserData",
             WARN, WARN, hint="Restored Keystore-encrypted data from another device crashes on decrypt."),
    LineRule("P505", "Info.plist identity / data protection changed", ("Info.plist",),
             r"CFBundleIdentifier|AppIdentifierPrefix|keychain|NSFileProtection|AppGroup",
             BLOCKER, WARN),
]


def parse_unified_diff(text: str) -> list[dict]:
    files, cur, in_hunk, hunk = [], None, False, 0
    old_ln = new_ln = 0
    for raw in text.split("\n"):
        if raw.startswith("diff --git "):
            cur = {"path": None, "old_path": None, "new_file": False, "deleted": False, "lines": []}
            files.append(cur)
            in_hunk = False
            continue
        if cur is None:
            continue
        if not in_hunk:
            if raw.startswith("new file mode"):
                cur["new_file"] = True
            elif raw.startswith("deleted file mode"):
                cur["deleted"] = True
            elif raw.startswith("--- "):
                cur["old_path"] = None if raw[4:] == "/dev/null" else raw[6:]
            elif raw.startswith("+++ "):
                cur["path"] = None if raw[4:] == "/dev/null" else raw[6:]
            elif raw.startswith("rename to "):
                cur["path"] = raw[len("rename to "):]
            elif raw.startswith("rename from "):
                cur["old_path"] = raw[len("rename from "):]
        if raw.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if m:
                old_ln, new_ln = int(m.group(1)), int(m.group(2))
                in_hunk = True
                hunk += 1
            continue
        if not in_hunk:
            continue
        if raw.startswith("+"):
            cur["lines"].append(("+", new_ln, raw[1:], hunk))
            new_ln += 1
        elif raw.startswith("-"):
            cur["lines"].append(("-", old_ln, raw[1:], hunk))
            old_ln += 1
        elif raw.startswith(" "):
            old_ln += 1
            new_ln += 1
    for f in files:
        f["path"] = f["path"] or f["old_path"]
    return [f for f in files if f["path"]]


def check_persistence_diff(ctx: Context):
    if not ctx.base:
        ctx.add(Finding("P000", WARN, "No previous release found - persistence and dependency diff skipped",
                        evidence=f"tag_pattern={ctx.cfg['tag_pattern']}",
                        hint="Pass --base <tag-of-version-on-store> or set base_ref / tag_pattern."))
        return
    diff = ctx.git("diff", "-U0", "--no-color", "--no-ext-diff", "-M", ctx.base, ctx.head)
    files = [f for f in parse_unified_diff(diff) if not ctx.ignored(f["path"])]

    added_all = {normalize_ws(l[2]) for f in files for l in f["lines"] if l[0] == "+"}
    removed_all = {normalize_ws(l[2]) for f in files for l in f["lines"] if l[0] == "-"}
    rules = [(r, re.compile(r.pattern), re.compile(r.exclude) if r.exclude else None,
              re.compile(r.file_contains) if r.file_contains else None) for r in LINE_RULES]
    storage_name = [re.compile(p) for p in ctx.cfg["storage_path_patterns"]]

    for f in files:
        path = f["path"]
        flagged = False
        flagged |= file_level_rules(ctx, f)
        content_new = ctx.show(ctx.head, path) or ""
        content_old = ctx.show(ctx.base, f["old_path"] or path) or ""
        for rule, rx, ex, fc in rules:
            if not path.endswith(rule.exts):
                continue
            if fc and not (fc.search(content_new) or fc.search(content_old)):
                continue
            groups: dict = {}
            for kind, ln, text, hunk in f["lines"]:
                if not rx.search(text) or (ex and ex.search(text)) or text.strip().startswith(("//", "#", "*", "/*")):
                    continue
                groups.setdefault((hunk, kind), []).append((ln, text))
            for (hunk, kind), hits in groups.items():
                moved = all(normalize_ws(t) in (added_all if kind == "-" else removed_all) for _, t in hits)
                sev = rule.on_removed if kind == "-" else rule.on_added
                # A pure move (same lines removed and added elsewhere) is not a change.
                if sev is None or (moved and rule.on_removed is not None):
                    continue
                if kind == "+":
                    # Only report additions when no removal in the same hunk already did.
                    if (hunk, "-") in groups and rule.on_removed is not None:
                        continue
                if path.endswith(".pbxproj"):
                    text = content_old if kind == "-" else content_new
                    configs = {pbx_config_name(text, ln) for ln, _ in hits}
                    if None not in configs and not configs & set(ctx.cfg["release_configurations"]) and sev == BLOCKER:
                        sev = WARN
                    rule_title = f"{rule.title} [config: {', '.join(sorted(c or '?' for c in configs))}]"
                else:
                    rule_title = rule.title
                counterpart = [t for k, _, t, h in f["lines"] if h == hunk and k != kind][:6]
                ev = "\n".join(f"{kind} {t.strip()}" for _, t in hits[:8])
                det = "\n".join(f"{'+' if kind == '-' else '-'} {t.strip()}" for t in counterpart)
                ctx.add(Finding(rule.id, sev, rule_title, file=path, line=hits[0][0], evidence=ev,
                                detail=("replaced by / next to:\n" + det) if det else "", hint=rule.hint))
                flagged = True
        if not flagged and path.endswith(SRC):
            name = os.path.basename(path).lower()
            if any(r.search(name) for r in storage_name):
                digest = hashlib.sha1("\n".join(l[0] + l[2] for l in f["lines"]).encode()).hexdigest()[:10]
                ctx.add(Finding("P900", WARN, "Persistence-related file changed - review for upgrade compatibility",
                                file=path, evidence=f"{path} diff:{digest}",
                                detail=f"{sum(1 for l in f['lines'] if l[0] == '+')} added / "
                                       f"{sum(1 for l in f['lines'] if l[0] == '-')} removed lines"))


def pbx_config_name(text: str, line_no: int) -> str | None:
    """Name of the XCBuildConfiguration block containing line_no (1-based)."""
    lines = text.splitlines()
    for i in range(min(line_no, len(lines)) - 1, -1, -1):
        m = re.match(r"^\s*\w+ /\* (.+?) \*/ = \{\s*$", lines[i])
        if m:
            body = "\n".join(lines[i:i + 3])
            return m.group(1) if "XCBuildConfiguration" in body else None
    return None


def file_level_rules(ctx: Context, f: dict) -> bool:
    path = f["path"]
    if re.search(r"\.xcdatamodeld/[^/]+\.xcdatamodel/contents$", path):
        if f["new_file"]:
            ctx.add(Finding("P602", WARN, "New Core Data model version added - verify migration from the store version",
                            file=path, evidence=path,
                            hint="Lightweight migration must be enabled, or a mapping model provided."))
        elif f["deleted"]:
            ctx.add(Finding("P601", BLOCKER, "Core Data model version deleted - stores created by it cannot be migrated",
                            file=path, evidence=path))
        else:
            ctx.add(Finding("P601", BLOCKER, "Existing Core Data model version was edited in place",
                            file=path, evidence=path + "\n" + "\n".join(l[0] + l[2].strip() for l in f["lines"][:10]),
                            hint="Never edit a shipped model version. Add a new version and set it current."))
        return True
    if path.endswith(".xccurrentversion"):
        ctx.add(Finding("P603", WARN, "Current Core Data model version changed", file=path, evidence=path))
        return True
    if re.search(r"(^|/)schemas/.+/\d+\.json$", path):
        if not f["new_file"]:
            ctx.add(Finding("P604", BLOCKER, "Exported Room schema of a shipped version was modified",
                            file=path, evidence=path, hint="Bump the database version and add a Migration."))
            return True
        return False
    if re.search(r"(backup_rules|data_extraction_rules|backup_descriptor)\w*\.xml$", path):
        ctx.add(Finding("P504", WARN, "Android backup rules changed", file=path, evidence=path))
        return True
    return False


# --------------------------------------------------------------------------
# Acks, reports, upgrade-test records
# --------------------------------------------------------------------------

def acks_path(ctx: Context) -> Path:
    return Path(ctx.root, STATE_DIR, "acks.json")


def load_acks(ctx: Context) -> list:
    p = acks_path(ctx)
    if not p.is_file():
        return []
    return json.loads(p.read_text()).get("acks", [])


def apply_acks(ctx: Context):
    acks = {a["fingerprint"]: a for a in load_acks(ctx)}
    for f in ctx.findings:
        a = acks.get(f.fingerprint)
        if a and f.rule not in NON_ACKABLE:
            f.acked_by, f.ack_reason = a.get("by", "?"), a.get("reason", "")


def run_checks(ctx: Context, scope: str):
    if scope == "all":
        check_clean_tree(ctx)
    check_lockfiles(ctx)
    check_critical_pinning(ctx)
    if "ios" in ctx.platforms:
        check_derived_data(ctx)
    if scope == "all":
        check_dependency_diff(ctx)
        check_persistence_diff(ctx)
    apply_acks(ctx)
    ctx.findings.sort(key=lambda f: (SEV_ORDER[f.severity], bool(f.acked_by), f.rule, f.file, f.line))


def render_text(ctx: Context, show_info: bool) -> str:
    out = [f"mobile-release-guard {VERSION}",
           f"repo:      {ctx.root}",
           f"platforms: {', '.join(ctx.platforms) or 'none detected'}",
           f"base:      {ctx.base or '-'} ({(ctx.base_sha or '')[:10]})  ->  head: {ctx.head} ({ctx.head_sha[:10]})",
           ""]
    for f in ctx.findings:
        if f.severity == INFO and not show_info:
            continue
        tag = f.severity + (" (acked)" if f.acked_by else "")
        loc = f"{f.file}:{f.line}" if f.line else f.file
        out.append(f"[{tag}] {f.rule} {f.title}")
        if loc:
            out.append(f"    at: {loc}")
        for ln in f.evidence.splitlines()[:12]:
            out.append(f"    | {ln}")
        for ln in f.detail.splitlines()[:8]:
            out.append(f"    . {ln}")
        if f.hint:
            out.append(f"    fix: {f.hint}")
        if f.acked_by:
            out.append(f"    ack: {f.acked_by}: {f.ack_reason}")
        out.append(f"    fp: {f.fingerprint}{'  (cannot be acknowledged)' if f.rule in NON_ACKABLE else ''}")
        out.append("")
    n = {s: sum(1 for f in ctx.findings if f.severity == s) for s in (BLOCKER, WARN, INFO)}
    acked = sum(1 for f in ctx.findings if f.severity == BLOCKER and f.acked_by)
    blocking = sum(1 for f in ctx.findings if f.blocking)
    out.append(f"summary: {n[BLOCKER]} BLOCKER ({acked} acknowledged), {n[WARN]} WARN, {n[INFO]} INFO")
    out.append("RESULT: " + ("FAIL - release blocked" if blocking else "PASS"))
    return "\n".join(out)


def save_last_check(ctx: Context):
    p = Path(ctx.root, LOCAL_DIR, "last-check.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"head": ctx.head_sha, "base": ctx.base, "base_sha": ctx.base_sha,
                             "platforms": ctx.platforms, "at": now_iso(),
                             "findings": [asdict(f) for f in ctx.findings]}, indent=2))


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def git_user(ctx: Context) -> str:
    return ctx.git("config", "user.email", check=False).strip() or os.environ.get("USER", "unknown")


def upgrade_tests_path(root: str) -> Path:
    return Path(root, LOCAL_DIR, "upgrade-tests.json")


def load_upgrade_tests(root: str) -> list:
    p = upgrade_tests_path(root)
    return json.loads(p.read_text()).get("tests", []) if p.is_file() else []


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_check(a) -> int:
    ctx = Context(a.repo, a.base)
    run_checks(ctx, a.scope)
    if not a.no_save:
        save_last_check(ctx)
    if a.json:
        print(json.dumps({"head": ctx.head_sha, "base": ctx.base, "platforms": ctx.platforms,
                          "blocked": any(f.blocking for f in ctx.findings),
                          "findings": [asdict(f) for f in ctx.findings]}, indent=2))
    else:
        print(render_text(ctx, a.verbose))
    return 1 if any(f.blocking for f in ctx.findings) else 0


def cmd_gate(a) -> int:
    ctx = Context(a.repo, a.base)
    run_checks(ctx, "all")
    save_last_check(ctx)
    print(render_text(ctx, False))
    blocked = any(f.blocking for f in ctx.findings)
    if not ctx.base:
        blocked = True
        print("\nno base: the version on the store is unknown, so dependency and persistence changes were not "
              "checked. Pass --base <tag or commit of the store build> (or tag it).")
    required = a.platform or ctx.cfg["require_upgrade_test"] or app_platforms(ctx)
    tests = load_upgrade_tests(ctx.root)
    print("\nupgrade tests (must pass on this exact commit):")
    missing = []
    for p in required:
        recs = [t for t in tests if t["platform"] == p and t["commit"] == ctx.head_sha]
        last = recs[-1] if recs else None
        if last and last["result"] == "pass":
            print(f"  {p}: PASS  {last['from_version']} -> {last['to_version']} via {last['method']} "
                  f"by {last['by']} at {last['at']}")
        else:
            missing.append(p)
            print(f"  {p}: {'FAIL' if last else 'MISSING'}"
                  + (f" ({last['notes']})" if last and last.get("notes") else ""))
    go = not blocked and not missing
    print("\n" + ("GO - release allowed" if go else "NO-GO - release blocked"))
    return 0 if go else 1


def cmd_ack(a) -> int:
    root = run(["git", "rev-parse", "--show-toplevel"], cwd=a.repo).strip()
    last = Path(root, LOCAL_DIR, "last-check.json")
    if not last.is_file():
        raise MrgError("run `mrg check` first")
    findings = {f["fingerprint"]: f for f in json.loads(last.read_text())["findings"]}
    f = findings.get(a.fingerprint)
    if not f:
        raise MrgError(f"fingerprint {a.fingerprint} not in the last check")
    if f["rule"] in NON_ACKABLE:
        raise MrgError(f"{f['rule']} describes a broken build and cannot be acknowledged - fix it")
    if len(a.reason.strip()) < 20:
        raise MrgError("--reason must explain why upgrading users are safe (>= 20 chars), "
                       "e.g. which migration handles it and how it was tested")
    by = a.by or run(["git", "config", "user.email"], cwd=root, check=False).strip() or "unknown"
    p = Path(root, STATE_DIR, "acks.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(p.read_text()) if p.is_file() else {"acks": []}
    data["acks"] = [x for x in data["acks"] if x["fingerprint"] != a.fingerprint]
    data["acks"].append({"fingerprint": a.fingerprint, "rule": f["rule"], "file": f["file"],
                         "evidence": f["evidence"], "reason": a.reason.strip(), "by": by, "at": now_iso()})
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"acknowledged {a.fingerprint} ({f['rule']}) in {p.relative_to(root)} - commit this file")
    return 0


def cmd_record_upgrade_test(a) -> int:
    root = run(["git", "rev-parse", "--show-toplevel"], cwd=a.repo).strip()
    sha = run(["git", "rev-parse", "HEAD"], cwd=root).strip()
    p = upgrade_tests_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(p.read_text()) if p.is_file() else {"tests": []}
    by = a.by or run(["git", "config", "user.email"], cwd=root, check=False).strip() or "unknown"
    rec = {"platform": a.platform, "commit": sha, "from_version": a.from_version, "to_version": a.to_version,
           "method": a.method, "result": a.result, "notes": a.notes or "", "by": by, "at": now_iso()}
    data["tests"].append(rec)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"recorded {a.platform} upgrade test {a.from_version} -> {a.to_version}: {a.result.upper()} @ {sha[:10]}")
    return 0


def cmd_ios_build_guard(a) -> int:
    """Run from an Xcode Run Script build phase. Fails Release builds whose
    resolved SPM packages differ from the committed Package.resolved."""
    srcroot = os.environ.get("SRCROOT", a.repo)
    config = os.environ.get("CONFIGURATION", "")
    ctx = Context(srcroot)
    enforce = config in ctx.cfg["release_configurations"] or a.enforce
    level = "error" if enforce else "warning"
    problems = []

    build_dir = os.environ.get("BUILD_DIR") or os.environ.get("BUILD_ROOT")
    state = None
    if build_dir:
        for parent in [Path(build_dir)] + list(Path(build_dir).parents):
            cand = parent / "SourcePackages" / "workspace-state.json"
            if cand.is_file():
                state = cand
                break
    proj_path = os.environ.get("PROJECT_FILE_PATH")
    projects = spm_projects(ctx)
    sp = None
    if proj_path:
        rel = os.path.relpath(os.path.realpath(proj_path), os.path.realpath(ctx.root))
        sp = next((p for p in projects if p.project == rel), None)
    sp = sp or (projects[0] if len(projects) == 1 else None)

    if sp and state:
        committed = ctx.show("HEAD", sp.canonical)
        working = ctx.show(None, sp.canonical)
        if committed is None:
            problems.append(f"{sp.canonical} is not committed")
        else:
            if working != committed:
                problems.append(f"{sp.canonical} has uncommitted changes (Xcode re-resolved packages)")
            for d in compare_pins(parse_workspace_state(state.read_text()), parse_package_resolved(committed)):
                problems.append(f"resolved package mismatch - {d}")
    elif sp and not state:
        problems.append("cannot locate SourcePackages/workspace-state.json - "
                        "set ENABLE_USER_SCRIPT_SANDBOXING = NO for this target")

    check_critical_pinning(ctx)
    check_lockfiles(ctx)
    for f in ctx.findings:
        if f.severity == BLOCKER and f.rule in ("R004", "R005", "R002", "R007"):
            problems.append(f"{f.title} [{f.evidence.splitlines()[0] if f.evidence else ''}]")

    for p in problems:
        print(f"{level}: [release-guard] {p}")
    if problems and enforce:
        print(f"error: [release-guard] {config} build blocked. Open the .xcworkspace, reset package caches, "
              "and make sure the build matches git.")
        return 1
    if not problems:
        print("note: [release-guard] resolved packages match git")
    return 0


def cmd_init(a) -> int:
    root = run(["git", "rev-parse", "--show-toplevel"], cwd=a.repo).strip()
    cfg = Path(root, ".release-guard.json")
    if not cfg.exists():
        cfg.write_text(json.dumps({
            "tag_pattern": a.tag_pattern,
            "release_configurations": ["Release"],
            "critical_package_patterns_extra": [],
            "critical_package_ignore": [],
            "ignore_paths_extra": [],
            "require_upgrade_test": None,
            "artifact_archive": a.archive,
        }, indent=2) + "\n")
        print(f"created {cfg.relative_to(root)}")
    acks = Path(root, STATE_DIR, "acks.json")
    if not acks.exists():
        acks.parent.mkdir(parents=True, exist_ok=True)
        acks.write_text('{\n  "acks": []\n}\n')
        print(f"created {acks.relative_to(root)}")
    gi = Path(root, ".gitignore")
    lines = gi.read_text().splitlines() if gi.exists() else []
    if f"{LOCAL_DIR}/" not in lines:
        with open(gi, "a") as fh:
            fh.write(("\n" if lines and lines[-1] else "") + f"# mobile-release-guard (per-machine state)\n{LOCAL_DIR}/\n")
        print(f"added {LOCAL_DIR}/ to .gitignore")
    dest = Path(root, "tools", "release-guard", "mrg.py")
    if a.vendor:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(__file__, dest)
        dest.chmod(0o755)
        print(f"vendored mrg.py {VERSION} -> {dest.relative_to(root)}")
    return 0


def cmd_archive(a) -> int:
    ctx = Context(a.repo)
    dest_root = a.dest or ctx.cfg.get("artifact_archive")
    if not dest_root:
        raise MrgError("no destination: pass --dest or set artifact_archive in .release-guard.json")
    dest = Path(os.path.expanduser(dest_root), a.platform, f"{a.version}" + (f"+{a.build}" if a.build else ""))
    dest.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for art in a.artifact:
        src = Path(art)
        if not src.exists():
            raise MrgError(f"artifact not found: {art}")
        if src.is_dir():  # .app bundle
            target = shutil.make_archive(str(dest / src.name), "zip", root_dir=src.parent, base_dir=src.name)
        else:
            target = str(dest / src.name)
            shutil.copy2(src, target)
        artifacts.append({"name": os.path.basename(target), "sha256": sha256(target)})
    dirty = bool(ctx.git("status", "--porcelain").strip())
    manifest = {
        "platform": a.platform, "version": a.version, "build": a.build,
        "commit": ctx.head_sha, "describe": ctx.git("describe", "--tags", "--always", "--dirty", check=False).strip(),
        "dirty_tree": dirty, "archived_by": git_user(ctx), "archived_at": now_iso(), "artifacts": artifacts,
        "lockfiles": {p: v for p, v in lock_snapshot(ctx, "HEAD").items()},
        "toolchain": toolchain_versions(a.platform),
    }
    if a.platform == "ios":
        manifest["xcode_resolved_packages"] = latest_derived_data_pins(ctx)
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"archived {len(artifacts)} artifact(s) + manifest.json -> {dest}")
    if dirty:
        print("warning: working tree was dirty - this artifact does not match a commit")
    return 0


def latest_derived_data_pins(ctx: Context) -> dict:
    root = derived_data_root()
    best = None
    if root.is_dir():
        for d in root.iterdir():
            info, state = d / "info.plist", d / "SourcePackages" / "workspace-state.json"
            if not info.is_file() or not state.is_file():
                continue
            try:
                with open(info, "rb") as fh:
                    pl = plistlib.load(fh)
            except Exception:
                continue
            wp = os.path.realpath(pl.get("WorkspacePath", "/nonexistent"))
            if not wp.startswith(os.path.realpath(ctx.root) + os.sep):
                continue
            last = pl.get("LastAccessedDate") or dt.datetime.min
            if best is None or last > best[0]:
                best = (last, wp, state)
    if not best:
        return {}
    return {"workspace": best[1], "pins": {k: pin_label(v) for k, v in parse_workspace_state(best[2].read_text()).items()}}


def toolchain_versions(platform: str) -> dict:
    cmds = {"xcodebuild": ["xcodebuild", "-version"], "flutter": ["flutter", "--version"],
            "node": ["node", "--version"], "java": ["java", "-version"], "pod": ["pod", "--version"]}
    out = {}
    for name, c in cmds.items():
        if shutil.which(c[0]) is None:
            continue
        try:
            p = subprocess.run(c, capture_output=True, text=True, timeout=30)
            out[name] = (p.stdout or p.stderr).strip().splitlines()[0] if (p.stdout or p.stderr) else ""
        except Exception:
            pass
    return out


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mrg", description="mobile release guard - upgrade-safety checks before release")
    ap.add_argument("--version", action="version", version=VERSION)
    ap.add_argument("-C", "--repo", default=".", help="path inside the app's git repo (default: cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("check", help="run static checks (exit 1 if blocked)")
    s.add_argument("--base", help="git ref of the version currently on the store")
    s.add_argument("--scope", choices=["all", "build"], default="all",
                   help="build = only reproducibility checks (for build-time guards)")
    s.add_argument("--json", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true", help="also print INFO findings")
    s.add_argument("--no-save", action="store_true", help="do not write .release-guard/local/last-check.json")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("gate", help="final GO/NO-GO: checks + upgrade test recorded for HEAD")
    s.add_argument("--base")
    s.add_argument("--platform", action="append", choices=["ios", "android"])
    s.set_defaults(fn=cmd_gate)

    s = sub.add_parser("ack", help="acknowledge a reviewed BLOCKER (writes .release-guard/acks.json)")
    s.add_argument("fingerprint")
    s.add_argument("--reason", required=True)
    s.add_argument("--by")
    s.set_defaults(fn=cmd_ack)

    s = sub.add_parser("record-upgrade-test", help="record the result of an upgrade test for HEAD")
    s.add_argument("--platform", required=True, choices=["ios", "android"])
    s.add_argument("--from", dest="from_version", required=True)
    s.add_argument("--to", dest="to_version", required=True)
    s.add_argument("--method", required=True, choices=["simulator", "emulator", "device", "testflight", "play-internal"])
    s.add_argument("--result", required=True, choices=["pass", "fail"])
    s.add_argument("--notes")
    s.add_argument("--by")
    s.set_defaults(fn=cmd_record_upgrade_test)

    s = sub.add_parser("ios-build-guard", help="for an Xcode Run Script phase")
    s.add_argument("--enforce", action="store_true", help="fail even for non-release configurations")
    s.set_defaults(fn=cmd_ios_build_guard)

    s = sub.add_parser("init", help="set up .release-guard.json, acks file, gitignore")
    s.add_argument("--tag-pattern", default="*")
    s.add_argument("--archive", help="artifact archive directory (shared drive)")
    s.add_argument("--vendor", action="store_true", help="copy mrg.py into tools/release-guard/")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("archive", help="store release artifacts + a provenance manifest")
    s.add_argument("--platform", required=True, choices=["ios", "android"])
    s.add_argument("--version", required=True)
    s.add_argument("--build")
    s.add_argument("--artifact", action="append", required=True, help=".ipa/.app/.apk/.aab (repeatable)")
    s.add_argument("--dest")
    s.set_defaults(fn=cmd_archive)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except MrgError as e:
        print(f"mrg: error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
