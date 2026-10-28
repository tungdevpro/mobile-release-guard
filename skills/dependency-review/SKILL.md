---
name: dependency-review
description: Review dependency changes between the store version and the release candidate for upgrade-breaking behavior - storage/keychain/encryption key or format changes, changed defaults, schema changes, removed migrations - by reading changelogs, release notes and source diffs of the exact versions involved. Use for release-guard findings D001-D005, before bumping a storage/auth/crypto/database library (SecureDefaults, KeychainAccess, flutter_secure_storage, react-native-keychain, security-crypto, Room, Realm, Hive, MMKV, Firebase Auth...), or when asking "is it safe to upgrade library X".
---

# Dependency review

A library upgrade can break upgrading users without a single line of app code
changing: a new version renames its keychain key, changes a default
accessibility, switches the Android cipher, or bumps an on-disk format - even
in a minor or patch release.

## For each D001 / D002 (critical dependency changed / removed)

1. **Get the exact versions** from the finding (`name old -> new`). If the
   lockfile shows a revision instead of a version, resolve it to a tag.
2. **Read what changed between them**, in this order:
   - CHANGELOG / release notes for every version in between (not only the
     newest). GitHub: `gh release list -R owner/repo`, `gh release view <tag> -R owner/repo`,
     or fetch `CHANGELOG.md`. pub.dev / npm / Maven pages have changelogs too.
   - Search them for: key, keychain, service, account, access group,
     accessibility, prefix, encrypt, cipher, storage, migration, schema,
     format, default, breaking, deprecated, reset, delete.
   - If the changelog is thin, diff the source between the two tags for the
     storage-related files: `gh api repos/owner/repo/compare/<old>...<new>`
     or clone and `git diff <old> <new> -- <paths>`. Look at how item names /
     keys / file names are built.
3. **Check transitive changes**: a critical library may have been bumped
   indirectly (another package required it). The lockfile diff shows it; find
   the parent with `npm ls <pkg>`, `flutter pub deps`, `./gradlew :app:dependencies`,
   `pod outdated`.
4. **Conclude** for each: SAFE (no storage-relevant change, with a link /
   quote), NEEDS MIGRATION (the library documents one - implement it, see the
   `persistence-review` migration patterns), or REVERT (pin back to the store
   version; upgrade later as its own release with its own upgrade test).

Unless there is a reason to upgrade now (security fix, store requirement),
recommend reverting critical upgrades that slipped into a release. Mixing a
storage library upgrade with feature work hides the cause when something
breaks.

## D002 removed / D003 added

Removing a storage library while its data still lives on users' devices
means the new version can no longer read it. Make sure the replacement reads
and migrates the old data first (and can do so without the removed library,
or keep it for one more release). Adding a new storage library alongside the
old one is a migration project - check the read path for upgraders.

## D004 / D005

Major bumps of non-critical libraries: skim the breaking changes for
anything touching persistence, startup, or auth flows (e.g. an HTTP client
changing cookie storage, an analytics SDK resetting the device id). Minor
changes (D005) only need attention if the name suggests persistence.

## Output

One line per dependency: `name old -> new: SAFE | MIGRATE | REVERT - reason
(source link)`. For SAFE blockers, propose the `mrg.py ack` reason text,
citing the changelog entry you checked; let the user confirm before acking.
