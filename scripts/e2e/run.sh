#!/usr/bin/env bash
# End-to-end test of the released Senel APK on the CI emulator.
# Called by .github/workflows/senel-e2e.yml from inside android-emulator-runner.
# Steps T1-T8 are described in the workflow file. Results go to $E2E_OUT/results.json.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export E2E_OUT="${E2E_OUT:-$PWD/e2e-out}"
export E2E_STATE="${E2E_STATE:-${RUNNER_TEMP:-/tmp}/e2e-state.json}"
mkdir -p "$E2E_OUT/ui"
rm -f "$E2E_STATE"

PKG=me.capcom.smsgateway
APK_URL=https://github.com/MikkelGodiksen1/android-sms-gateway/releases/download/senel-v1.77.1-senel.1/sms-gateway-senel.apk
APK="${RUNNER_TEMP:-/tmp}/sms-gateway-senel.apk"
T4_TIMEOUT=180
DOZE_TIMEOUT=600
T7_TIMEOUT=600
T8_TIMEOUT=180

E2E() { python3 "$HERE/e2e.py" "$@"; }
log() { echo "[$(date -u +%H:%M:%S)] $*"; }
section() { echo; echo "=================== $* ==================="; }

LOGCAT_PID=""
start_logcat() {
  adb logcat -v threadtime -b main,system,crash >> "$E2E_OUT/logcat-$1.txt" 2>&1 &
  LOGCAT_PID=$!
}
stop_logcat() {
  if [ -n "$LOGCAT_PID" ]; then kill "$LOGCAT_PID" 2>/dev/null; fi
  LOGCAT_PID=""
}
trap stop_logcat EXIT

grant_perms() {
  for p in SEND_SMS RECEIVE_SMS READ_SMS RECEIVE_MMS RECEIVE_WAP_PUSH READ_PHONE_STATE READ_PHONE_NUMBERS POST_NOTIFICATIONS; do
    out=$(adb shell pm grant "$PKG" "android.permission.$p" 2>&1 | tr -d '\r')
    log "pm grant $p: ${out:-ok}"
  done
}

app_pid() { adb shell pidof "$PKG" 2>/dev/null | tr -d '\r'; }

