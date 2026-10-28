---
name: persistence-review
description: Decide whether code changes since the store version keep data written by the old app version readable - keychain / secure storage items, UserDefaults / SharedPreferences / DataStore keys, Core Data / SwiftData / Room / Realm / Hive / sqflite schema, Codable / JSON models cached on disk, app group / bundle id / signing / entitlements - and write the migration when they don't. Use for release-guard findings P001-P900, when a PR renames a storage key or changes secure storage options, when adding a DB migration, or when investigating "users logged out / lost data / crashed after updating the app".
---

# Persistence review

Every app update is a data migration whether or not anyone wrote one: the new
binary starts with whatever the old binary left on disk and in the keychain.
A change is safe only if **the new version can read everything the old
version could have written** - including from users who skip versions
(1.0 -> 1.3).

The checker (`mrg.py check`) finds candidate lines with patterns; it cannot
tell whether a key is actually persisted or whether a migration elsewhere
already covers it. That judgment is your job here.

## For each P-finding

1. **Open the full diff for the file**, not just the flagged lines:
   `git diff <base> HEAD -- <file>`. The finding's `detail` shows what replaced
   the removed lines.
2. **Classify the change:**
   - *Rename / move*: key, keychain service/account/access group/accessibility,
     prefs file name, DataStore name, DB file name, box name, app group.
   - *Format*: model fields / CodingKeys / `@SerialName` / `@HiveField` /
     DB schema / encryption scheme.
   - *Identity*: bundle id, team id, entitlements, applicationId, signing.
   - *Destructive*: `fallbackToDestructiveMigration`, `deleteRealmIfMigrationNeeded`,
     `resetOnError`, clearing storage on error.
   - *Not persisted*: the flagged code only touches in-memory or network data.
3. **Trace the read path for the old data.** Search for the old key/value
   (`git grep -n '<old literal>' HEAD`) and for migration code. Ask: on the
   first launch after updating, what code reads the old value, and what
   happens if it is missing or in the old format? "Returns nil -> app shows
   login" is the logout bug. "Force-unwrap / `try!` / unchecked cast" is the
   crash bug.
4. **Check whether the old value ever shipped.** If the key was introduced
   after the store tag and is renamed before release, no user has it:
   `git show <base>:<file> | grep '<old literal>'`. Not in the base = safe.
5. **Conclude**: safe (with a one-sentence reason a teammate can verify) or
   needs a fix.

## Fixes

Prefer, in this order:
1. **Don't change it.** Keep the old key / service / options; rename only the
   variable in code. Usually the cheapest and safest fix.
2. **Read-old-write-new migration**, idempotent, run before first use, old
   value deleted only after the new one is written. Patterns per platform are
   in `references/migrations.md` - read the relevant section.
3. **Versioned schema migration** for databases (new Core Data model version,
   Room `Migration`, Realm `schemaVersion` + block, Hive new field index,
   sqflite `onUpgrade`).
4. **Tolerant decoding** for cached models: optional / default values for new
   fields, never crash on old data, treat undecodable cache as a cache miss.
   Credentials are not a cache - losing them is the logout bug.

Add observability with the fix: log an analytics/crash-reporting event when a
migration fails or when an expected credential is missing after an update
(e.g. `session_missing_after_upgrade` with old/new app version). If this
slips through again, the team sees it within hours instead of from reviews.

## Rule notes

- **P101/P102/P301/P402 (keychain / secure storage options)**: also suspect a
  *library* upgrade that changes defaults (see `dependency-review`). Same app
  code with a different library version can mean a new key name.
- **P501/P502/P505 (team id, bundle id, entitlements)**: keychain items are
  scoped to `<TeamID>.<access group>`. Changing team or access group = all
  items invisible. Treat as a blocker unless there is a shared access group
  covering both.
- **P503 (applicationId / signing)**: a different applicationId is a different
  app; a different signing certificate cannot update in place. Not fixable by
  migration - revert.
- **P504 (backup)**: restoring Keystore-encrypted data on a new device fails to
  decrypt; make the read path catch and recover.
- **P601 (Core Data model edited in place)**: always a blocker - a store
  created by the shipped model no longer matches any model version. Revert and
  add a new model version.
- **P900 (persistence-related file changed)**: a catch-all for files whose
  name suggests persistence. Skim the diff; most are fine. Only escalate if you
  find something from the classification above.

## Output

For each finding, give one line: rule, file:line, verdict (SAFE / FIX /
NEEDS-TEST), and the reason. For SAFE blockers, propose the exact `--reason`
text for `mrg.py ack` and let the user decide. For FIX, implement the fix,
add or update a unit test that seeds the old value and asserts the new code
reads it, and mention which upgrade-test step will prove it on device.
