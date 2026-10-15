#!/usr/bin/env bash
# Demo-day launcher for the two-laptop call (Mac = peer A, Linux = peer B).
#
#   ./demo.sh mac   <LINUX_IP> [off|fixed|adaptive] [webcam|file]
#   ./demo.sh linux <MAC_IP>   [off|fixed|adaptive] [webcam|file]
#   ./demo.sh mac-split   <LINUX_IP>     two calls at once: FEC off and adaptive
#   ./demo.sh linux-split <MAC_IP>
#   sudo ./demo.sh dial <iface> [secs]   (Linux) step loss 0->60% on Mac-bound ports
#   ./demo.sh local [off|fixed|adaptive] [loss%]   offline fallback, one machine
#
# Ports: Linux 5000/5001 (split: also 5010/5011), Mac 6000/6001 (split: also
# 6010/6011).  Loss is injected on Linux egress towards the Mac, so the Mac
# shows the damaged video and serves the dashboard: http://127.0.0.1:8080
# (split: 8080 = adaptive, 8081 = off).  Set DASH_HOST=0.0.0.0 to view it
# from another device.
set -euo pipefail
cd "$(dirname "$0")"

usage() { sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
[[ $# -ge 1 ]] || usage

py=python3
[[ -x venv/bin/python ]] && py=venv/bin/python
dash_host=${DASH_HOST:-127.0.0.1}
role=$1; shift

need_ip() { [[ $# -ge 1 && -n $1 ]] || { echo "error: peer IP required" >&2; usage; }; }

pids=()
cleanup() { for p in "${pids[@]:-}"; do [[ -n $p ]] && kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM

case $role in
  mac)
    need_ip "$@"; ip=$1 fec=${2:-adaptive} mode=${3:-webcam}
    exec $py peer.py --name "Mac (from Linux) fec=$fec" --mode "$mode" --fec "$fec" \
      --listen-port 6000 --peer-ip "$ip" --peer-port 5000 \
      --dashboard --dashboard-host "$dash_host" --dashboard-port 8080
    ;;
  linux)
    need_ip "$@"; ip=$1 fec=${2:-adaptive} mode=${3:-webcam}
    exec $py peer.py --name "Linux (from Mac) fec=$fec" --mode "$mode" --fec "$fec" \
      --listen-port 5000 --peer-ip "$ip" --peer-port 6000
    ;;
  mac-split)
    # Only one process can own the camera, so both calls replay the sample
    # clip (which also gives PSNR/SSIM ground truth).
    need_ip "$@"; ip=$1
    $py peer.py --name "FEC OFF" --mode file --fec off \
      --listen-port 6010 --peer-ip "$ip" --peer-port 5010 \
      --dashboard --dashboard-host "$dash_host" --dashboard-port 8081 &
    pids+=($!)
    $py peer.py --name "FEC ADAPTIVE" --mode file --fec adaptive \
      --listen-port 6000 --peer-ip "$ip" --peer-port 5000 \
      --dashboard --dashboard-host "$dash_host" --dashboard-port 8080
    ;;
  linux-split)
    need_ip "$@"; ip=$1
    $py peer.py --name "FEC OFF" --mode file --fec off --no-window --quiet \
      --listen-port 5010 --peer-ip "$ip" --peer-port 6010 &
    pids+=($!)
    $py peer.py --name "FEC ADAPTIVE" --mode file --fec adaptive --no-window \
      --listen-port 5000 --peer-ip "$ip" --peer-port 6000
    ;;
  dial)
    [[ $# -ge 1 ]] || usage
    iface=$1 hold=${2:-}
    ports=6000,6001,6010,6011
    ./impair.sh add "$iface" --loss 0 --ports "$ports"
    for loss in 5 10 15 20 25 30 40 50 60; do
      if [[ -n $hold ]]; then sleep "$hold"; else read -rp "Enter -> ${loss}% loss " _; fi
      ./impair.sh change "$iface" --loss "$loss" --ports "$ports"
    done
    read -rp "Enter -> clear impairment " _
    ./impair.sh clear "$iface"
    ;;
  local)
    # No network, no root: both peers on loopback, loss dropped in-app.
    fec=${1:-adaptive} loss=${2:-20}
    $py peer.py --name "Peer B" --mode file --fec "$fec" --sim-loss "$loss" \
      --listen-port 6000 --peer-port 5000 --no-window --quiet &
    pids+=($!)
    $py peer.py --name "Local fec=$fec loss=$loss%" --mode file --fec "$fec" \
      --sim-loss "$loss" --listen-port 5000 --peer-port 6000 \
      --dashboard --dashboard-host "$dash_host" --dashboard-port 8080
    ;;
  *) usage ;;
esac
