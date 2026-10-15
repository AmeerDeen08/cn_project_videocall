# Demo runbook: two-laptop "unbreakable call"

**Mac** = peer A (shows the damaged video and the dashboard).
**Linux** = peer B (injects packet loss with `tc netem`).

Packet loss is added on the Linux laptop, on traffic it sends to the Mac's
call ports. The Mac's window and dashboard show what that loss does. The
Mac's loss reports go back to Linux over a clean path, so Linux's adaptive
controller can respond. macOS has no `tc`, and doesn't need it.

| | Linux | Mac |
|---|---|---|
| Media / control ports | 5000 / 5001 | 6000 / 6001 |
| Split-view second call | 5010 / 5011 | 6010 / 6011 |
| Dashboard | — | http://127.0.0.1:8080 (split: 8080 adaptive, 8081 off) |

All commands are run from the repo root, `cn_project_videocall/`.

---

## 0. Before the examiner arrives (10 min)

**Mac**

```bash
git pull
./mac_setup.sh            # first time: builds venv + checks. Later: ./mac_setup.sh check
```

Every line should say `ok`. The script prints the Mac's IP (call it `MAC_IP`).
Run the demo from **Terminal.app**, because macOS grants camera access per app.
If the camera check fails, go to System Settings → Privacy & Security →
Camera, enable Terminal, restart Terminal, and run the check again.

**Linux**

```bash
git pull && source venv/bin/activate
ip -4 -br addr                      # note LINUX_IP
ip route get <MAC_IP>               # "dev XXX" is your interface → IFACE (e.g. wlo1)
ping -c 3 <MAC_IP>
sudo -v                             # cache sudo so impair commands don't prompt mid-demo
sudo ./impair.sh clear <IFACE>      # start clean
```

**Network:** both laptops must be on the same network. Campus or enterprise
Wi‑Fi often blocks client-to-client traffic. If the peers can't see each
other, use a phone hotspot (tested: the Mac on an iPhone hotspot,
172.20.10.x) or an Ethernet cable (see §6).

Open http://127.0.0.1:8080 on the Mac once a call is running, and keep the
**Live** tab visible next to the call window.

---

## 1. Live call, no loss (sanity check, 30 s)

| Linux | Mac |
|---|---|
| `./demo.sh linux <MAC_IP> adaptive webcam` | `./demo.sh mac <LINUX_IP> adaptive webcam` |

Both windows show the other laptop's camera. The bottom bar shows `loss 0.0%`,
`r=4`, and `recovered 100%`.

> **Say:** "This is a real two-way video call over UDP between two laptops.
> Every frame is cut into about 25–30 packets. On a clean link the sender adds
> only about 4 parity packets, roughly 14% overhead."

Quit both with `q` (or Ctrl‑C) before the next step.

---

## 2. Baseline fail: FEC off at 20% loss

| Linux | Mac |
|---|---|
| `./demo.sh linux <MAC_IP> off webcam` | `./demo.sh mac <LINUX_IP> off webcam` |
| *(new terminal)* `sudo ./impair.sh add <IFACE> --loss 20 --ports 6000,6001,6010,6011` | |

On the Mac, the video breaks into grey/green blocks and freezes. The status
dot turns red, `recovered` falls to about 0%, and the dashboard explainer says
"Video is breaking up".

> **Say:** "I've told Linux to drop 1 in 5 of the packets it sends to the Mac.
> Without FEC, a frame survives only if all ~29 of its packets arrive:
> 0.8²⁹ ≈ 0.15%. So essentially every frame is damaged, and video calls have
> no time to retransmit."

Leave the impairment **on**. Quit both peers.

---

## 3. The save: adaptive FEC at the same 20% loss

| Linux | Mac |
|---|---|
| `./demo.sh linux <MAC_IP> adaptive webcam` | `./demo.sh mac <LINUX_IP> adaptive webcam` |
| *(impairment still at 20%)* | |

