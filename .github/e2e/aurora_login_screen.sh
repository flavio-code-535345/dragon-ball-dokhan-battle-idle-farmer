#!/usr/bin/env bash
# Walks Aurora Store (installed by the farmer) through its welcome screens on the
# redroid of the all-in-one stack, the way a user does on the Screen tab, and checks
# that its login screen ("Google" / "Anonymous") appears.
#
# It does not log in: Aurora's anonymous accounts are refused from datacenter IPs such
# as GitHub's runners ("Access denied! Are you using VPN or Tor?"). At home it works.
set -u

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
has() { python3 "$HERE/ui_find.py" ui.xml "$1" >/dev/null; }
labels() { python3 "$HERE/ui_find.py" ui.xml --list | head -n 25 | sed 's/^/    /'; }
shot() {
  local id
  id=$(curl -sS -X POST "$BASE/api/screen/capture" | jq -r .frame_id)
  curl -sS "$BASE/api/screen.jpg?frame=$id&w=360" -o "shots/$1.jpg"
}

for i in $(seq 1 20); do
  dump
  echo "step $i:"; labels
  if has '(?i)anonymous' && has '(?i)google'; then
    shot aurora-login
    echo "Aurora Store's login screen is up"
    exit 0
  fi
  if ! tap '(?i)(skip|next|finish|done|continue|accept|agree|ok|allow|got it)'; then
    # Not on an Aurora screen (e.g. a settings page opened): bring Aurora back.
    grep -q 'package="com.aurora.store"' ui.xml 2>/dev/null ||
      adb shell monkey --pct-syskeys 0 -p com.aurora.store -c android.intent.category.LAUNCHER 1 >/dev/null
  fi
  sleep 3
done
shot aurora-stuck
echo "Aurora Store's login screen did not appear"
exit 1
