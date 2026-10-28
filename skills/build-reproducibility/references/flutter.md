# Flutter build guard

## 1. Lock and pin

- Commit `pubspec.lock` (apps always; it is only optional for published packages).
- Pin critical packages exactly in `pubspec.yaml` - no caret:
  `flutter_secure_storage: 9.2.2`, `shared_preferences: 2.3.2`, `hive: 2.2.3`,
  `sqflite: 2.3.3`, auth SDKs. Use `dependency_overrides` only temporarily.
- Git dependencies need `ref:` with a tag or commit sha.

## 2. Release build script

Builds must install exactly the lockfile. Put this in `scripts/release_build.sh`
and use it instead of calling `flutter build` directly:

```sh
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
python3 tools/release-guard/mrg.py check --scope build
flutter pub get --enforce-lockfile      # fails if pubspec.lock would change
( cd ios && pod install --deployment )   # fails if Podfile.lock would change
case "${1:-}" in
  ios)     flutter build ipa --release ;;
  android) flutter build appbundle --release ;;
  *) echo "usage: $0 ios|android"; exit 2 ;;
esac
```

## 3. Native sides

Flutter apps are also iOS and Android apps. Install the iOS Run Script phase
on `ios/Runner.xcodeproj` (see ios.md) and the Gradle task in
`android/app/build.gradle(.kts)` (see android.md). Recent Flutter versions can
use Swift Package Manager for plugins - the iOS guard covers that.

## 4. Storage gotchas specific to Flutter

- `flutter_secure_storage`: `AndroidOptions(encryptedSharedPreferences: ...)`,
  `IOSOptions(accessibility:, groupId:, accountName:)` decide where items
  live. Changing them (or a major version that changes defaults) hides old
  items -> user is logged out. Migrate explicitly: read with old options,
  write with new, delete old.
- `resetOnError: true` turns a read failure into silent data loss.
- `shared_preferences` 2.3+ `SharedPreferencesAsync` / `WithCache` use a
  different backend on Android (DataStore) than the legacy API - data written
  with the legacy API is not visible without the documented migration
  (`migrateLegacySharedPreferencesToSharedPreferencesAsyncIfNecessary`).
- Hive: never reuse or change a `typeId` or `@HiveField` index.
