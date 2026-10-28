# React Native build guard

## 1. Lock and pin

- Commit exactly one JS lockfile (`package-lock.json` or `yarn.lock` or
  `pnpm-lock.yaml`) and `ios/Podfile.lock`.
- Pin critical packages without `^`/`~` in `package.json`:
  `react-native-keychain`, `@react-native-async-storage/async-storage`,
  `react-native-mmkv`, `react-native-encrypted-storage`, `expo-secure-store`,
  `redux-persist`, `realm`, `@op-engineering/op-sqlite`, auth SDKs.

## 2. Release build script

```sh
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
python3 tools/release-guard/mrg.py check --scope build
if [ -f yarn.lock ]; then yarn install --frozen-lockfile    # yarn berry: yarn install --immutable
else npm ci; fi
( cd ios && bundle exec pod install --deployment 2>/dev/null || pod install --deployment )
case "${1:-}" in
  ios)     xcodebuild -workspace ios/App.xcworkspace -scheme App -configuration Release archive -archivePath build/App.xcarchive ;;
  android) ( cd android && ./gradlew bundleRelease ) ;;
  *) echo "usage: $0 ios|android"; exit 2 ;;
esac
```

`npm install` (not `ci`) may rewrite the lockfile and pull newer versions;
`check` reports a critical package whose installed version in `node_modules`
differs from the lockfile as R007.

## 3. Native sides

Install the iOS Run Script phase on the app target in `ios/*.xcodeproj` and
the Gradle task in `android/app/build.gradle` (see ios.md / android.md).

## 4. Storage gotchas specific to React Native

- `react-native-keychain`: `service`, `accessGroup`, `accessible`,
  `storage` (Android cipher) select which item is read. Changing them, or a
  major upgrade that changes the Android default cipher storage, hides old
  credentials. Read with old options and re-save as a migration.
- `redux-persist`: changing `key` or the persisted shape needs `version` +
  `migrate: createMigrate(...)`; otherwise rehydration drops or crashes on
  old state.
- `react-native-mmkv`: instance `id` and `encryptionKey` must stay the same.
- Expo `SecureStore`: `keychainService` option namespaces items.
