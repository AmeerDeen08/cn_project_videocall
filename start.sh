#!/usr/bin/env bash
# One command for both laptops:   ./start.sh
#
# Detects macOS (peer A) or Linux (peer B), prints this machine's IP, asks for
# the other laptop's IP and the FEC mode, then starts the call.  Pick the SAME
# mode number on both laptops.  On Linux it then shows a loss prompt: type a
# loss % (e.g. 20) to apply it with tc netem, 0 to clear, q to quit.
set -uo pipefail
cd "$(dirname "$0")"

os=$(uname -s)
ports=6000,6001,6010,6011          # the Mac's call ports (loss goes Linux -> Mac)

if [[ $os == Darwin ]]; then
  role=mac
  my_ip=$(ipconfig getifaddr en0 2>/dev/null || true)
  [[ -n $my_ip ]] || my_ip=$(ipconfig getifaddr "$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')" 2>/dev/null || true)
else
  role=linux
  my_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
fi

echo "=============================================="
echo "  This is the $( [[ $role == mac ]] && echo "MAC (peer A)" || echo "LINUX (peer B)" )"
echo "  My IP address:  ${my_ip:-unknown}   <- type this on the other laptop"
echo "=============================================="

last=$(cat .peer_ip 2>/dev/null || true)
read -rp "Other laptop's IP${last:+ [$last]}: " peer_ip
peer_ip=${peer_ip:-$last}
[[ $peer_ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Not an IP address: '$peer_ip'"; exit 1; }
echo "$peer_ip" > .peer_ip

if [[ $role == mac ]]; then wait_flag=(-t 1); else wait_flag=(-W 1); fi
if ping -c 2 "${wait_flag[@]}" "$peer_ip" >/dev/null 2>&1; then
  echo "ping $peer_ip: OK"
else
  echo "WARNING: $peer_ip does not answer ping (same Wi-Fi? try a phone hotspot). Continuing anyway."
fi

echo
echo "Mode (choose the same number on both laptops):"
echo "  1) adaptive FEC, webcam"
echo "  2) FEC off,      webcam"
echo "  3) side by side: FEC off + adaptive (sample clip)"
echo "  4) adaptive FEC, sample clip (shows PSNR)"
echo "  5) FEC off,      sample clip"
read -rp "Choice [1]: " choice
case ${choice:-1} in
  1) args=("$role" "$peer_ip" adaptive webcam) ;;
  2) args=("$role" "$peer_ip" off webcam) ;;
  3) args=("$role-split" "$peer_ip") ;;
  4) args=("$role" "$peer_ip" adaptive file) ;;
  5) args=("$role" "$peer_ip" off file) ;;
  *) echo "Invalid choice"; exit 1 ;;
esac

# ------------------------------------------------------------------ Mac
if [[ $role == mac ]]; then
  echo
  echo "Starting call... dashboard opens in the browser. Press q in the video window to stop."
  ( sleep 4; open http://127.0.0.1:8080
    [[ $choice == 3 ]] && open http://127.0.0.1:8081 ) >/dev/null 2>&1 &
  exec ./demo.sh "${args[@]}"
fi

# ---------------------------------------------------------------- Linux
iface=$(ip route get "$peer_ip" 2>/dev/null | grep -oP 'dev \K\S+')
[[ -n $iface ]] || { echo "Cannot find the interface towards $peer_ip"; exit 1; }
echo "Interface towards the Mac: $iface"
echo "sudo is needed to inject packet loss:"
sudo -v || exit 1

mkdir -p logs
log=logs/linux_call.log
./demo.sh "${args[@]}" > "$log" 2>&1 &
peer_pid=$!

finish() {
  kill "$peer_pid" 2>/dev/null; pkill -f "peer.py --name" 2>/dev/null
  sudo ./impair.sh clear "$iface" >/dev/null 2>&1
  echo "Call stopped, loss cleared."
}
trap finish EXIT

sleep 3
echo
echo "Call running (log: $log)."
echo "Type a loss % and Enter: 0 5 10 20 30 40 50 60 | s = status | q = quit"
while kill -0 "$peer_pid" 2>/dev/null; do
  read -rp "loss> " x || break
  case $x in
    q|Q) break ;;
    s|S|"") ;;
    0) sudo ./impair.sh clear "$iface" ;;
    *[!0-9]*) echo "type a number, s or q"; continue ;;
    *) sudo ./impair.sh add "$iface" --loss "$x" --ports "$ports" ;;
  esac
  sleep 0.3
  grep 'adaptive:' "$log" | tail -1
  grep 'rx\[' "$log" | tail -1
done
