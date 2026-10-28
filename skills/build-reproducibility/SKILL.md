---
name: build-reproducibility
description: Make mobile release builds match git exactly - lockfiles committed, storage/auth/crypto libraries pinned to exact versions, no .xcodeproj vs .xcworkspace package drift, clean tree - and install build-time guards (Xcode Run Script phase, Gradle task, Flutter/RN release scripts) that fail a Release build when resolved dependencies differ from git. Use for release-guard findings R001-R011, when setting up a new app repo for safe releases, when someone builds releases on a local machine, or when a production bug might come from "the build had a different library version than the repo" (Package.resolved, Podfile.lock, pubspec.lock, package-lock/yarn.lock, gradle.lockfile, upToNextMajor, ^ ranges, dynamic Gradle versions).
---

# Build reproducibility

A release must be a pure function of a git commit. When builds happen on
developer machines, three things quietly break that:

1. **Ranges** (`upToNextMajor`, `^1.2.0`, `~>`, `1.+`) let the resolver pick a
   newer version whenever it re-resolves.
2. **Multiple or ignored lockfiles** - e.g. Xcode keeps a separate
   `Package.resolved` for the `.xcodeproj` and for the `.xcworkspace`; opening
   the wrong one re-resolves with the ranges from (1).
3. **Local state** - uncommitted edits, stale `Pods/`, `node_modules/`, or
   DerivedData that was resolved earlier.

Fix all three layers: pin what matters, commit lockfiles, and add a build-time
guard so the mistake fails loudly on the machine where it happens, without
relying on anyone remembering a checklist.

## Fixing findings

| Rule | Fix |
|---|---|
| R001 dirty tree | Commit or stash, rebuild. Untracked lockfiles count as dirty. |
| R002 lockfile not committed | Generate it (`pod install`, resolve packages in Xcode, `flutter pub get`, `npm install`) and commit it; remove it from `.gitignore`. |
| R004 critical lib not pinned | Pin to the exact version **currently shipped** (read it from the lockfile at the store tag, not "latest"). Ecosystem syntax is in the platform reference below. |
| R005 two Package.resolved disagree | Delete the `.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved` copy, add it to `.gitignore`, always open the `.xcworkspace`. |
| R007 installed deps != lockfile | `pod install` / `npm ci` / `yarn install --frozen-lockfile` / `flutter pub get --enforce-lockfile`. |
| R008 Gradle locking off | Enable dependency locking (android reference). |
| R010 Xcode resolved other versions | Close Xcode, delete that DerivedData folder, open the `.xcworkspace`, File > Packages > Reset Package Caches, rebuild, re-run `check`. Any archive produced before this is suspect - rebuild it. |
| R011 opened via .xcodeproj | Informational: tell the team to use the workspace. |

When pinning, keep the version that the **store build** used, not the newest
one. Upgrading a storage library is a separate, deliberate change that goes
through the `dependency-review` skill and an upgrade test.

## Installing build-time guards

Once per app repo, after `mrg.py init --vendor` has put the checker at
`tools/release-guard/mrg.py`. Read only the reference for the platforms the
repo has:

- iOS (native, and the iOS side of Flutter/RN): `references/ios.md`
- Android (native, and the Android side of Flutter/RN): `references/android.md`
- Flutter: `references/flutter.md`
- React Native: `references/react-native.md`

Edit Gradle files and shell scripts directly. For Xcode, prefer giving the user
exact click-by-click steps over editing `project.pbxproj` by hand, because a
malformed pbxproj breaks the project for the whole team. If the user asks you
to edit it anyway, back it up first and validate with
`plutil -lint` / `xcodebuild -list` afterwards.

After installing, prove the guard works before telling the user it is done:
temporarily change a version in the committed lockfile (or the resolved
state), run a Release build, confirm it fails with the `[release-guard]`
error, then revert.
