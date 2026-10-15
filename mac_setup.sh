#!/usr/bin/env bash
# One-time macOS setup and pre-flight check for the demo.
#
#   ./mac_setup.sh            create venv (if missing), install, run checks
#   ./mac_setup.sh check      checks only
#
# Checks: Python deps and FEC backend, a 6 s loopback call under 20% in-app
# loss, the webcam, the application firewall, and this Mac's IP address.
# Run it from Terminal.app (or whichever app you will demo from): macOS grants
# camera access per app, and the first webcam use triggers the permission prompt.
set -uo pipefail
cd "$(dirname "$0")"

ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; }

if [[ ${1:-} != check ]]; then
  if [[ ! -x venv/bin/python ]]; then
    # zfec publishes macOS arm64 wheels up to CPython 3.13; newer Pythons
    # must compile it, which fails with some Xcode/CLT combinations.
    py=""
    for c in python3.13 python3.12 python3.11 python3; do
      command -v "$c" >/dev/null && { py=$c; break; }
    done
    [[ -n $py ]] || { bad "no python3 found (brew install python@3.13)"; exit 1; }
    echo "creating venv with $($py --version)"
    $py -m venv venv
  fi
  venv/bin/pip install -q --upgrade pip
  if ! venv/bin/pip install -q -r requirements.txt; then
    warn "full install failed (usually zfec); installing without it, reedsolo fallback will be used"
    grep -v '^zfec' requirements.txt | venv/bin/pip install -q -r /dev/stdin
  fi
fi

py=venv/bin/python
echo "== Python packages"
if out=$($py -c "import cv2, numpy, reedsolo, skimage, matplotlib, flask, flask_sock, fec; print(cv2.__version__, fec.BACKEND)" 2>&1); then
  set -- $out
  ok "opencv $1, FEC backend: $2"
  [[ $2 == zfec ]] || warn "using pure-Python reedsolo (slower, but works)"
else
  bad "$out"; exit 1
fi

echo "== Loopback call (adaptive FEC, 20% in-app loss, 6 s)"
$py peer.py --mode file --fec adaptive --sim-loss 20 --listen-port 6100 \
  --peer-port 6200 --no-window --duration 6 --quiet --metrics-csv /dev/null &
$py peer.py --mode file --fec adaptive --sim-loss 20 --listen-port 6200 \
  --peer-port 6100 --no-window --duration 6 --metrics-csv /dev/null 2>&1 \
  | grep 'rx\[' | tail -1 | sed 's/^/        /'
wait

echo "== Webcam"
cam=$($py - <<'EOF' 2>/dev/null
import time, cv2
for idx in (0, 1):
    cap = cv2.VideoCapture(idx)
    t = time.time()
    good = False
    while cap.isOpened() and time.time() - t < 3:
        good, _ = cap.read()
        if good:
            break
    cap.release()
    print(f"{idx}:{int(good)}")
EOF
)
if [[ $cam == *"0:1"* ]]; then
  ok "camera 0 works"
else
  bad "camera 0 not available. Grant access in System Settings > Privacy & Security > Camera"
  echo "        for the app running this shell (Terminal / VS Code), restart that app, rerun."
  echo "        Until then use 'file' mode instead of 'webcam'."
fi
[[ $cam == *"1:1"* ]] && warn "camera 1 also exists (likely iPhone Continuity Camera); pass --camera 0/1 to choose"

echo "== Firewall"
fw=/usr/libexec/ApplicationFirewall/socketfilterfw
real=$($py -c "import os, sys; print(os.path.realpath(sys.executable))")
if $fw --getglobalstate | grep -q disabled; then
  ok "application firewall is off"
elif $fw --getappblocked "$real" 2>/dev/null | grep -q "permitted"; then
  ok "firewall allows $real"
else
  warn "firewall may block incoming UDP for $real; to allow it:"
  echo "        sudo $fw --add '$real' && sudo $fw --unblockapp '$real'"
fi

echo "== Network"
for i in en0 en1 en2 en3 en4 en5 en6; do
  a=$(ipconfig getifaddr "$i" 2>/dev/null) && ok "$i: $a"
done
echo "Give the Linux machine this Mac's IP; it runs:  ./demo.sh linux <MAC_IP>"