The Mac's video is clean again: `recovered 100%`, and the explainer turns
green. In the Linux terminal, the controller logs its decision as reports
arrive:

```
adaptive: peer reports 20.2% loss -> r=19 parity for k=29 (66% overhead)
```

On the dashboard's **Loss and redundancy** chart, the blue redundancy line
rises to sit above the orange loss line.

> **Say:** "Same 20% loss. Now each frame carries extra Reed–Solomon parity
> packets, and *any* 29 of the 48 packets rebuild it, so nothing is
> retransmitted. The Mac measures loss and reports it back every 5 frames.
> Linux then picks the smallest parity count that gives a 99.5% chance a frame
> survives. It spends bandwidth only when the network needs it."

---

## 4. Side by side: off and adaptive at the same time

Two calls run together over the same impaired link. Both replay the sample
clip (only one process can own the camera), which also gives PSNR/SSIM
numbers.

| Linux | Mac |
|---|---|
| `./demo.sh linux-split <MAC_IP>` | `./demo.sh mac-split <LINUX_IP>` |
| *(impairment from step 2 still at 20%; it already covers ports 6010/6011)* | open http://127.0.0.1:8080 and http://127.0.0.1:8081 side by side |

The Mac opens two windows, **FEC OFF** and **FEC ADAPTIVE**, which can be
dragged side by side. Expected result at 20%: OFF at about 0% recovered and
PSNR about 14 dB; ADAPTIVE at 100% recovered and PSNR about 40 dB.

Then open the **Results** tab on either dashboard for the full offline
sweep: 0–30% loss across the no/fixed/adaptive modes, with graphs and a table.

> **Say:** "Same network, same loss, at the same moment. The only difference
> is the FEC mode. These graphs are the full sweep. Fixed FEC hits a ceiling
> around 10–15% loss. Adaptive held 100% up to 30%."

---

## 5. Dial the loss: 0% → 60% live

Leave either step 3 (single call) or step 4 (split) running. On Linux:

```bash
sudo ./demo.sh dial <IFACE>         # press Enter for each step: 5,10,15,20,25,30,40,50,60%, then clear
# or automatic, 8 s per step:
sudo ./demo.sh dial <IFACE> 8
```

What to expect (measured on the Mac over loopback; real Wi‑Fi adds a little
loss of its own):

| Loss | Parity r (k≈29) | Recovered | PSNR |
|---:|---:|---:|---:|
| 0% | 4 | 100% | 40 dB |
| 20% | 19 | 100% | 40 dB |
| 30% | ~26 | 100% | 40 dB |
| 45% | 43 (cap = 1.5·k) | 100% | 40 dB |
| 50% | 43 (cap) | ~93% | 39 dB |
| 60% | 43 (cap) | ~51% | 29 dB |

> **Say (rising):** "Each time loss steps up there's a short dip, about half a
> second. That's the feedback delay: the report travels back, and the
> smoothed estimate catches up. Then it's clean again."
>
> **Say (at 50–60%):** "Here we've exceeded the FEC budget. Parity is capped
> at 1.5× the data, about 2.5× total bandwidth, because spending more would
> congest a real network. Beyond that, no erasure code can save the frame.
> The design degrades gracefully instead of collapsing."

Ctrl‑C stops the dial. Always finish with `sudo ./impair.sh clear <IFACE>`.

---

## 6. Fallback plans

**Peers can't reach each other (Wi‑Fi isolation, firewall):**

1. Switch both laptops to a phone hotspot and run step 0 again.
2. **Ethernet cable**: on the Mac, run
   `sudo ifconfig <MAC_ETH_IF> inet 10.0.0.1 netmask 255.255.255.0`
   (find the interface with `networksetup -listallhardwareports`). On Linux,
   run `sudo ip addr add 10.0.0.2/24 dev <ETH_IF>`. Then use
   `MAC_IP=10.0.0.1`, `LINUX_IP=10.0.0.2`, and `IFACE=<ETH_IF>`.
