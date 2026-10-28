# mobile-release-guard

A **mandatory** pre-release gate for mobile apps (iOS, Android, Flutter, React Native), packaged as a Claude Code plugin plus a standalone CLI.

It targets one specific, expensive class of bugs: *users on the current store version update to the new release and get logged out, crash on launch, or lose their data.*

## Why this exists

Upgrade-path bugs are hard to catch with a normal release process:

- **The git diff looks clean.** The break often comes from what was actually built, not from what was committed. Suppose git pins a secure-storage library at `4.1.0`, but the release build is made after opening the `.xcodeproj` instead of the `.xcworkspace`. Xcode quietly resolves `4.2.0`, a version that renames its Keychain keys, and every user is signed out once after updating.
- **QA tests fresh installs.** A clean install never has old Keychain items, old preferences, or an old database schema, so it can't reproduce a problem that only appears when a new build reads data written by an old one.
- **Library changelogs are rarely read.** A minor version bump of a storage, auth, or crypto library can change key names, encryption formats, or default options.

mobile-release-guard makes these risks explicit and blocks the release until each one is fixed, or reviewed and signed off.

## How it blocks a release

| Layer | What it checks | Where it runs |
|---|---|---|
| Reproducible build (R*) | Clean working tree; lockfiles committed; storage/auth/crypto libraries pinned to exact versions; no two diverging `Package.resolved` files; the versions Xcode actually resolved on the build machine (DerivedData) match git | Xcode build phase, Gradle task, `mrg check` |
| Dependency drift (D*) | Lockfile diff between the store version and HEAD; any version change of a critical library is a BLOCKER | `mrg check` |
| Persisted data (P*) | Code diff since the store version: UserDefaults/SharedPreferences keys, Keychain/secure-storage options, database schema, Codable models, bundle id/team/signing/entitlements, destructive migrations | `mrg check` |
| Upgrade test | Install the old build → log in → install the new build over it → still logged in, data intact, no crash. The result is tied to a specific commit | `mrg gate` |

`mrg gate` returns **GO** only when there are no unresolved BLOCKERs **and** a passing upgrade test has been recorded for the exact commit being released.

## Requirements

- Python 3 (the CLI uses only the standard library)
- Git
- [Claude Code](https://claude.com/claude-code), if you want the guided workflow through the skills
- [Maestro](https://maestro.dev), for automated upgrade tests on simulators/emulators

## Installation

```bash
# 1. In Claude Code: add the marketplace (the git repo containing this directory), then install the plugin
/plugin marketplace add <git-url-of-this-repo>
/plugin install mobile-release-guard@tungdevpro

# 2. In each app repository (once), then commit the generated files
python3 <plugin-path>/tools/mrg.py init --vendor --tag-pattern 'v*' --archive '/Volumes/Shared/releases/<app>'
```

`init --vendor` will:

- create `.release-guard.json` (configuration) and `.release-guard/acks.json` (BLOCKERs that have been reviewed and acknowledged as safe),
- add `.release-guard/local/` to `.gitignore`,
- copy `mrg.py` into `tools/release-guard/`, so teammates who don't use Claude can still run it, build phases can call it, and the whole team runs the same version.

Then ask Claude: *"set up the build guard for this repo"* (the `build-reproducibility` skill). This wires the check into your Xcode build phase and/or Gradle build so that a release build made from mismatched dependencies fails immediately.

## Release workflow

Tell Claude: **"run the pre-release check for version 2.4.0"** (the `release-guard` skill). Claude will:

1. Ask which version is currently live on the store and use it as the base.
2. Run `mrg check --base <tag>`, analyze each BLOCKER, and either fix it or propose a verifiable reason to acknowledge it as safe.
3. Run the upgrade test (simulator/emulator via Maestro, plus TestFlight or Google Play internal testing).
4. Run `mrg gate` and produce a GO/NO-GO report you can paste into the release ticket.
5. Archive the release artifacts with a provenance manifest via `mrg archive`.

### Running the CLI manually

```bash
python3 tools/release-guard/mrg.py check --base v2.3.0 -v     # exit code 1 = blocked
python3 tools/release-guard/mrg.py ack <fingerprint> --reason "Migration X reads the old key; upgrade-tested 2.3.0 -> 2.4.0"
python3 tools/release-guard/mrg.py gate --base v2.3.0          # GO / NO-GO
```

Other subcommands:

| Command | Purpose |
|---|---|
| `init` | Create the config, acks file and `.gitignore` entry; `--vendor` copies the CLI into the repo |
| `check` | Run all static checks; exits 1 if anything is blocking |
| `ack` | Acknowledge a reviewed BLOCKER, recording who acknowledged it and why |
| `record-upgrade-test` | Record the result of an upgrade test for HEAD |
| `gate` | Final GO/NO-GO: static checks plus a recorded upgrade test for HEAD |
| `ios-build-guard` | Entry point for an Xcode Run Script build phase |
| `archive` | Store release artifacts together with a provenance manifest |

Run `python3 tools/release-guard/mrg.py <command> --help` for all options.

## Team rules

- **Never disable a check to ship on time.** Every BLOCKER has exactly two outcomes: fix it, or acknowledge it (ack) with a reason someone else can verify. Each ack records the acknowledger's git email and is committed to the repo.
- R001, R002, R005, R007 and R010 (the build does not match git) **cannot be acknowledged**. Fix the cause and rebuild.
- Any new commit after the upgrade test means the test must be run again.
- Archive the artifacts of every release (including the simulator `.app` build) so the next release has an "old version" to upgrade from.
- Bump storage/auth/crypto libraries in a dedicated change with its own upgrade test, not bundled with feature work.

## Project structure

```
.claude-plugin/          plugin.json, marketplace.json
tools/mrg.py             the checker CLI (pure Python 3, no third-party dependencies)
skills/
  release-guard/         orchestration and the GO/NO-GO report
  build-reproducibility/ version pinning, lockfiles, build guards (refs: ios, android, flutter, react-native)
  persistence-review/    analysis of storage changes, migration patterns
  dependency-review/     reading changelogs of upgraded libraries
  upgrade-test/          upgrade-test script and Maestro flows
tests/test_mrg.py        unit tests for the CLI
```

Run the tests with:

```bash
python3 -m unittest discover -s tests
```

## Configuration (`.release-guard.json`)

| Key | Meaning |
|---|---|
| `tag_pattern` | Tag pattern used to find the previous release automatically (e.g. `v*`, `ios/*`) |
| `base_ref` | A fixed base ref, instead of auto-detection |
| `release_configurations` | Xcode build configurations that are hard-blocked (default: `Release`) |
| `critical_package_patterns_extra` | Extra regexes for library names that should be treated as critical |
| `critical_package_ignore` | Libraries wrongly detected as critical (e.g. `firebase_storage`) |
| `ignore_paths_extra` | Additional paths to ignore |
| `require_upgrade_test` | Platforms that require an upgrade test (default: every detected platform) |
| `artifact_archive` | Directory where release artifacts are stored (ideally a shared drive) |
