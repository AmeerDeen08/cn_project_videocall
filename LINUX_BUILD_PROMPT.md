# Build Prompt — Linux machine (primary development)

**Paste this whole file to Claude Code inside the cloned repo on your Linux laptop.**

---

You are building a **course project for a Computer Networks subject**: an **"unbreakable" video‑calling application** that survives heavy packet loss using **adaptive Forward Error Correction (FEC)**. This machine (Linux) is the primary development and testing environment. Build the complete application, a **live web dashboard**, and a rigorous evaluation harness here, then commit and push to GitHub. A second machine (macOS) will later clone this repo and act as the remote peer for the live demo — so everything must be **one cross‑platform codebase**, not Linux‑only code.

## Core idea / novelty
A normal call drops quality or freezes under packet loss. This app sends **redundant FEC packets** so the receiver can **reconstruct lost data without retransmission**, and the **redundancy level adapts in real time to the measured loss rate**. The deliverable includes a clean **loss‑vs‑quality study** comparing three modes: no‑FEC, fixed‑FEC, and adaptive‑FEC. No machine learning is used anywhere.

## Design decision (important)
Do **not** fight the internal FEC of a full WebRTC stack. Build a **custom UDP media pipeline** so FEC is fully controllable and measurable — this is the whole point of the project. Use Python. The browser/dashboard is a **display and explanation layer only**; all networking, FEC, and decoding happen in Python over UDP.

## Architecture
```
   Peer A  <─────────── UDP media (RTP-like packets) ───────────>  Peer B
           <─────────── UDP control (loss feedback) ────────────>

   Python receiver ──► decoded frames ──► (1) native OpenCV call window
                                          (2) live web dashboard (Linux side only)
```
Each peer is **bidirectional**: it captures video, encodes + FEC‑encodes, and sends; and simultaneously receives, FEC‑decodes, and displays. The same program runs on both machines with a role/config flag.

## Two display surfaces
1. **Live call window (both machines):** the actual received video renders in a **native OpenCV window** (`cv2.imshow`) — reliable, low‑latency, always works.
2. **Live dashboard webpage (Linux side only):** a local web page that shows the system working **and explains the output** in real time. This is the showpiece for the demo and must look polished (see UI direction below).

## Components to implement
1. **`peer.py`** — the unified application. CLI args:
   - `--listen-ip`, `--listen-port`, `--peer-ip`, `--peer-port`
   - `--mode {webcam,file}` and `--source <path>` (a sample `.mp4` for repeatable, measurable runs; webcam for the live demo)
   - `--fec {off,fixed,adaptive}` and `--fixed-redundancy <int>`
   - `--dashboard` to enable the web dashboard server (Linux side).
   - Runs concurrent loops: send pipeline, receive pipeline, control‑feedback loop, and (if enabled) the dashboard server.

2. **`codec.py`** — frame encode/decode. Capture frames with OpenCV, encode each frame to **JPEG** (`cv2.imencode`), decode on the other side. Deterministic and simple.

3. **`fec.py`** — **systematic erasure coding** over packets using **`zfec`** (preferred; built exactly for k‑of‑n packet erasure). Split each JPEG frame into `k` fixed‑size data chunks, produce `k + r` chunks where `r` is the redundancy. Receiver reconstructs the frame if **any `k` of the `k+r` chunks arrive**; otherwise mark the frame unrecovered. Fall back to `reedsolo` if `zfec` is unavailable.

4. **Packetization** — each UDP media packet header carries: `frame_id, chunk_index, k, total_chunks, redundancy, send_timestamp`. Used for reassembly, loss measurement, and latency.

5. **`adaptive.py`** — the adaptive controller (the novelty). The receiver measures the **per‑frame chunk loss rate** over a sliding window and sends an estimate back on the **control channel** every N frames. The sender sets `r` as a function of measured loss (enough parity to cover loss plus a small margin), clamped to sane bounds. Log `r` over time so the adaptation is visible.

6. **`metrics.py`** — per‑frame logging to CSV **and** an in‑memory live stats object the dashboard reads: sent chunks, received chunks, recovered? (bool), loss %, **PSNR and SSIM** of the decoded frame vs the original (use `scikit-image`), FEC overhead %, redundancy `r`, and added latency. In `--mode file` both peers share the same source file, giving exact ground truth for PSNR/SSIM.

7. **`dashboard/` — the live web dashboard (Linux side only).** A small **Flask** app (plus a single‑page frontend) that serves:
   - **Live received video** via an **MJPEG** endpoint (`multipart/x-mixed-replace`), displayed in the page with an `<img>` — fed by the Python receiver's already‑decoded frames. (Optionally also show the outgoing/local feed.)
   - **Live metrics**, pushed via **WebSocket** (preferred) or polled from a `/stats` JSON endpoint ~2–4×/sec: current loss %, redundancy `r`, FEC overhead %, recovery rate, PSNR, SSIM, latency, current mode.
   - **Live charts** that update in real time: loss % and redundancy `r` on a shared timeline (shows adaptation), and PSNR over time. Use a lightweight chart lib (e.g. Chart.js from a CDN).
   - **A plain‑language "what's happening" explainer panel** that updates live, e.g. *"Loss rose to 22%. Adaptive FEC increased redundancy to 40% to compensate — frames are still being fully recovered, call stable."* and flips to a warning state if loss exceeds the FEC budget. This "explains the output" as required.
   - **A mode badge** (no‑FEC / fixed / adaptive) and a small legend explaining each metric in one line so a viewer understands the screen without narration.
   - After an `evaluate.py` run, a **results view** embedding the loss‑vs‑quality chart and the three‑mode comparison table.

