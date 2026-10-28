"""Tests for tools/mrg.py. Run: python3 -m unittest discover -s tests -v"""
import datetime as dt
import io
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import mrg  # noqa: E402

PBXPROJ = """// !$*UTF8*$!
{
	objects = {
/* Begin XCRemoteSwiftPackageReference section */
		AA0001 /* XCRemoteSwiftPackageReference "KeychainStore" */ = {
			isa = XCRemoteSwiftPackageReference;
			repositoryURL = "https://github.com/example/KeychainStore.git";
			requirement = {
				kind = %s;
				%s = 2.3.0;
			};
		};
		AA0002 /* XCRemoteSwiftPackageReference "Alamofire" */ = {
			isa = XCRemoteSwiftPackageReference;
			repositoryURL = "https://github.com/Alamofire/Alamofire.git";
			requirement = {
				kind = upToNextMajorVersion;
				minimumVersion = 5.8.0;
			};
		};
/* End XCRemoteSwiftPackageReference section */
	};
}
"""

WORKSPACE = """<?xml version="1.0" encoding="UTF-8"?>
<Workspace version = "1.0">
   <FileRef location = "group:MyApp.xcodeproj"></FileRef>
   <FileRef location = "group:Pods/Pods.xcodeproj"></FileRef>
</Workspace>
"""


def resolved(secure_version, rev="aaa111"):
    return json.dumps({"pins": [
        {"identity": "keychainstore", "kind": "remoteSourceControl",
         "location": "https://github.com/example/KeychainStore.git",
         "state": {"revision": rev, "version": secure_version}},
        {"identity": "alamofire", "kind": "remoteSourceControl",
         "location": "https://github.com/Alamofire/Alamofire.git",
         "state": {"revision": "fff000", "version": "5.8.1"}},
    ], "version": 2}, indent=2)


def workspace_state(secure_version, rev):
    deps = [
        {"packageRef": {"identity": "keychainstore", "kind": "remoteSourceControl",
                        "location": "https://github.com/example/KeychainStore.git", "name": "KeychainStore"},
         "state": {"checkoutState": {"revision": rev, "version": secure_version}, "name": "sourceControlCheckout"},
         "subpath": "KeychainStore"},
        {"packageRef": {"identity": "alamofire", "kind": "remoteSourceControl",
                        "location": "https://github.com/Alamofire/Alamofire.git", "name": "Alamofire"},
         "state": {"checkoutState": {"revision": "fff000", "version": "5.8.1"}, "name": "sourceControlCheckout"},
         "subpath": "Alamofire"},
    ]
    return json.dumps({"object": {"artifacts": [], "dependencies": deps}, "version": 6})


class Repo:
    def __init__(self, tmp):
        self.root = Path(tmp, "app")
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "dev@example.com")
        self.git("config", "user.name", "Dev")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "tag.gpgsign", "false")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, msg, tag=None):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        if tag:
            self.git("tag", tag)


def check(root, *extra):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mrg.main(["-C", str(root), "check", "--json", *extra])
    return rc, json.loads(buf.getvalue())


def rules(report, severity=None):
    return {f["rule"] for f in report["findings"] if severity is None or f["severity"] == severity}