wait_boot() {
  adb wait-for-device
  for _ in $(seq 1 120); do
    if [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; then return 0; fi
    sleep 5
  done
  return 1
}

# Saves service, notification and SSE state for a test; records the key facts.
capture_service_state() {  # $1=test $2=marker
  local t="$1" m="$2"
  adb shell dumpsys activity services "$PKG" > "$E2E_OUT/dumpsys-services-$t.txt" 2>&1
  adb shell dumpsys notification --noredact > "$E2E_OUT/dumpsys-notification-$t.txt" 2>&1
  E2E logcat-grep --marker "$m" --pattern "SSEManager|SSEForegroundService" --limit 60 > "$E2E_OUT/sse-$t.txt"
  local fgs notif listening sse_conn sse_err
  fgs=$(grep -c "SSEForegroundService" "$E2E_OUT/dumpsys-services-$t.txt" || true)
  notif=$(grep -c "pkg=$PKG" "$E2E_OUT/dumpsys-notification-$t.txt" || true)
  listening=$(grep -c "Listening to server events" "$E2E_OUT/dumpsys-notification-$t.txt" || true)
  sse_conn=$(grep -c "SSE connected" "$E2E_OUT/sse-$t.txt" || true)
  sse_err=$(grep -cE "SSE error|Reconnecting|connection closed" "$E2E_OUT/sse-$t.txt" || true)
  E2E record "$t" --set "sse_service_in_dumpsys=$fgs" --set "notification_records=$notif" \
    --set "notification_listening_text=$listening" --set "sse_connected_lines=$sse_conn" \
    --set "sse_error_or_reconnect_lines=$sse_err" --set-file "sse_log=$E2E_OUT/sse-$t.txt"
  log "$t: SSE service in dumpsys=$fgs notifications=$notif listening-text=$listening sse-connected=$sse_conn sse-errors=$sse_err"
}

adb shell settings put global window_animation_scale 0 >/dev/null 2>&1
adb shell svc power stayon false >/dev/null 2>&1
start_logcat 1-boot

# ------------------------------------------------------------------ T1
section "T1 install and launch"
E2E mark T1
if ! curl -fsSL -o "$APK" "$APK_URL"; then
  E2E record T1 --set status=fail --set error=apk_download_failed
fi
sha256sum "$APK" | tee "$E2E_OUT/apk-sha256.txt"
BT=$(ls -d "${ANDROID_HOME:-/usr/local/lib/android/sdk}"/build-tools/*/ 2>/dev/null | sort -V | tail -1)
if [ -n "$BT" ] && [ -x "$BT/aapt2" ]; then
  "$BT/aapt2" dump permissions "$APK" > "$E2E_OUT/apk-permissions.txt" 2>&1
  "$BT/aapt2" dump badging "$APK" 2>/dev/null | head -3 >> "$E2E_OUT/apk-permissions.txt"
fi
install_out=$(adb install "$APK" 2>&1 | tr -d '\r' | tail -1)
log "install: $install_out"
grant_perms
adb shell dumpsys package "$PKG" | grep -E "granted=|versionName" > "$E2E_OUT/permissions-t1.txt" 2>&1
adb shell am start -W -n "$PKG/.MainActivity" | tr -d '\r'
sleep 15
pid=$(app_pid)
E2E logcat-grep --pattern "AndroidRuntime: Process: me\.capcom\.smsgateway" > "$E2E_OUT/crash-t1.txt"
E2E logcat-grep --pattern "[Ff]irebase|FirebaseApp|FirebaseInstallations|FIS_AUTH|GoogleApiManager" --limit 40 > "$E2E_OUT/firebase-lines.txt"
E2E snap t1-launched
crashes=$(grep -c . "$E2E_OUT/crash-t1.txt" || true)
t1=fail; if [ -n "$pid" ] && [ "$crashes" = "0" ] && [[ "$install_out" == *Success* ]]; then t1=pass; fi
E2E record T1 --set "status=$t1" --set "install=$install_out" --set "pid=$pid" --set "fatal_exceptions=$crashes" \
  --set-file "firebase_log=$E2E_OUT/firebase-lines.txt" --set-file "crash_log=$E2E_OUT/crash-t1.txt"
log "T1: $t1 (pid=$pid crashes=$crashes)"

# ------------------------------------------------------------------ T2
section "T2 cloud registration over SSE"
E2E mark T2
E2E snap t2-home
cloud=$(E2E checked t2-cloud-switch --id switchUseRemoteServer --want true)
log "cloud switch checked: $cloud"
E2E tap t2-start-service --id buttonStart
E2E tap t2-signup-continue --id buttonContinue --timeout 30
E2E read-registration --prefix t2 --store --timeout 120
dev_out=$(E2E devices --label t2 --test T2 --expect-key t2_device_id_ui)
log "T2 devices: $dev_out"
sse_line=$(E2E logcat-wait --marker T2 --pattern "SSE connected" --timeout 60 | head -1)
sleep 5
capture_service_state T2 T2
E2E snap t2-registered
age=$(echo "$dev_out" | awk '/^FOUND/{print $2}')
t2=fail
if [ -n "$age" ] && [ -n "$sse_line" ] && python3 -c "import sys; sys.exit(0 if float('$age') < 300 else 1)" 2>/dev/null; then t2=pass; fi
E2E record T2 --set "status=$t2" --set "cloud_switch=$cloud" --set "sse_connected_line=$sse_line"
log "T2: $t2"

if [ -z "$(E2E state-get username)" ]; then
  log "No credentials from T2; API-based tests cannot run."
fi

# ------------------------------------------------------------------ T3
section "T3 register webhooks"
E2E mark T3
wh_url=$(E2E wh-create)
log "webhook.site URL: $wh_url"
E2E webhooks-register
sleep 20
E2E logcat-grep --marker T3 --pattern "[Ww]ebhook" --limit 30 > "$E2E_OUT/webhooks-sync-t3.txt"
E2E record T3 --set "webhook_site_url=$wh_url" --set-file "device_log=$E2E_OUT/webhooks-sync-t3.txt"

# ------------------------------------------------------------------ T4
section "T4 awake send"
E2E mark T4
E2E measure-send --test T4 --timeout "$T4_TIMEOUT"
capture_service_state T4 T4

# ------------------------------------------------------------------ T5a
section "T5a Doze send, app allow-listed"
E2E mark T5a
E2E measure-send --test T5a --timeout "$DOZE_TIMEOUT" --doze whitelist
capture_service_state T5a T5a
sleep 30

# ------------------------------------------------------------------ T5b
section "T5b Doze send, app NOT allow-listed"
E2E mark T5b
E2E measure-send --test T5b --timeout "$DOZE_TIMEOUT" --doze nowhitelist
capture_service_state T5b T5b
sleep 30

# ------------------------------------------------------------------ T6
section "T6 Doze inbound SMS, app allow-listed"
E2E mark T6
E2E measure-inbound --test T6 --timeout "$DOZE_TIMEOUT" --doze whitelist
capture_service_state T6 T6
E2E snap t6-after

# ------------------------------------------------------------------ T7
# Two variants: "Start on boot" off (the default) and on.
reboot_and_measure() {  # $1=test $2=logcat suffix
  local t="$1"
  E2E mark "$t"
  sleep 2
  stop_logcat
  adb reboot
  if ! wait_boot; then
    E2E record "$t" --set status=fail --set error=boot_timeout
    return
  fi
  start_logcat "$2"
  log "$t: boot completed, not opening the app, waiting 60s"
  sleep 60
  local p
  p=$(app_pid)
  E2E snap "${t,,}-after-boot"
  capture_service_state "$t" "$t"
  E2E record "$t" --set "pid_after_boot=$p"
  E2E measure-send --test "$t" --timeout "$T7_TIMEOUT"
  capture_service_state "$t" "$t"
}

section "T7off reboot, Start on boot OFF (default)"
auto=$(E2E checked t7off-autostart --id switchAutostart)
log "autostart switch before T7off: $auto"
E2E record T7off --set "autostart_switch=$auto"
adb shell input keyevent 3
reboot_and_measure T7off 2-after-reboot-autostart-off

section "T7on reboot, Start on boot ON"
adb shell wm dismiss-keyguard
adb shell am start -W -n "$PKG/.MainActivity" | tr -d '\r'
sleep 5
auto=$(E2E checked t7on-autostart --id switchAutostart --want true)
log "autostart switch before T7on: $auto"
E2E snap t7on-autostart-set
E2E record T7on --set "autostart_switch=$auto"
adb shell input keyevent 3
sleep 3
reboot_and_measure T7on 3-after-reboot-autostart-on

# ------------------------------------------------------------------ T8
section "T8 reinstall and sign in to the existing account"
adb shell wm dismiss-keyguard
E2E mark T8
adb uninstall "$PKG" | tr -d '\r'
install_out=$(adb install "$APK" 2>&1 | tr -d '\r' | tail -1)
log "reinstall: $install_out"
grant_perms
adb shell am start -W -n "$PKG/.MainActivity" | tr -d '\r'
sleep 10
E2E snap t8-launched
cloud=$(E2E checked t8-cloud-switch --id switchUseRemoteServer --want true)
E2E tap t8-start-service --id buttonStart
E2E tap t8-signin-tab --text "Sign In" --timeout 30
E2E type t8-username --id editUsername --state-key username
E2E type t8-password --id editPassword --state-key password
adb shell input keyevent 4   # hide the keyboard; the dialog itself is not cancelable
sleep 1
E2E tap t8-continue --id buttonContinue
E2E read-registration --prefix t8 --timeout 120
new_dev=$(E2E new-device --old-key device_id --new-key device_id_t8 --timeout 60)
log "T8 devices: $new_dev"
E2E logcat-wait --marker T8 --pattern "SSE connected" --timeout 60 > /dev/null
capture_service_state T8 T8
E2E record T8 --set "install=$install_out" --set "cloud_switch=$cloud" --set "device_lookup=$new_dev"
E2E measure-send --test T8 --device-key device_id_t8 --timeout "$T8_TIMEOUT"
E2E snap t8-after-send

# ------------------------------------------------------------------ cleanup
section "Cleanup"
E2E webhooks-delete
E2E logcat-grep --pattern "AndroidRuntime: Process: me\.capcom\.smsgateway" > "$E2E_OUT/crash-all.txt"
E2E logcat-grep --pattern "AndroidRuntime" --limit 80 > "$E2E_OUT/androidruntime-all.txt"
E2E record overall --set "fatal_exceptions_total=$(grep -c . "$E2E_OUT/crash-all.txt" || true)"
adb shell dumpsys deviceidle > "$E2E_OUT/dumpsys-deviceidle-end.txt" 2>&1
stop_logcat

section "Results"
cat "$E2E_OUT/results.json"
E2E summary
exit 0
