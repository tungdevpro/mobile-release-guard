---
name: upgrade-test
description: Run and record a real in-place upgrade test for a mobile app - install the version that is on the store, log in and create user data, install the new build over it without uninstalling, and verify the user is still logged in, data is intact and nothing crashes. Automated on iOS simulator / Android emulator with Maestro, with a TestFlight / Play internal-testing procedure for real store builds. Use before every release (release-guard requires it), after fixing a storage migration, or to reproduce "users were logged out / crashed after updating".
---

# Upgrade test

QA almost always tests fresh installs. Users almost never do one: they have
an old version with a session in the keychain, preferences, a database, and
the store replaces the binary underneath it. This test reproduces exactly
that, on the exact commit being released, and records the result so
`mrg.py gate` can enforce it.

## What you need

- **Old build** = the version users have now (the store version), per platform.
  - iOS automated: a *simulator* `.app` of that version. Store `.ipa` files
    are device builds and cannot run on a simulator.
  - Android automated: an `.apk` signed with the **same key** as the new one.
- **New build** of the current HEAD, same bundle id / applicationId,
  same signing key (Android).
- A test account, and Maestro (`maestro --version`) with a booted simulator /
  emulator (`xcrun simctl list devices booted`, `adb devices`).

Look for old builds in the artifact archive first (`artifact_archive` in
`.release-guard.json`, layout `<archive>/<platform>/<version>/` with
`manifest.json`). If none exists, see "No old simulator build" below.

## Flows

Copy `assets/seed_state.yaml` and `assets/verify_state.yaml` (next to this
file) into the app repo, e.g. `.maestro/upgrade/`, and adapt them. Inspect
the real screens first (Maestro MCP `inspect_screen`, or `maestro studio`) to
get ids. Seed should create the state your users actually have: logged in
plus whatever the risky change touches (if a release changes settings
storage, toggle a setting in seed and assert it in verify). Commit the flows -
they are reused every release.

Both flows must keep `clearState: false`; clearing state turns this into a
fresh-install test and hides every bug this exists to find.

## Automated run (simulator / emulator)

```
<this skill's directory>/scripts/upgrade_test.sh \
  --platform ios --app-id com.example.app \
  --old <archive>/ios/2.3.0/MyApp.app.zip --new build/sim/MyApp.app \
  --seed-flow .maestro/upgrade/seed_state.yaml --verify-flow .maestro/upgrade/verify_state.yaml \
  -e TEST_USER=qa@example.com -e TEST_PASSWORD=... \
  --from 2.3.0 --to 2.4.0 --record
```

Android is the same with `--platform android`, `.apk` paths. The script:
clean-installs old, runs seed, installs new **over** it (`simctl install` /
`adb install -r`), runs verify (two cold starts), checks crash reports /
logcat crash buffer, and with `--record` stores pass/fail for HEAD.

Build the new simulator app from the release commit:
`xcodebuild -workspace App.xcworkspace -scheme App -configuration Release -sdk iphonesimulator -derivedDataPath build/sim build`
(the `.app` is under `build/sim/Build/Products/Release-iphonesimulator/`).
For Android, `./gradlew assembleRelease` signed with the upload key, or a
universal APK from the AAB via `bundletool build-apks --mode=universal`.

If it fails, do not retry until it passes. Read the failure: screenshot and
hierarchy in `~/.maestro/tests/<latest>/`, then hand the cause to the
`persistence-review` skill. A logout after upgrade is the bug this test exists
for, not flakiness, until proven otherwise.

Do not pass passwords in a command that ends up in shell history or logs you
share; prefer exporting them in the environment and passing
`-e TEST_PASSWORD="$TEST_PASSWORD"`.

## Store-faithful run (real devices) - recommended for iOS at least once

The simulator test uses builds made from git, but the store binary can differ
from git (e.g. dependencies re-resolved on the build machine), so before release also do the real path once:

- **iOS (TestFlight)**: on a real device, install the current version from the
  App Store, log in, create data. Then install the release candidate from
  TestFlight - TestFlight replaces the App Store app in place and keeps its
  data and keychain. Open, verify, open again.
- **Android (Play internal testing)**: install the production version from
  Play, log in, create data. Join the internal testing track and update to the
  release candidate. (A locally signed APK cannot update a Play-signed app.)

Then record it:
```
python3 <mrg> -C <repo> record-upgrade-test --platform ios --from 2.3.0 --to 2.4.0 \
  --method testflight --result pass --notes "iPhone 13, iOS 18.5, session kept, settings kept"
```

Ask the user to perform the device steps; you cannot do them. Do not record a
pass that you did not see evidence for.

## No old simulator build

If the store version's simulator `.app` was never archived:
1. Build it from the store tag in a separate worktree:
   `git worktree add ../app-2.3.0 v2.3.0`, then build for simulator there.
   Caveat: this is what *git* says 2.3.0 was, not necessarily what shipped. Run
   `mrg.py check` in that worktree and compare its lockfiles with the
   store build's archived `manifest.json` if one exists.
2. Do the store-faithful device run above, which needs no old artifact.
3. From now on, archive a simulator `.app` with every release
   (`mrg.py archive ... --artifact <sim .app>`).

## Skipped versions

Users do not all update every release. When the release touches persistence
(any P-finding or a critical D-finding), also run from the oldest version
that still has meaningful usage (check the store console's version
distribution), e.g. 2.0.0 -> 2.4.0, if that build is archived.
