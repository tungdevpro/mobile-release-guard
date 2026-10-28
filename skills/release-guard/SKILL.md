---
name: release-guard
description: Hard pre-release gate for mobile apps (iOS, Android, Flutter, React Native) that blocks releases which would break users updating from the store version - logged out after update, crash on first launch, lost data. Use it whenever someone is about to build, archive, upload or submit an app to the App Store / Google Play / TestFlight, bumps the version for a release, asks "can we release", "check before release", "release checklist", "kiểm tra trước khi release", "chuẩn bị lên store", or investigates "users got logged out / crashed after updating". Also use it when reviewing changes to keychain, secure storage, preferences, database schema, bundle id/signing, or dependency versions of storage/auth libraries.
---

# Release guard

You run the final GO / NO-GO gate before a mobile release. The question this
gate answers is: **"A real user on the version that is live on the store, with
real saved state, updates to this build. Does anything break?"**

Most of these bugs are invisible to normal QA because QA does fresh installs
and code review looks at code, not at what actually got built. A typical
example: the git repo pins a keychain wrapper library at `4.1.0`, but the
release is built after someone opens the `.xcodeproj` instead of the
`.xcworkspace`, Xcode re-resolves the `upToNextMajor` range to `4.2.0`, which
renames its keychain key - every user is logged out once after updating. No
diff in git shows anything. So this gate checks the machine, the lockfiles,
the diff since the store version, **and** requires a real upgrade test.

The gate is hard: if it says NO-GO, the release does not ship. Your job is to
help the team get to GO honestly - by fixing problems, or by documenting why a
flagged change is safe - never by weakening the check.

## Tooling

The deterministic checks live in `mrg.py` (stdlib Python 3). Locate it in this order:
1. `<app repo>/tools/release-guard/mrg.py` (vendored - the version the team agreed on)
2. `<this skill's directory>/../../tools/mrg.py` (the plugin copy)

Call it as `python3 <mrg> -C <app repo> <command>`. Commands: `check`, `gate`,
`ack`, `record-upgrade-test`, `init`, `archive`, `ios-build-guard`.
Run `python3 <mrg> <command> --help` for flags.

## Workflow

### 1. Establish the base: what is on the store right now?

Everything is measured against the version users currently have. Ask the user
which version is live on each store if it is not obvious, then find its git
tag (`git tag --sort=-creatordate | head`). iOS and Android may be on different
versions - then run the checks once per platform with its own `--base`. If
there is no tag for the live version, ask for the commit it was built from;
note in the report that the base is uncertain, because a wrong base silently
hides changes.

If `.release-guard.json` does not exist, run `init --vendor` (creates config,
`.release-guard/acks.json`, gitignores `.release-guard/local/`, vendors
`mrg.py` into the repo) and tell the user to commit those files.

### 2. Run the static checks

```
python3 <mrg> -C <repo> check --base <store-tag> -v
```

Exit 1 means blocked. Read every BLOCKER and WARN. Group them for the user
by kind and handle each kind with the matching skill:

| Rules | Meaning | Handle with |
|---|---|---|
| R001-R011 | Build is not reproducible from git (dirty tree, missing/disagreeing lockfiles, unpinned critical libs, Xcode resolved different versions than git) | `build-reproducibility` skill |
| D001-D005 | Dependencies changed since the store version | `dependency-review` skill |
| P001-P900 | Code/config that touches persisted data changed | `persistence-review` skill |

R001, R002, R005, R007 and R010 cannot be acknowledged: they mean the binary
you would ship is not what git says. They must be fixed and the build redone.

### 3. Resolve each BLOCKER: fix it or acknowledge it with evidence

For each remaining BLOCKER, investigate (read the code, the diff, the
changelog) and reach one of two conclusions:

- **It is a real upgrade risk** - propose the fix (pin the version, add a
  migration that reads the old key/format, revert the rename...). After the fix
  is committed, re-run `check`.
- **It is safe** - for example a migration already reads the old key, or the
  renamed key was never shipped to the store. Write down *why* in one or two
  sentences that a teammate could verify, and show it to the user. Only after
  the user confirms, record it:
  `python3 <mrg> -C <repo> ack <fingerprint> --reason "<why upgrading users are safe + how verified>"`
  then commit `.release-guard/acks.json`.

Do not acknowledge on your own initiative. An ack is a human sign-off, and it
is attributed to the person's git email. If you are unsure whether something
is safe, say so and recommend proving it with the upgrade test rather than
acknowledging it.

WARN findings do not block, but read them: if your investigation shows a WARN
is actually a real break (e.g. a `P107` Codable change on a model that *is*
cached to disk), treat it as a blocker in your verdict and fix it.

### 4. Upgrade test on the real build

Static analysis cannot see everything (server-side token changes, OS behavior,
a library changing its storage format internally). Every release needs a
passing upgrade test from the store version to this exact commit, per
platform. Use the `upgrade-test` skill. It records the result for HEAD.

### 5. Final gate

```
python3 <mrg> -C <repo> gate --base <store-tag>
```

`gate` = all static checks + a passing upgrade test recorded for the current
commit for every app platform. Any commit after the upgrade test invalidates
it - that is intentional, since the tested build is no longer what ships.

### 6. Archive what you ship

After the store build is produced, archive it with provenance so the next
release has a real "old version" to upgrade from, and so a future incident
can be investigated in minutes:

```
python3 <mrg> -C <repo> archive --platform ios --version 2.4.0 --build 12 \
    --artifact build/MyApp.ipa --artifact build/sim/MyApp.app
```

The manifest records commit, dirty state, lockfile contents and the package
versions Xcode actually resolved on this machine. For iOS also archive a
simulator `.app` built from the same commit - it is what the next upgrade test
installs as "old version".

## Report format

End with a report in the user's language, short enough to paste into the
release ticket:

```
## Release guard: <app> <version> (<platform>) - GO | NO-GO
Base (store): <tag> @ <sha>   Head: <sha>
Blockers: <n open> / <n acknowledged>
- [rule] <one line> -> <fixed in abc123 | acked by x: reason | OPEN: what is needed>
Warnings reviewed: <n>  (list only those that matter)
Upgrade test: ios <from> -> <to> PASS (simulator, <who>) | MISSING
Next step: <the single most important action if NO-GO>
```

Never report GO unless `gate` exited 0 in this session on the current HEAD.
If you could not run something (no simulator, no old build), say so plainly -
the verdict is NO-GO until it is done.
