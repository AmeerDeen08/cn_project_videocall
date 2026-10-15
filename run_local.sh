#!/usr/bin/env bash
# Start a two-peer call on this machine over loopback.
#
#   ./run_local.sh [off|fixed|adaptive] [file|webcam]
#
# Peer A (ports 5000/5001) serves the dashboard at http://127.0.0.1:8080 and
# shows what it receives from peer B (ports 6000/6001).  Inject loss on the
# call's ports with, e.g.:  sudo ./impair.sh add lo --loss 20
set -euo pipefail
cd "$(dirname "$0")"

fec=${1:-adaptive}
mode=${2:-file}
py=python3
[[ -x venv/bin/python ]] && py=venv/bin/python

$py peer.py --name "Peer B" --mode "$mode" --fec "$fec" \
  --listen-port 6000 --peer-port 5000 --quiet &
b=$!
trap 'kill $b 2>/dev/null || true' EXIT

# Only one process can own the webcam, so peer A replays the sample clip.
$py peer.py --name "Peer A" --mode file --fec "$fec" \
  --listen-port 5000 --peer-port 6000 --dashboard
