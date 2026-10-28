# Android build guard

## 1. No dynamic versions

`1.+`, `latest.release`, `[1.0,2.0)` resolve differently over time. Replace
with fixed versions, ideally in `gradle/libs.versions.toml`. Critical ones
(security-crypto, room, datastore, auth SDKs, tink, sqlcipher...) must be fixed.

## 2. Dependency locking

In each module that ships (usually `app/build.gradle(.kts)`), or for all
projects in the root build file:

```kotlin
// build.gradle.kts
dependencyLocking {
    lockAllConfigurations()
    lockMode.set(LockMode.STRICT)   // fail if a configuration has no lock state
}
```

```groovy
// build.gradle
dependencyLocking {
    lockAllConfigurations()
    lockMode = LockMode.STRICT
}
```

Generate and commit the lock state (only when you intend to change deps):

```sh
./gradlew :app:dependencies --write-locks
git add **/gradle.lockfile && git commit -m "chore: lock gradle dependencies"
```

From then on Gradle itself fails the build if resolution would differ from
`gradle.lockfile`. Update deliberately with `--write-locks` + review the diff.
For buildscript classpath: `buildscript { configurations.classpath { resolutionStrategy.activateDependencyLocking() } }`.

## 3. Release Guard task (hard block on release builds)

Add to `app/build.gradle.kts`:

```kotlin
// Release Guard: block release builds that don't match git.
val releaseGuard = tasks.register<Exec>("releaseGuard") {
    group = "verification"
    description = "Fails if the release would not be reproducible from git"
    workingDir = rootDir
    commandLine("sh", "-c",
        "python3 \"\$(git rev-parse --show-toplevel)/tools/release-guard/mrg.py\" check --scope build")
}
tasks.configureEach {
    if (Regex("(assemble|bundle).*Release").matches(name)) dependsOn(releaseGuard)
}
```

Groovy:

```groovy
def releaseGuard = tasks.register("releaseGuard", Exec) {
    group = "verification"
    workingDir = rootDir
    commandLine "sh", "-c", 'python3 "$(git rev-parse --show-toplevel)/tools/release-guard/mrg.py" check --scope build'
}
tasks.configureEach { t ->
    if (t.name ==~ /(assemble|bundle).*Release/) t.dependsOn(releaseGuard)
}
```

`--scope build` runs only reproducibility checks (lockfiles, pinning), so it
never blocks on a pending acknowledgement - those belong to `gate`.

## 4. Signing and identity

An in-place update requires the same `applicationId` and the same signing
certificate as the store build (Play App Signing: same upload key lineage).
Never change `applicationId`, `sharedUserId`, or the keystore for an existing
app. Keep the upload keystore backed up outside a single laptop.

## 5. Backup rules

Data encrypted with an Android Keystore key cannot be decrypted after Auto
Backup restores it to another device - a classic "crash on first launch after
restore". Exclude Keystore-encrypted prefs / files from backup in
`data_extraction_rules.xml` (API 31+) and `backup_rules.xml`:

```xml
<data-extraction-rules>
  <cloud-backup>
    <exclude domain="sharedpref" path="secure_prefs.xml"/>
  </cloud-backup>
  <device-transfer>
    <exclude domain="sharedpref" path="secure_prefs.xml"/>
  </device-transfer>
</data-extraction-rules>
```

And make the read path tolerant: catch `AEADBadTagException` /
`KeyPermanentlyInvalidatedException`, clear only that store, log an event.