class ResolvedVersionDriftTest(unittest.TestCase):
    """Package.resolved in git says KeychainStore 2.3.0, but the release was
    built after opening the .xcodeproj, which resolved 2.5.0."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(self.tmp.name)
        r = self.repo
        r.write("MyApp.xcodeproj/project.pbxproj", PBXPROJ % ("upToNextMajorVersion", "minimumVersion"))
        r.write("MyApp.xcworkspace/contents.xcworkspacedata", WORKSPACE)
        r.write("MyApp.xcworkspace/xcshareddata/swiftpm/Package.resolved", resolved("2.3.0"))
        r.write("MyApp/Session.swift", 'let store = KeychainStore()\nlet tokenKey = "auth_token"\n')
        r.commit("2.3.0", tag="v2.3.0")
        r.write("MyApp/Feature.swift", "struct Feature {}\n")
        r.commit("2.4.0 (12)")
        self.dd = Path(self.tmp.name, "DerivedData")
        os.environ["MRG_DERIVED_DATA"] = str(self.dd)

    def tearDown(self):
        os.environ.pop("MRG_DERIVED_DATA", None)
        self.tmp.cleanup()

    def open_xcodeproj_and_resolve(self, version="2.5.0", rev="bbb222", accessed=None):
        r = self.repo
        r.write("MyApp.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved", resolved(version, rev))
        d = self.dd / "MyApp-abcdef"
        (d / "SourcePackages").mkdir(parents=True)
        (d / "SourcePackages/workspace-state.json").write_text(workspace_state(version, rev))
        with open(d / "info.plist", "wb") as fh:
            plistlib.dump({"WorkspacePath": str(r.root / "MyApp.xcodeproj"),
                           "LastAccessedDate": accessed or dt.datetime.utcnow()}, fh)

    def test_clean_state_only_flags_unpinned_critical_package(self):
        rc, rep = check(self.repo.root)
        self.assertEqual(rc, 1)
        self.assertEqual(rules(rep, "BLOCKER"), {"R004"})
        r004 = [f for f in rep["findings"] if f["rule"] == "R004"]
        self.assertEqual(len(r004), 1)
        self.assertIn("KeychainStore", r004[0]["title"])  # Alamofire is not critical

    def test_resolved_version_drift_is_blocked(self):
        self.open_xcodeproj_and_resolve()
        rc, rep = check(self.repo.root)
        self.assertEqual(rc, 1)
        blockers = rules(rep, "BLOCKER")
        self.assertIn("R010", blockers)  # DerivedData resolved 2.5.0, git says 2.3.0
        self.assertIn("R005", blockers)  # two Package.resolved disagree
        self.assertIn("R001", blockers)  # untracked lockfile = dirty tree
        r010 = next(f for f in rep["findings"] if f["rule"] == "R010")
        self.assertIn("keychainstore: built 2.5.0, git has 2.3.0", r010["evidence"])
        self.assertIn("instead of MyApp.xcworkspace", r010["title"])

    def test_stale_derived_data_is_only_a_warning(self):
        self.open_xcodeproj_and_resolve(accessed=dt.datetime.utcnow() - dt.timedelta(days=60))
        _, rep = check(self.repo.root)
        self.assertIn("R010s", rules(rep, "WARN"))
        self.assertNotIn("R010", rules(rep))

    def test_pinned_and_matching_build_passes(self):
        r = self.repo
        r.write("MyApp.xcodeproj/project.pbxproj", PBXPROJ % ("exactVersion", "version"))
        r.commit("pin KeychainStore")
        self.open_xcodeproj_and_resolve(version="2.3.0", rev="aaa111")
        r.git("rm", "-q", "--cached", "-r", "--ignore-unmatch", "MyApp.xcodeproj/project.xcworkspace")
        (r.root / ".gitignore").write_text("MyApp.xcodeproj/project.xcworkspace/\n")
        r.commit("ignore embedded workspace")
        rc, rep = check(r.root)
        self.assertEqual(rules(rep, "BLOCKER"), set(), rep)
        self.assertEqual(rc, 0)
        self.assertIn("R011", rules(rep, "WARN"))  # still warns about opening the .xcodeproj

    def test_dependency_bump_in_git_is_blocker(self):
        r = self.repo
        r.write("MyApp.xcworkspace/xcshareddata/swiftpm/Package.resolved", resolved("2.5.0", "bbb222"))
        r.commit("bump KeychainStore")
        _, rep = check(r.root)
        d001 = [f for f in rep["findings"] if f["rule"] == "D001"]
        self.assertEqual(len(d001), 1)
        self.assertIn("keychainstore 2.3.0 -> 2.5.0", d001[0]["evidence"])

    def test_build_guard_fails_release_build(self):
        self.open_xcodeproj_and_resolve()
        env = {"SRCROOT": str(self.repo.root), "CONFIGURATION": "Release",
               "BUILD_DIR": str(self.dd / "MyApp-abcdef/Build/Products"),
               "PROJECT_FILE_PATH": str(self.repo.root / "MyApp.xcodeproj")}
        old = dict(os.environ)
        os.environ.update(env)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = mrg.main(["ios-build-guard"])
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual(rc, 1)
        out = buf.getvalue()
        self.assertIn("error: [release-guard] resolved package mismatch - keychainstore: built 2.5.0, git has 2.3.0", out)

    def test_build_guard_only_warns_for_debug(self):
        self.open_xcodeproj_and_resolve()
        old = dict(os.environ)
        os.environ.update({"SRCROOT": str(self.repo.root), "CONFIGURATION": "Debug",
                           "BUILD_DIR": str(self.dd / "MyApp-abcdef/Build/Products")})
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = mrg.main(["ios-build-guard"])
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual(rc, 0)
        self.assertIn("warning: [release-guard]", buf.getvalue())


class PersistenceDiffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(self.tmp.name)
        os.environ["MRG_DERIVED_DATA"] = str(Path(self.tmp.name, "none"))

    def tearDown(self):
        os.environ.pop("MRG_DERIVED_DATA", None)
        self.tmp.cleanup()

    def test_android_pref_key_rename_and_destructive_migration(self):
        r = self.repo
        r.write("app/build.gradle", 'plugins { id "com.android.application" }\nandroid { defaultConfig { applicationId "com.x.app" } }\n'
                'dependencies { implementation "androidx.security:security-crypto:1.1.0-alpha06" }\n')
        r.write("app/src/main/java/com/x/SessionManager.kt",
                'class SessionManager(p: SharedPreferences) {\n  fun token() = p.getString("token", null)\n}\n')
        r.write("app/src/main/java/com/x/AppDb.kt",
                "val db = Room.databaseBuilder(ctx, AppDb::class.java, \"app.db\")\n  .build()\n")
        r.commit("1.0.0", tag="v1.0.0")
        r.write("app/src/main/java/com/x/SessionManager.kt",
                'class SessionManager(p: SharedPreferences) {\n  fun token() = p.getString("access_token", null)\n}\n')
        r.write("app/src/main/java/com/x/AppDb.kt",
                "val db = Room.databaseBuilder(ctx, AppDb::class.java, \"app.db\")\n  .fallbackToDestructiveMigration()\n  .build()\n")
        r.commit("1.1.0")
        rc, rep = check(r.root)
        self.assertEqual(rc, 1)
        p202 = next(f for f in rep["findings"] if f["rule"] == "P202")
        self.assertEqual(p202["severity"], "BLOCKER")
        self.assertIn('"token"', p202["evidence"])
        self.assertIn("access_token", p202["detail"])
        self.assertIn("P106", rules(rep, "BLOCKER"))
        self.assertIn("R008", rules(rep, "WARN"))

    def test_moved_code_is_not_flagged(self):
        r = self.repo
        r.write("ios/App.xcodeproj/project.pbxproj", "// empty\n")
        r.write("ios/App/Old.swift", 'func save() { UserDefaults.standard.set(1, forKey: "launches") }\n')
        r.commit("1.0", tag="v1.0")
        (r.root / "ios/App/Old.swift").unlink()
        r.write("ios/App/Settings/Launch.swift",
                '// moved\nfunc save() { UserDefaults.standard.set(1, forKey: "launches") }\nfunc other() {}\n')
        r.commit("move")
        _, rep = check(r.root)
        self.assertNotIn("P103", rules(rep, "BLOCKER"))

    def test_flutter_secure_storage_change_and_unpinned(self):
        r = self.repo
        r.write("pubspec.yaml", "name: app\ndependencies:\n  flutter:\n    sdk: flutter\n"
                "  flutter_secure_storage: ^9.0.0\n  http: ^1.2.0\n")
        r.write("pubspec.lock", 'packages:\n  flutter_secure_storage:\n    dependency: "direct main"\n'
                '    version: "9.0.0"\n  http:\n    version: "1.2.0"\n')
        r.write("ios/Runner.xcodeproj/project.pbxproj", "// empty\n")
        r.write("lib/auth/token_store.dart",
                "const storage = FlutterSecureStorage();\nFuture<String?> t() => storage.read(key: 'jwt');\n")
        r.commit("1.0.0", tag="v1.0.0")
        r.write("lib/auth/token_store.dart",
                "const storage = FlutterSecureStorage(aOptions: AndroidOptions(encryptedSharedPreferences: true));\n"
                "Future<String?> t() => storage.read(key: 'jwt');\n")
        r.write("pubspec.lock", 'packages:\n  flutter_secure_storage:\n    dependency: "direct main"\n'
                '    version: "9.2.2"\n  http:\n    version: "1.2.1"\n')
        r.commit("1.1.0")
        _, rep = check(r.root)
        blockers = rules(rep, "BLOCKER")
        self.assertIn("P301", blockers)
        self.assertIn("R004", blockers)
        self.assertIn("D001", blockers)
        r004 = next(f for f in rep["findings"] if f["rule"] == "R004")
        self.assertIn("flutter_secure_storage", r004["title"])
        self.assertIn("D005", rules(rep, "INFO"))  # http bump is informational

    def test_ack_unblocks_but_not_for_broken_build(self):
        r = self.repo
        r.write("ios/App.xcodeproj/project.pbxproj", "// empty\n")
        r.write("ios/App/Store.swift", 'let k = "user_token"\nfunc f() { d.string(forKey: "user_token") }\n')
        r.commit("1.0", tag="v1.0")
        r.write("ios/App/Store.swift", 'let k = "user_token"\nfunc f() { d.string(forKey: "token_v2") }\n')
        r.commit("1.1")
        rc, rep = check(r.root)
        self.assertEqual(rc, 1)
        fp = next(f["fingerprint"] for f in rep["findings"] if f["rule"] == "P103")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(mrg.main(["-C", str(r.root), "ack", fp, "--reason", "short"]), 2)
            self.assertEqual(mrg.main(["-C", str(r.root), "ack", fp, "--reason",
                                       "MigrationV2 copies user_token to token_v2 on first launch; upgrade-tested 1.0->1.1"]), 0)
        r.commit("ack")
        rc, rep = check(r.root)
        self.assertEqual(rc, 0, rep)


class RealRepoRegressionTest(unittest.TestCase):
    """Behaviour found while dogfooding on real projects."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(self.tmp.name)
        os.environ["MRG_DERIVED_DATA"] = str(Path(self.tmp.name, "none"))

    def tearDown(self):
        os.environ.pop("MRG_DERIVED_DATA", None)
        self.tmp.cleanup()

    def pbx(self, debug_id, release_id):
        return ("{\n\tobjects = {\n"
                "\t\tC1 /* Debug */ = {\n\t\t\tisa = XCBuildConfiguration;\n\t\t\tbuildSettings = {\n"
                f"\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = {debug_id};\n\t\t\t}};\n\t\t\tname = Debug;\n\t\t}};\n"
                "\t\tC2 /* Release */ = {\n\t\t\tisa = XCBuildConfiguration;\n\t\t\tbuildSettings = {\n"
                f"\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = {release_id};\n\t\t\t}};\n\t\t\tname = Release;\n\t\t}};\n"
                "\t};\n}\n")

    def test_bundle_id_change_only_in_debug_is_warning(self):
        r = self.repo
        r.write("App.xcodeproj/project.pbxproj", self.pbx("com.a.app", "com.a.app"))
        r.commit("1.0", tag="v1.0")
        r.write("App.xcodeproj/project.pbxproj", self.pbx("com.a.app.dev", "com.a.app"))
        r.commit("1.1")
        _, rep = check(r.root)
        p501 = [f for f in rep["findings"] if f["rule"] == "P501"]
        self.assertEqual([f["severity"] for f in p501], ["WARN"])
        self.assertIn("config: Debug", p501[0]["title"])

    def test_bundle_id_change_in_release_is_blocker(self):
        r = self.repo
        r.write("App.xcodeproj/project.pbxproj", self.pbx("com.a.app", "com.a.app"))
        r.commit("1.0", tag="v1.0")
        r.write("App.xcodeproj/project.pbxproj", self.pbx("com.a.app", "com.a.newapp"))
        r.commit("1.1")
        _, rep = check(r.root)
        self.assertIn("P501", rules(rep, "BLOCKER"))

    def test_tracked_build_outputs_get_specific_hint(self):
        r = self.repo
        r.write("app/build.gradle", 'plugins { id "com.android.application" }\n')
        r.write("app/release/app-release.aab", "v1")
        r.commit("1.0", tag="v1.0")
        r.write("app/release/app-release.aab", "v2")
        _, rep = check(r.root)
        r001 = next(f for f in rep["findings"] if f["rule"] == "R001")
        self.assertIn("build outputs committed to git", r001["hint"])

    def test_both_package_resolved_tracked_warns(self):
        r = self.repo
        r.write("MyApp.xcodeproj/project.pbxproj", PBXPROJ % ("exactVersion", "version"))
        r.write("MyApp.xcworkspace/contents.xcworkspacedata", WORKSPACE)
        r.write("MyApp.xcworkspace/xcshareddata/swiftpm/Package.resolved", resolved("2.3.0"))
        r.write("MyApp.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved", resolved("2.3.0"))
        r.commit("1.0", tag="v1.0")
        _, rep = check(r.root)
        self.assertIn("R006", rules(rep, "WARN"))
        self.assertNotIn("R005", rules(rep))

    def test_gate_without_base_is_no_go(self):
        r = self.repo
        r.write("app/build.gradle", 'plugins { id "com.android.application" }\ndependencyLocking { lockAllConfigurations() }\n')
        r.write(".gitignore", ".release-guard/local/\n")
        r.commit("initial")
        with redirect_stdout(io.StringIO()):
            mrg.main(["-C", str(r.root), "record-upgrade-test", "--platform", "android", "--from", "1.0",
                      "--to", "1.1", "--method", "emulator", "--result", "pass"])
            self.assertEqual(mrg.main(["-C", str(r.root), "gate"]), 1)


class GateTest(unittest.TestCase):
    def test_gate_requires_upgrade_test_on_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MRG_DERIVED_DATA"] = str(Path(tmp, "none"))
            r = Repo(tmp)
            r.write("app/build.gradle", 'plugins { id "com.android.application" }\ndependencyLocking { lockAllConfigurations() }\n')
            r.write(".gitignore", ".release-guard/local/\n")
            r.commit("1.0", tag="v1.0")
            r.write("app/src/main/Foo.kt", "class Foo\n")
            r.commit("1.1")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(mrg.main(["-C", str(r.root), "gate"]), 1)
                mrg.main(["-C", str(r.root), "record-upgrade-test", "--platform", "android", "--from", "1.0",
                          "--to", "1.1", "--method", "emulator", "--result", "pass"])
                self.assertEqual(mrg.main(["-C", str(r.root), "gate"]), 0)
                r.write("app/src/main/Bar.kt", "class Bar\n")
                r.commit("late change")
                self.assertEqual(mrg.main(["-C", str(r.root), "gate"]), 1)  # result belongs to old commit
            os.environ.pop("MRG_DERIVED_DATA", None)


if __name__ == "__main__":
    unittest.main()
