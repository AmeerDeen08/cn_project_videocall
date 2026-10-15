#!/usr/bin/env bash
# Inject packet loss / delay / jitter with tc netem (Linux only, needs root).
#
#   sudo ./impair.sh add    <iface> [--loss PCT] [--delay MS] [--jitter MS] [--ports P1,P2,...]
#   sudo ./impair.sh change <iface> [same options]
#   sudo ./impair.sh clear  <iface>
#   ./impair.sh show <iface>
#
# On a real interface (e.g. wlo1) netem is applied to all egress traffic.
# On the loopback interface it is scoped to the call's UDP ports, so the web
# dashboard, DNS and everything else on 'lo' stay unaffected.  Default ports
# are the two peers' media + control ports: 5000,5001,6000,6001.
#
# Examples:
#   sudo ./impair.sh add lo --loss 20
#   sudo ./impair.sh change lo --loss 30 --delay 40 --jitter 10
#   sudo ./impair.sh add wlo1 --loss 15
#   sudo ./impair.sh clear lo
set -euo pipefail

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

[[ $# -ge 2 ]] || usage
cmd=$1 iface=$2; shift 2

loss=0 delay=0 jitter=0 ports=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --loss)   loss=$2; shift 2 ;;
    --delay)  delay=$2; shift 2 ;;
    --jitter) jitter=$2; shift 2 ;;
    --ports)  ports=$2; shift 2 ;;
    *) usage ;;
  esac
done
if [[ $iface == lo && -z $ports ]]; then
  ports="5000,5001,6000,6001"
fi

netem_args() {
  local a=(loss "${loss}%")
  if [[ $delay != 0 || $jitter != 0 ]]; then
    a+=(delay "${delay}ms")
    [[ $jitter != 0 ]] && a+=("${jitter}ms" distribution normal)
  fi
  echo "${a[@]}"
}

clear_qdisc() { tc qdisc del dev "$iface" root 2>/dev/null || true; }

apply() {
  local verb=$1
  if [[ -n $ports ]]; then
    if [[ $verb == change ]] && tc qdisc show dev "$iface" | grep -q "qdisc netem 30:"; then
      # shellcheck disable=SC2046
      tc qdisc change dev "$iface" parent 1:3 handle 30: netem $(netem_args)
    else
      clear_qdisc
      # prio sends everything to bands 1:1/1:2 by default; only matched
      # ports are steered into band 1:3, which carries the netem impairment.
      tc qdisc add dev "$iface" root handle 1: prio
      # shellcheck disable=SC2046
      tc qdisc add dev "$iface" parent 1:3 handle 30: netem $(netem_args)
      IFS=, read -ra plist <<< "$ports"
      for p in "${plist[@]}"; do
        tc filter add dev "$iface" parent 1:0 protocol ip prio 1 u32 \
          match ip protocol 17 0xff match ip dport "$p" 0xffff flowid 1:3
      done
    fi
  else
    if [[ $verb == change ]] && tc qdisc show dev "$iface" | grep -q "qdisc netem"; then
      # shellcheck disable=SC2046
      tc qdisc change dev "$iface" root netem $(netem_args)
    else
      clear_qdisc
      # shellcheck disable=SC2046
      tc qdisc add dev "$iface" root netem $(netem_args)
    fi
  fi
  echo "impair: $iface -> $(netem_args)${ports:+ (UDP ports $ports)}"
}

case $cmd in
  add)    apply add ;;
  change) apply change ;;
  clear)  clear_qdisc; echo "impair: cleared $iface" ;;
  show)   tc qdisc show dev "$iface"; tc filter show dev "$iface" 2>/dev/null || true ;;
  *) usage ;;
esac