3. If the Mac never receives video, check the macOS firewall:
   `./mac_setup.sh check` prints the exact `socketfilterfw` command to allow
   Python.

**No usable network at all.** Run the whole story on the Mac alone. Loss is
dropped inside the app (`--sim-loss`), so neither `tc` nor root is needed:

```bash
./demo.sh local off 20          # baseline fail   (dashboard: http://127.0.0.1:8080)
./demo.sh local adaptive 20     # the save
./demo.sh local adaptive 40     # still holding
./demo.sh local adaptive 60     # FEC budget exceeded
```

Then open the dashboard's **Results** tab. It shows the pre-generated
evaluation from the Linux run (`results/*.png`, `comparison.csv`,
`summary.json`), so it works offline.

> **Say:** "The network here won't let the laptops talk to each other, so I'm
> running both peers on one machine. The app itself drops packets with the
> same random-loss model. The code path is identical; only the place the loss
> happens has moved."

**Camera won't open:** replace `webcam` with `file` in any command. The
sample clip also adds PSNR/SSIM to the dashboard.

---

## 7. Multipath / link-cut (future work)

The app uses a **single path**: one UDP socket pair, one peer IP. If the
interface carrying the call goes down, the call stops until that link
returns. It does not fail over to the second interface. We are not faking
this in the demo.

How it would be added: bind one socket per interface (Wi‑Fi and Ethernet),
learn the peer's address on each, and spray each frame's `k + r` chunks
across both paths. Because any `k` chunks rebuild the frame, losing a whole
path is just a burst of erasures, and the existing loss-report and parity
logic absorbs it as long as `r` covers that path's share. A per-path loss
report would let the sender shift chunks to the healthier link. This is
essentially FEC-protected multipath, the idea behind MPTCP/MPQUIC
redundancy schedulers.

> **Say:** "Erasure coding makes multipath easy: chunks from different paths
> are interchangeable, so a dead link is just more loss. We've scoped it as
> the next step."

---

## Copy-paste cheat sheet

```bash
# ---------- Mac (peer A) ----------
./mac_setup.sh check
./demo.sh mac <LINUX_IP> adaptive webcam      # steps 1, 3   (off for step 2)
./demo.sh mac-split <LINUX_IP>                # step 4
./demo.sh local adaptive 20                   # offline fallback
# dashboard: http://127.0.0.1:8080  (split: also :8081)

# ---------- Linux (peer B) ----------
./demo.sh linux <MAC_IP> adaptive webcam      # steps 1, 3   (off for step 2)
./demo.sh linux-split <MAC_IP>                # step 4
sudo ./impair.sh add    <IFACE> --loss 20 --ports 6000,6001,6010,6011
sudo ./impair.sh change <IFACE> --loss 30 --ports 6000,6001,6010,6011
sudo ./demo.sh dial <IFACE>                   # step 5
sudo ./impair.sh clear  <IFACE>
```

## Mac setup notes

* **Python 3.13**, not 3.14. `zfec` publishes macOS arm64 wheels only up
  to CPython 3.13. On 3.14, pip compiles it from source, and with the current
  Xcode Command Line Tools the link step fails
  (`libSystem.B.tbd: unknown architecture`). `mac_setup.sh` prefers 3.13
  automatically. If zfec still won't install, it falls back to the
  pure-Python `reedsolo` backend, which was verified to rebuild frames
  correctly.
* **Firewall:** with the macOS application firewall on, incoming UDP to
  Python must be allowed. Either click "Allow" on the first run, or run the
  command `./mac_setup.sh check` prints.
* **Two cameras:** an iPhone Continuity Camera may appear as camera 1. The
  demo uses camera 0, the built-in FaceTime HD camera. To change it, pass
  `--camera N` to `peer.py`.
