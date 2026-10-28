# iOS build guard

## 1. Pin critical Swift packages

Xcode > project > Package Dependencies > select package > Dependency Rule:
**Exact Version** = version from the store build's `Package.resolved`.
In `project.pbxproj` this becomes `kind = exactVersion; version = 1.1.0;`.

For `Package.swift` (local packages): `.package(url: "...", exact: "1.1.0")`.

CocoaPods: `pod 'KeychainAccess', '4.2.2'` (no `~>`). Git pods need `:tag` or
`:commit`, never `:branch`.

## 2. One Package.resolved

If the project has an `.xcworkspace` (CocoaPods, or several projects):
- canonical file: `App.xcworkspace/xcshareddata/swiftpm/Package.resolved` - commit it
- delete `App.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved`
- add to `.gitignore`: `*.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/`

Optional extra lock: in the archive command use
`xcodebuild -workspace App.xcworkspace -scheme App -disableAutomaticPackageResolution ...`
which fails instead of silently re-resolving.

## 3. Run Script build phase (the hard block)

In Xcode, for the **app target**:
1. Build Phases > + > New Run Script Phase. Name it `Release Guard`.
2. Drag it right after "Dependencies" (before Compile Sources) so it fails fast.
3. Uncheck "Based on dependency analysis" (it must run on every build).
4. Shell `/bin/sh`, script:

```sh
# Release Guard: fail Release builds whose resolved packages differ from git.
REPO="$(git -C "$SRCROOT" rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$REPO" ]; then echo "error: [release-guard] not a git checkout"; exit 1; fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "error: [release-guard] python3 not found (xcode-select --install)"; exit 1
fi
python3 "$REPO/tools/release-guard/mrg.py" ios-build-guard
```

5. Build Settings of the same target > **User Script Sandboxing**
   (`ENABLE_USER_SCRIPT_SANDBOXING`) = **No**. With sandboxing on, the script
   cannot read `.git` or DerivedData and will fail with "cannot locate
   SourcePackages".

Behaviour: in configurations listed in `release_configurations` (default
`Release`) mismatches are `error:` and fail the build/archive. In Debug they
are `warning:` so daily work is not blocked.

What it checks: resolved package versions in this build's
`SourcePackages/workspace-state.json` vs the committed `Package.resolved`;
uncommitted changes to `Package.resolved`; unpinned critical packages; two
disagreeing `Package.resolved`; `Pods/Manifest.lock` vs `Podfile.lock`.

Flutter / React Native: add the same phase to the `Runner` / app target in
`ios/*.xcodeproj`.

## 4. Verify the guard

```sh
# make the committed Package.resolved disagree with what Xcode resolved
sed -i '' 's/"version" : "4.1.0"/"version" : "4.1.1"/' App.xcworkspace/xcshareddata/swiftpm/Package.resolved
xcodebuild -workspace App.xcworkspace -scheme App -configuration Release \
  -destination 'generic/platform=iOS Simulator' build 2>&1 | grep release-guard
git checkout -- App.xcworkspace/xcshareddata/swiftpm/Package.resolved
```

Expected: `error: [release-guard] ...` and BUILD FAILED.