8. **`impair.sh`** — a helper wrapping **`tc netem`** to inject/clear loss, delay, and jitter on an interface (e.g. `sudo tc qdisc add dev <iface> root netem loss 20%`), with add/change/clear subcommands. Linux‑only; used for testing/evaluation here.

9. **`evaluate.py`** — the study. Sweep loss ∈ {0,5,10,15,20,25,30}%. For each level, run a fixed‑length clip in all three FEC modes, collect metrics, and output: a **comparison CSV**, **matplotlib graphs** (loss % vs PSNR; FEC overhead vs loss; recovery rate vs loss), and a summary table that the dashboard results view can load.

10. **`README.md`** — architecture diagram (ASCII fine), run instructions for single‑machine testing and two‑machine use, how to open the dashboard, and a short "how the adaptive FEC works" section.

## UI design direction (make the dashboard look genuinely professional — Apple‑like)
The dashboard must read as a clean, modern, Apple‑style interface — calm, spacious, and precise. Follow these rules:
- **Typography:** system font stack `-apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", system-ui, sans-serif`. Large, confident numbers for key metrics; quiet, muted labels. Two weights only (regular + medium). Sentence case everywhere.
- **Layout:** generous whitespace, a centered max‑width container, content organized into **rounded cards** (`border-radius: 16–20px`) on a light neutral canvas. Hairline separators, not heavy borders. Clear hierarchy: video + live explainer at top, metric cards in a tidy grid, charts below.
- **Color:** mostly neutral grays/whites with **one accent** (Apple blue `#0071e3` or similar). Use color only to carry meaning — green for "stable/recovered", amber for "at FEC budget", red for "loss exceeds FEC". No gradients, no neon, no clutter.
- **Surfaces:** flat with **very subtle** soft shadows (`box-shadow: 0 1px 3px rgba(0,0,0,.08)`), not heavy. Smooth, rounded, restrained.
- **Metric cards:** small muted label on top, large value below (e.g. `34px/500`), optional tiny trend. Rounded, soft‑shadowed, evenly spaced in a responsive grid.
- **Motion:** gentle, quick transitions on value changes (no bouncing or flashing). Numbers must round cleanly (no float noise like `0.300000004`).
- **Dark mode:** support it via `prefers-color-scheme` so it looks right in any room.
- **Overall feel:** if it looks like a default Bootstrap page, it's wrong. Aim for the restraint and polish of an Apple product page / macOS Settings — lots of air, perfect alignment, a single accent, legible at a glance from across a room.

## Tech stack
Python 3.11+, `opencv-python`, `numpy`, `zfec` (or `reedsolo`), `scikit-image` (PSNR/SSIM), `matplotlib`, `flask`, and a WebSocket mechanism (`flask-sock` or `simple-websocket`; polling `/stats` is an acceptable fallback). Frontend uses plain HTML/CSS/JS with Chart.js from a CDN — no heavy build step. Keep dependencies in `requirements.txt`. Pure‑Python/cross‑platform only — nothing that won't install on macOS (the `tc`/`impair.sh` part may be Linux‑only since it's test tooling, not the app). The dashboard runs on the Linux side for the demo.

## How to test on this one machine (before the Mac exists)
- Run two instances of `peer.py` in two terminals, one sending to the other over loopback or two **Linux network namespaces**, with `--dashboard` on the receiver.
- Open the dashboard in a browser; apply loss with `impair.sh` on the link.
- Confirm: with `--fec off` the video breaks under 20% loss and the dashboard explainer flags frames lost; with `--fec adaptive` the video stays intact, the redundancy line visibly rises on the chart, and the explainer says the call is stable.
- Then run `evaluate.py` to produce the full study and load it into the dashboard results view.

## Definition of done (check all)
- [ ] Two local instances hold a live video call over UDP, shown in the native window.
- [ ] `--fec off` visibly fails at 20% loss; `--fec adaptive` recovers.
- [ ] Adaptive controller changes redundancy in response to measured loss (shown live on the dashboard chart).
- [ ] Live dashboard shows the received video (MJPEG) + live metrics + real‑time charts + the plain‑language explainer, and looks clean/modern/Apple‑like in both light and dark mode.
- [ ] `evaluate.py` produces the comparison CSV + graphs for all three modes, viewable in the dashboard results view.
- [ ] `README.md` explains architecture, single‑machine run, two‑machine run, and how to open the dashboard.
- [ ] `requirements.txt` installs cleanly.

## Git steps (do these at the end)
This repo is already set to `https://github.com/AmeerDeen08/cn_project_videocall`.
1. Ensure a sensible `.gitignore` (ignore `__pycache__/`, large `*.mp4` captures, `venv/`, bulk CSV/PNG outputs except a few sample results worth keeping).
2. Stage and commit everything with a clear message.
3. Push to `main`.
4. Print the final file tree and a one‑paragraph summary of what was built, how to launch the dashboard, and how to run the two‑machine demo, so I can hand the next prompt to the Mac.

Work autonomously: scaffold, implement, test locally with `tc netem`, iterate until the Definition of Done is met, then push.
