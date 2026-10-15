#!/usr/bin/env bash
# Installs Dokkan from Google Play through Aurora Store's UI on the redroid of the
# all-in-one stack, like a user does on the Screen tab: anonymous login, then Install.
# Expects Aurora Store to be installed already (the farmer's "Install Aurora Store").
set -u

PKG=com.bandainamcogames.dbzdokkanww
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p shots

adb() { docker exec -u farmer dokkan-farmer adb -s redroid:5555 "$@"; }
dump() {
  adb shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1
  adb exec-out cat /sdcard/ui.xml > ui.xml 2>/dev/null
}
# Tap the first element whose text fully matches the regex (from the last dump).
tap() {
  local xy
  xy=$(python3 "$HERE/ui_find.py" ui.xml "$1") || return 1
  echo "  tap ${1} at ${xy}"
  # shellcheck disable=SC2086 # "x y"
  adb shell input tap $xy
}
labels() { python3 "$HERE/ui_find.py" ui.xml --list | head -n 25 | sed 's/^/    /'; }
installed() { adb shell pm path "$PKG" 2>/dev/null | grep -q "package:"; }
shot() {
  local id
  id=$(curl -sS -X POST "$BASE/api/screen/capture" | jq -r .frame_id)
  curl -sS "$BASE/api/screen.jpg?frame=$id&w=360" -o "shots/$1.jpg"
}

# What a user grants once in Android's settings, done with adb here.
adb shell appops set com.aurora.store REQUEST_INSTALL_PACKAGES allow
adb shell pm grant com.aurora.store android.permission.POST_NOTIFICATIONS 2>/dev/null || true
for setting in window_animation_scale transition_animation_scale animator_duration_scale; do
  adb shell settings put global "$setting" 0
done

echo "== Aurora Store onboarding and anonymous login"
logged_in=""
for i in $(seq 1 40); do
  dump
  echo "step $i:"; labels
  if tap '(?i)anonymous'; then
    logged_in=1
    break
  fi
  if ! tap '(?i)(skip|next|finish|done|continue|accept|agree|ok|allow|got it)'; then
    # Not on an Aurora screen (e.g. a settings page opened): bring Aurora back.
    grep -q 'package="com.aurora.store"' ui.xml 2>/dev/null ||
      adb shell monkey --pct-syskeys 0 -p com.aurora.store -c android.intent.category.LAUNCHER 1 >/dev/null
  fi
  sleep 3
done
shot aurora-login
[ -n "$logged_in" ] || { echo "No anonymous login button found"; exit 1; }
sleep 15

echo "== Dokkan's page in Aurora Store"
adb shell am start -a android.intent.action.VIEW -d "market://details?id=$PKG" >/dev/null
sleep 10
for i in $(seq 1 120); do
  if installed; then
    echo "Installed after step $i"
    break
  fi
  dump
  if [ $((i % 5)) = 1 ]; then echo "step $i:"; labels; fi
  tap '(?i)install' || tap '(?i)(allow|ok|continue|update)' || true
  [ "$i" = 3 ] && shot aurora-details
  sleep 5
done
shot aurora-after-install
installed || { echo "Dokkan was not installed"; dump; labels; exit 1; }
adb shell "dumpsys package $PKG | grep -E 'primaryCpuAbi|versionName|versionCode' | head -n 4"
