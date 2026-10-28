#!/usr/bin/env bash
# Upgrade test: install the OLD build, put it in a real user state (logged in,
# some data), install the NEW build over it WITHOUT uninstalling, relaunch and
# verify the state survived and nothing crashed.
#
#   upgrade_test.sh --platform ios|android --app-id <id> --old <path> --new <path> \
#       --seed-flow <maestro.yaml> --verify-flow <maestro.yaml> \
#       [--device <udid|serial>] [--from 2.3.0 --to 2.4.0] [-e KEY=VALUE ...] [--record]
#
# iOS:     --old/--new are simulator .app bundles (or .zip of one).
# Android: --old/--new are .apk files signed with the SAME key.
# --record writes the result for the current git HEAD via `mrg record-upgrade-test`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORM="" APP_ID="" OLD="" NEW="" SEED="" VERIFY="" DEVICE="" FROM="" TO="" RECORD=0
MAESTRO_ENV=()

die() { echo "upgrade-test: $*" >&2; exit 2; }
step() { printf '\n==> %s\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform) PLATFORM="$2"; shift 2 ;;
    --app-id) APP_ID="$2"; shift 2 ;;
    --old) OLD="$2"; shift 2 ;;
    --new) NEW="$2"; shift 2 ;;
    --seed-flow) SEED="$2"; shift 2 ;;
    --verify-flow) VERIFY="$2"; shift 2 ;;
    --device) DEVICE="$2"; shift 2 ;;
    --from) FROM="$2"; shift 2 ;;
    --to) TO="$2"; shift 2 ;;
    -e) MAESTRO_ENV+=(-e "$2"); shift 2 ;;
    --record) RECORD=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "$PLATFORM" && -n "$APP_ID" && -n "$OLD" && -n "$NEW" && -n "$SEED" && -n "$VERIFY" ]] \
  || die "missing required arguments (see --help)"
[[ -e "$OLD" ]] || die "old build not found: $OLD"
[[ -e "$NEW" ]] || die "new build not found: $NEW"
[[ -f "$SEED" && -f "$VERIFY" ]] || die "maestro flow not found"
command -v maestro >/dev/null || die "maestro is not installed (curl -Ls https://get.maestro.mobile.dev | bash)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
MAESTRO_ENV+=(-e "APP_ID=$APP_ID")
RESULT="fail"
NOTES=""

record() {
  [[ $RECORD -eq 1 ]] || return 0
  local mrg="${MRG:-}"
  if [[ -z "$mrg" ]]; then
    local top; top="$(git rev-parse --show-toplevel 2>/dev/null || true)"
    if [[ -n "$top" && -f "$top/tools/release-guard/mrg.py" ]]; then mrg="$top/tools/release-guard/mrg.py"
    else mrg="$SCRIPT_DIR/../../../tools/mrg.py"; fi
  fi
  local method; method=$([[ $PLATFORM == ios ]] && echo simulator || echo emulator)
  python3 "$mrg" record-upgrade-test --platform "$PLATFORM" --from "${FROM:-unknown}" --to "${TO:-unknown}" \
    --method "$method" --result "$RESULT" --notes "$NOTES"
}

fail() { NOTES="$*"; echo "FAIL: $*" >&2; RESULT="fail"; record; exit 1; }

run_flow() {  # $1 = label, $2 = flow
  step "maestro: $1"
  maestro --device "$DEVICE" test "${MAESTRO_ENV[@]}" "$2" || fail "maestro flow failed: $1 ($2)"
}

# ---------------------------------------------------------------- iOS ----
ios_app() {  # unzip if needed, echo path to .app
  local src="$1" name="$2"
  if [[ "$src" == *.zip ]]; then
    mkdir -p "$WORK/$name" && unzip -q "$src" -d "$WORK/$name"
    find "$WORK/$name" -maxdepth 2 -name '*.app' -type d | head -1
  elif [[ "$src" == *.ipa ]]; then
    die "$src is a device .ipa - it cannot run on a simulator. Use a simulator .app build, or follow the device/TestFlight procedure in the upgrade-test skill."
  else
    echo "$src"
  fi
}

