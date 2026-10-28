# Migration patterns

Principles for every pattern below:
- Run before anything reads the value (app start, before session restore).
- Idempotent: safe to run on every launch, safe if interrupted halfway.
- Write new first, verify, then delete old. Never delete old on failure.
- Gate with a "migrated to vN" flag only *after* success.
- Log failure to crash reporting with old/new app version.

Contents: iOS keychain - iOS UserDefaults - Core Data - Android prefs -
Android EncryptedSharedPreferences - Room - Flutter secure storage -
Flutter shared_preferences - Hive - React Native keychain - redux-persist -
Codable / JSON cache.

## iOS: keychain item moved (service / account / access group / accessibility)

```swift
enum KeychainMigration {
    static func migrateToken() {
        let new = Keychain(service: "com.app.auth")          // new location
        if (try? new.getData("token")) != nil { return }       // already migrated
        let old = Keychain(service: "com.app")                 // where 1.x stored it
        guard let data = try? old.getData("auth_token") else { return }
        do {
            try new.set(data, key: "token")
            try old.remove("auth_token")
        } catch {
            Crash.log("keychain_migration_failed", error)      // keep old item
        }
    }
}
```

With a library that derives key names (SecureDefaults, KeychainWrapper with
prefixes), instantiate the old configuration / pinned old version logic to
read, then write with the new one. If the library itself changed its naming
scheme, read the raw item with `SecItemCopyMatching` using the old
service/account the old version used (check the library's source at the old
tag).

`kSecAttrAccessible` change: you cannot update in place reliably on all
iOS versions - read, delete, re-add with the new accessibility.

## iOS: UserDefaults key renamed

```swift
let d = UserDefaults.standard
if d.object(forKey: "onboardingDone") == nil, let v = d.object(forKey: "didOnboard") {
    d.set(v, forKey: "onboardingDone")
    d.removeObject(forKey: "didOnboard")
}
```

Suite change (`UserDefaults(suiteName:)` for app groups): copy keys from
`.standard` to the suite once.

## iOS: Core Data

Never edit a shipped `.xcdatamodel`. Editor > Add Model Version, make changes
in the new version, set it current. Lightweight migration is on by default
with `NSPersistentContainer`; for renames set the *Renaming ID* on the new
attribute to the old name. Non-trivial changes need a mapping model.
SwiftData: `VersionedSchema` per shipped version + `SchemaMigrationPlan`.

## Android: SharedPreferences key or file renamed

```kotlin
fun migratePrefs(ctx: Context) {
    val new = ctx.getSharedPreferences("session", MODE_PRIVATE)
    if (new.contains("access_token")) return
    val old = ctx.getSharedPreferences("app_prefs", MODE_PRIVATE)
    val token = old.getString("token", null) ?: return
    if (new.edit().putString("access_token", token).commit()) {
        old.edit().remove("token").apply()
    }
}
```

Moving to DataStore: use `SharedPreferencesMigration(context, "app_prefs")`
in `preferencesDataStore(produceMigrations = ...)` and keep key names.

## Android: EncryptedSharedPreferences / Keystore

The prefs file name, MasterKey alias and key schemes identify the data.
Changing any of them = new empty store. To move: open old with old params,
copy entries, open new, write, then `deleteSharedPreferences(oldName)`.
Wrap opening in try/catch for `GeneralSecurityException` / `AEADBadTagException`
(happens after backup-restore or key invalidation): delete the corrupted
file, log, and continue as logged-out - do not crash.

## Android: Room

Bump `version` in `@Database`, add `Migration(old, new)` or
`@AutoMigration(from, to, spec = ...)` (renames need a spec with
`@RenameColumn`/`@RenameTable`). Keep `exportSchema = true` and the
`schemas/` JSON committed; test with `MigrationTestHelper`. Never ship
`fallbackToDestructiveMigration()` for tables holding user data.

## Flutter: flutter_secure_storage options changed

```dart
Future<void> migrateSecureStorage() async {
  const newStore = FlutterSecureStorage(
      aOptions: AndroidOptions(encryptedSharedPreferences: true),
      iOptions: IOSOptions(accessibility: KeychainAccessibility.first_unlock));
  if (await newStore.containsKey(key: 'jwt')) return;
  const oldStore = FlutterSecureStorage();                 // options used by 1.x
  final v = await oldStore.read(key: 'jwt');
  if (v == null) return;
  await newStore.write(key: 'jwt', value: v);
  await oldStore.delete(key: 'jwt');
}
```

Check the package CHANGELOG for default option changes between the store
version and the new one - a default change has the same effect as changing
the option in code.

## Flutter: shared_preferences

Key rename: same pattern as iOS UserDefaults (`getX(old)` -> `setX(new)` ->
`remove(old)`). Moving from the legacy `SharedPreferences` API to
`SharedPreferencesAsync`/`WithCache`: call
`migrateLegacySharedPreferencesToSharedPreferencesAsyncIfNecessary(...)`
once at startup, before reading.

## Flutter: Hive

Never change a `typeId` or an existing `@HiveField(n)` index; add new fields
with new indexes and default values; a removed field's index is retired
forever. Box renamed: open old box, copy, close, `deleteBoxFromDisk` old.

## React Native: react-native-keychain

```ts
export async function migrateCredentials() {
  const fresh = await Keychain.getGenericPassword({ service: 'com.app.auth' });
  if (fresh) return;
  const old = await Keychain.getGenericPassword();          // default service used by 1.x
  if (!old) return;
  await Keychain.setGenericPassword(old.username, old.password, { service: 'com.app.auth' });
  await Keychain.resetGenericPassword();
}
```

## React Native: redux-persist

```ts
const migrations = {
  2: (state: any) => ({ ...state, auth: { token: state.session?.token ?? null } }),
};
const persistConfig = { key: 'root', storage, version: 2, migrate: createMigrate(migrations) };
```

Never change `key`. Every shape change of a persisted slice gets a version.

## Codable / JSON cache on disk

```swift
struct Profile: Codable {
    var name: String
    var avatarURL: URL?                 // new field: optional, or provide default
    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        avatarURL = try c.decodeIfPresent(URL.self, forKey: .avatarURL)
    }
}
// Load: on failure treat as cache miss, never `try!`
let profile = (try? JSONDecoder().decode(Profile.self, from: data)) ?? nil
```

Kotlin: default values + `ignoreUnknownKeys = true` / `coerceInputValues`.
Dart: nullable fields or `@JsonKey(defaultValue: ...)`.
Credentials must never live only inside a cache blob that is dropped on a
decode failure.