run_ios() {
  if [[ -z "$DEVICE" ]]; then
    DEVICE="$(xcrun simctl list devices booted -j | python3 -c 'import json,sys; d=[x for v in json.load(sys.stdin)["devices"].values() for x in v]; print(d[0]["udid"] if d else "")')"
    [[ -n "$DEVICE" ]] || die "no booted simulator; boot one or pass --device"
  fi
  local old_app new_app
  old_app="$(ios_app "$OLD" old)"; new_app="$(ios_app "$NEW" new)"
  [[ -d "$old_app" && -d "$new_app" ]] || die "could not locate .app bundles"
  local old_v new_v exe
  old_v="$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$old_app/Info.plist") ($(/usr/libexec/PlistBuddy -c 'Print CFBundleVersion' "$old_app/Info.plist"))"
  new_v="$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$new_app/Info.plist") ($(/usr/libexec/PlistBuddy -c 'Print CFBundleVersion' "$new_app/Info.plist"))"
  exe="$(/usr/libexec/PlistBuddy -c 'Print CFBundleExecutable' "$new_app/Info.plist")"
  for app in "$old_app" "$new_app"; do
    local bid; bid="$(/usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' "$app/Info.plist")"
    [[ "$bid" == "$APP_ID" ]] || fail "$app has bundle id $bid, expected $APP_ID - an upgrade requires the same id"
  done
  FROM="${FROM:-$old_v}"; TO="${TO:-$new_v}"
  echo "simulator $DEVICE: $old_v -> $new_v"

  step "clean install OLD $old_v"
  xcrun simctl uninstall "$DEVICE" "$APP_ID" >/dev/null 2>&1 || true
  xcrun simctl keychain "$DEVICE" reset >/dev/null 2>&1 || true
  xcrun simctl install "$DEVICE" "$old_app"
  run_flow "seed state on OLD" "$SEED"
  xcrun simctl terminate "$DEVICE" "$APP_ID" >/dev/null 2>&1 || true

  local marker="$WORK/marker"; touch "$marker"
  step "upgrade in place to NEW $new_v (no uninstall)"
  xcrun simctl install "$DEVICE" "$new_app"
  run_flow "verify state after upgrade" "$VERIFY"

  step "check for crash reports"
  local crashes
  crashes="$(find "$HOME/Library/Logs/DiagnosticReports" -newer "$marker" \( -name "${exe}*.ips" -o -name "${exe}*.crash" \) 2>/dev/null || true)"
  [[ -z "$crashes" ]] || fail "crash report(s) after upgrade: $crashes"
}

# ------------------------------------------------------------ Android ----
cert_digest() {
  local apksigner
  apksigner="$(command -v apksigner || ls -1 "${ANDROID_HOME:-$HOME/Library/Android/sdk}"/build-tools/*/apksigner 2>/dev/null | tail -1 || true)"
  [[ -n "$apksigner" ]] || { echo ""; return; }
  "$apksigner" verify --print-certs "$1" 2>/dev/null | grep -m1 'SHA-256 digest' | awk '{print $NF}'
}

run_android() {
  [[ "$OLD" == *.apk && "$NEW" == *.apk ]] \
    || die "Android needs .apk files. For an .aab: bundletool build-apks --mode=universal --bundle app.aab --output app.apks --ks <upload keystore> ... then unzip universal.apk"
  command -v adb >/dev/null || die "adb not found"
  if [[ -z "$DEVICE" ]]; then
    DEVICE="$(adb devices | awk 'NR>1 && $2=="device"{print $1; exit}')"
    [[ -n "$DEVICE" ]] || die "no device/emulator connected; start one or pass --device"
  fi
  local a b; a="$(cert_digest "$OLD")"; b="$(cert_digest "$NEW")"
  if [[ -n "$a" && -n "$b" && "$a" != "$b" ]]; then
    fail "old and new APKs are signed with different certificates ($a vs $b); Android will refuse the in-place update"
  fi
  [[ -n "$a" ]] || echo "note: apksigner not found, skipping signature comparison"
  FROM="${FROM:-$(basename "$OLD")}"; TO="${TO:-$(basename "$NEW")}"
  echo "device $DEVICE: $FROM -> $TO"

  step "clean install OLD"
  adb -s "$DEVICE" uninstall "$APP_ID" >/dev/null 2>&1 || true
  adb -s "$DEVICE" install "$OLD" >/dev/null
  run_flow "seed state on OLD" "$SEED"
  adb -s "$DEVICE" shell am force-stop "$APP_ID"

  step "upgrade in place to NEW (adb install -r, no uninstall)"
  local out
  out="$(adb -s "$DEVICE" install -r "$NEW" 2>&1)" || fail "in-place install failed: $out"
  adb -s "$DEVICE" logcat -c || true
  run_flow "verify state after upgrade" "$VERIFY"

  step "check for crashes"
  local crash
  crash="$(adb -s "$DEVICE" logcat -b crash -d 2>/dev/null | grep -A20 "$APP_ID" || true)"
  [[ -z "$crash" ]] || fail "crash after upgrade:\n$crash"
}

case "$PLATFORM" in
  ios) run_ios ;;
  android) run_android ;;
  *) die "--platform must be ios or android" ;;
esac

RESULT="pass"
NOTES="maestro seed+verify ok"
echo
echo "PASS: $APP_ID upgraded $FROM -> $TO with state intact"
record
