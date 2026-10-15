#!/usr/bin/env python3
"""Bidirectional loss-resilient video call peer.

Run the same program on both machines; each instance captures and sends its
own video while receiving, FEC-decoding and displaying the other side's.

    send loop     capture -> JPEG -> FEC encode (k + r chunks) -> UDP media
    receive loop  UDP media -> reassemble -> FEC decode -> display + metrics
    control loop  loss feedback (receiver -> sender), clock-offset ping/pong
    UI            native OpenCV window (main thread) and optional web dashboard
"""

import argparse
import os
import random
import signal
import socket
import threading
import time

import cv2
import numpy as np

import adaptive
import codec
import fec
import packet
from metrics import FrameRecord, Metrics, Truth

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOW_SIZE = (960, 540)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    net = p.add_argument_group("network")
    net.add_argument("--listen-ip", default="0.0.0.0")
    net.add_argument("--listen-port", type=int, default=5000,
                     help="UDP media port; control uses port + 1")
    net.add_argument("--peer-ip", default="127.0.0.1")
    net.add_argument("--peer-port", type=int, default=6000,
                     help="peer's UDP media port; its control port is + 1")
    src = p.add_argument_group("video source")
    src.add_argument("--mode", choices=["webcam", "file"], default="webcam")
    src.add_argument("--source", default=os.path.join(HERE, "samples", "sample.mp4"),
                     help="video file for --mode file (and ground truth)")
    src.add_argument("--camera", type=int, default=0, help="webcam index")
    src.add_argument("--truth", help="clip used as ground truth for PSNR/SSIM "
                     "(defaults to --source in file mode)")
    src.add_argument("--fps", type=float, default=20.0)
    src.add_argument("--jpeg-quality", type=int, default=70)
    f = p.add_argument_group("forward error correction")
    f.add_argument("--fec", choices=list(packet.FEC_MODES), default="adaptive")
    f.add_argument("--fixed-redundancy", type=int, default=5,
                   help="parity chunks per frame in --fec fixed")
    f.add_argument("--chunk-size", type=int, default=fec.DEFAULT_CHUNK_SIZE)
    f.add_argument("--playout-ms", type=float, default=120.0,
                   help="how long to wait for late chunks before giving up on a frame")
    f.add_argument("--feedback-every", type=int, default=5,
                   help="send a loss report every N received frames")
    ui = p.add_argument_group("display and logging")
    ui.add_argument("--dashboard", action="store_true", help="serve the web dashboard")
    ui.add_argument("--dashboard-host", default="127.0.0.1")
    ui.add_argument("--dashboard-port", type=int, default=8080)
    ui.add_argument("--no-window", action="store_true", help="headless (no OpenCV window)")
    ui.add_argument("--name", default=None, help="label for the window title")
    ui.add_argument("--metrics-csv", default=None,
                    help="per-frame CSV (default logs/peer_<listen-port>.csv)")
    ui.add_argument("--no-quality", action="store_true", help="skip PSNR/SSIM")
    ui.add_argument("--quiet", action="store_true")
    t = p.add_argument_group("testing")
    t.add_argument("--duration", type=float, default=0, help="exit after N seconds")
    t.add_argument("--sim-loss", type=float, default=0.0,
                   help="drop this %% of outgoing packets in-app (no tc needed)")
    t.add_argument("--no-send", action="store_true", help="receive only")
    return p.parse_args(argv)


class Assembly:
    """Chunks collected so far for one frame."""

    __slots__ = ("hdr", "chunks", "first", "decoded", "image", "jpeg",
                 "latency_ms", "fec_ms")

    def __init__(self, hdr, now):
        self.hdr = hdr
        self.chunks = {}
        self.first = now
        self.decoded = False
        self.image = None
        self.jpeg = None
        self.latency_ms = float("nan")
        self.fec_ms = 0.0


class Peer:
    def __init__(self, args):
        self.args = args
        self.stop = threading.Event()
        self.started = time.time()

        self.media = self._socket(args.listen_port)
        self.ctrl = self._socket(args.listen_port + 1)
        self.peer_media = (args.peer_ip, args.peer_port)
        self.peer_ctrl = (args.peer_ip, args.peer_port + 1)
        self.last_from_peer = 0.0        # last media packet from the peer
        self.local_ip = _local_ip(args.peer_ip)

        self.source = None
        clip = None
        if args.mode == "file":
            self.source = codec.FileSource(args.source, args.fps)
            clip = self.source.frames
        truth_path = args.truth
        if clip is None and truth_path:
            clip = codec.load_clip(truth_path)
        elif truth_path and truth_path != args.source:
            clip = codec.load_clip(truth_path)
        self.truth = Truth(clip) if clip is not None else None

        csv_path = args.metrics_csv
        if csv_path is None:
            os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
            csv_path = os.path.join(HERE, "logs", f"peer_{args.listen_port}.csv")
        self.metrics = Metrics(csv_path, self.truth, not args.no_quality)

        self.estimator = adaptive.LossEstimator()
        self.controller = adaptive.AdaptiveController()

        # Shared state read by the UI and the dashboard.
        self.lock = threading.Lock()
        self.remote_image = None
        self.remote_jpeg = None
        self.local_image = None
        self.local_jpeg = None
        self.tx = {"frames": 0, "packets": 0, "dropped_sim": 0, "k": None,
                   "r": None, "bytes": 0}
        self.clock_offset = 0.0          # peer clock - local clock
        self._rtt_samples = []
        self.rtt_ms = None

    def _socket(self, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        for opt in (socket.SO_RCVBUF, socket.SO_SNDBUF):
            try:
                s.setsockopt(socket.SOL_SOCKET, opt, 4 << 20)
            except OSError:
                pass
        s.bind((self.args.listen_ip, port))
        s.settimeout(0.02)
        return s

    def _send(self, sock, data, addr):
        if self.args.sim_loss and random.random() * 100 < self.args.sim_loss:
            self.tx["dropped_sim"] += 1
            return
        try:
            sock.sendto(data, addr)
        except OSError:
            pass                         # peer not up yet (ICMP unreachable)

    # ------------------------------------------------------------- sending

    def send_loop(self):
        args = self.args
        source = self.source or codec.WebcamSource(args.camera, args.fps)
        has_truth = args.mode == "file"
        flags = packet.make_flags(args.fec, has_truth)
        interval = 1.0 / args.fps
        next_t = time.time()
        frame_id = 0
        last_overhead = None
        try:
            while not self.stop.is_set():
                frame, src = source.read()
                if frame is None:
                    continue
                jpeg = codec.encode(frame, args.jpeg_quality)
                size = fec.chunk_size_for(len(jpeg), args.chunk_size)
                k = len(fec.split(jpeg, size))
                if args.fec == "off":
                    r = 0
                elif args.fec == "fixed":
                    r = args.fixed_redundancy
                else:
                    r = self.controller.redundancy(k)
                chunks = fec.encode(jpeg, r, size)
                r = len(chunks) - k
                now = time.time()
                for i, chunk in enumerate(chunks):
                    hdr = packet.MediaHeader(
                        flags, frame_id,
                        src if src is not None else packet.NO_SOURCE,
                        i, k, len(chunks), r, len(jpeg), now)
                    self._send(self.media, packet.pack_media(hdr, chunk),
                               self.peer_media)
                with self.lock:
                    self.local_image = frame
                    self.local_jpeg = jpeg
                    self.tx.update(k=k, r=r)
                    self.tx["frames"] += 1
                    self.tx["packets"] += len(chunks)
                    self.tx["bytes"] += len(chunks) * (size + packet.HEADER_SIZE)
                overhead = 100 * r / k
                if args.fec == "adaptive" and not args.quiet \
                        and self.controller.reported is not None \
                        and (last_overhead is None or abs(overhead - last_overhead) >= 5):
                    last_overhead = overhead
                    self.log(f"adaptive: peer reports {100 * self.controller.reported:.1f}% "
                             f"loss -> r={r} parity for k={k} ({overhead:.0f}% overhead)")
                frame_id += 1
                next_t += interval
                delay = next_t - time.time()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_t = time.time()
        finally:
            source.close()

    # ----------------------------------------------------------- receiving

    def recv_loop(self):
        playout = self.args.playout_ms / 1000.0
        pending = {}                 # frame_id -> Assembly
        last_final = None            # highest finalised frame_id
        last_shown = -1
        last_hdr = None
        since_feedback = 0
        shown_image = None

        def finalise(fid, asm):
            nonlocal last_final, since_feedback, shown_image, last_shown
            if last_final is not None and fid - last_final > 1:
                gap = range(max(last_final + 1, fid - 200), fid)
                for g in gap:                       # frames that never arrived
                    self._record(g, last_hdr, 0, "lost", shown_image)
                    self.estimator.add(last_hdr.total_chunks, 0)
            h = asm.hdr
            if asm.decoded:
                outcome = "intact" if all(i in asm.chunks for i in range(h.k)) else "rescued"
                image = asm.image
            else:
                image = None
                if any(i < h.k for i in asm.chunks):
                    size = len(next(iter(asm.chunks.values())))
                    image = codec.decode_damaged(
                        fec.zero_fill(asm.chunks, h.k, size, h.frame_len))
                outcome = "damaged" if image is not None else "lost"
                if image is not None and fid > last_shown:
                    last_shown = fid
                    self._show(image, None)
            if image is not None:
                shown_image = image
            self._record(fid, h, len(asm.chunks), outcome, shown_image,
                         asm.latency_ms, asm.fec_ms)
            self.estimator.add(h.total_chunks, len(asm.chunks))
            last_final = fid
            since_feedback += 1
            if since_feedback >= self.args.feedback_every:
                since_feedback = 0
                self.send_feedback()

        while not self.stop.is_set():
            try:
                data, src_addr = self.media.recvfrom(65536)
            except (socket.timeout, BlockingIOError):
                data = None
            except OSError:
                continue
            now = time.time()
            if data:
                parsed = packet.unpack_media(data)
                if parsed:
                    self._note_source(src_addr)
                    h, payload = parsed
                    fid = h.frame_id
                    if last_final is not None and (fid < last_final - 1000
                                                   or fid > last_final + 100000):
                        pending.clear()             # the sender restarted
                        last_final = None
                    if last_final is None:
                        last_final = fid - 1
                    if fid > last_final:
                        asm = pending.get(fid)
                        if asm is None:
                            asm = pending[fid] = Assembly(h, now)
                        last_hdr = h
                        asm.chunks[h.chunk_index] = payload
                        if not asm.decoded and len(asm.chunks) >= h.k:
                            t0 = time.perf_counter()
                            jpeg = fec.decode(asm.chunks, h.k, h.total_chunks,
                                              h.frame_len)
                            asm.fec_ms = 1000 * (time.perf_counter() - t0)
                            img = codec.decode(jpeg)
                            if img is not None:
                                asm.decoded = True
                                asm.image = img
                                local_send = h.send_ts - self.clock_offset
                                asm.latency_ms = 1000 * (time.time() - local_send)
                                if fid > last_shown:
                                    last_shown = fid
                                    self._show(img, jpeg)
            # Finalise frames in order once complete, stale, or overtaken.
            newest = max(pending) if pending else None
            for fid in sorted(pending):
                asm = pending[fid]
                complete = len(asm.chunks) >= asm.hdr.total_chunks
                if complete or now - asm.first > playout or fid < newest - 3:
                    finalise(fid, pending.pop(fid))
                else:
                    break

    def _note_source(self, addr):
        """Follow the peer if its video arrives from an unexpected address.

        A typo'd or stale --peer-ip on one side would otherwise give a one-way
        call; once we hear from the peer we know exactly where to send.
        """
        now = time.time()
        if addr == self.peer_media:
            self.last_from_peer = now
        elif now - self.last_from_peer > 3.0:
            self.log(f"receiving video from {addr[0]}:{addr[1]} but --peer-ip is "
                     f"{self.peer_media[0]}:{self.peer_media[1]}; sending there instead")
            self.peer_media = addr
            self.peer_ctrl = (addr[0], addr[1] + 1)
            self.last_from_peer = now

    def _show(self, image, jpeg):
        with self.lock:
            self.remote_image = image
            self.remote_jpeg = jpeg

    def _record(self, fid, h, received, outcome, shown, latency=float("nan"),
                fec_ms=0.0):
        # In file mode the sender plays clip frame frame_id % clip length, so
        # ground truth is known even for frames that never arrived.
        src = fid % len(self.truth) if h.has_truth and self.truth is not None else -1
        rec = FrameRecord(time.time(), fid, src, h.fec_mode, h.k,
                          h.total_chunks, h.redundancy, received, outcome,
                          latency, fec_ms)
        self.metrics.submit(rec, shown)

    # ------------------------------------------------------------- control

    def send_feedback(self):
        msg = {"t": "fb", "loss": round(self.estimator.loss(), 4),
               "n": len(self.estimator)}
        self._send(self.ctrl, packet.pack_control(msg), self.peer_ctrl)

    def control_loop(self):
        last_ping = last_fb = 0.0
        while not self.stop.is_set():
            now = time.time()
            if now - last_ping > 1.0:
                last_ping = now
                self._send(self.ctrl, packet.pack_control({"t": "ping", "t0": now}),
                           self.peer_ctrl)
            if now - last_fb > 1.0 and len(self.estimator):
                last_fb = now                # heartbeat even if frames stall
                self.send_feedback()
            try:
                data, addr = self.ctrl.recvfrom(4096)
            except (socket.timeout, BlockingIOError, OSError):
                continue
            msg = packet.unpack_control(data)
            if not msg:
                continue
            kind = msg.get("t")
            if kind == "fb":
                self.controller.on_feedback(float(msg["loss"]))
            elif kind == "ping":
                t1 = time.time()
                self._send(self.ctrl, packet.pack_control(
                    {"t": "pong", "t0": msg["t0"], "t1": t1, "t2": time.time()}),
                    self.peer_ctrl)
            elif kind == "pong":
                t3 = time.time()
                t0, t1, t2 = msg["t0"], msg["t1"], msg["t2"]
                rtt = (t3 - t0) - (t2 - t1)
                offset = ((t1 - t0) + (t2 - t3)) / 2
                self._rtt_samples = (self._rtt_samples + [(rtt, offset)])[-8:]
                best = min(self._rtt_samples)       # least-delayed sample
                self.rtt_ms = 1000 * best[0]
                self.clock_offset = best[1]

    # ------------------------------------------------------------------ UI

    def snapshot(self):
        """Everything the dashboard needs, as plain JSON-able data."""
        stats = self.metrics.stats()
        with self.lock:
            tx = dict(self.tx)
        c = self.controller
        return {
            "uptime": round(time.time() - self.started, 1),
            "config": {
                "name": self.args.name, "fec": self.args.fec,
                "listen": f"{self.args.listen_ip}:{self.args.listen_port}",
                "peer": f"{self.peer_media[0]}:{self.peer_media[1]}",
                "fixed_redundancy": self.args.fixed_redundancy,
                "source": self.args.mode, "backend": fec.BACKEND,
            },
            "rx": stats,
            "tx": {
                "k": tx["k"], "r": tx["r"], "frames": tx["frames"],
                "overhead_pct": round(100 * tx["r"] / tx["k"], 1) if tx["k"] else None,
                "peer_reported_loss_pct": None if c.reported is None
                else round(100 * c.reported, 1),
                "smoothed_loss_pct": round(100 * c.loss, 1),
            },
            "rtt_ms": None if self.rtt_ms is None else round(self.rtt_ms, 1),
            "history": self.metrics.history_list(),
        }

    def compose(self):
        """Frame for the native call window: remote video, local preview, status."""
        w, h = WINDOW_SIZE
        bar = 44
        canvas = np.full((h + bar, w, 3), 18, np.uint8)
        with self.lock:
            remote = self.remote_image
            local = self.local_image
        if remote is not None:
            canvas[:h] = cv2.resize(remote, (w, h), interpolation=cv2.INTER_LINEAR)
        else:
            cv2.putText(canvas, "Waiting for peer video...", (w // 2 - 190, h // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (200, 200, 200), 2, cv2.LINE_AA)
        if local is not None:
            pw, ph = w // 5, h // 5
            x, y = w - pw - 16, 16
            canvas[y:y + ph, x:x + pw] = cv2.resize(local, (pw, ph))
            cv2.rectangle(canvas, (x, y), (x + pw, y + ph), (240, 240, 240), 1)
        s = self.metrics.stats()
        if s["active"]:
            rec = s["recovery_pct"]
            color = (90, 200, 90) if rec >= 99 else (40, 180, 240) if rec >= 90 \
                else (70, 70, 230)
            text = (f"{s['mode'].upper()}  loss {s['loss_pct']:.1f}%  "
                    f"r={s['r']} (k={s['k']})  recovered {rec:.0f}%")
            if s["psnr"] is not None:
                text += f"  PSNR {s['psnr']:.1f} dB"
            cv2.circle(canvas, (20, h + bar // 2), 7, color, -1, cv2.LINE_AA)
            cv2.putText(canvas, text, (36, h + bar // 2 + 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (235, 235, 235), 1, cv2.LINE_AA)
        return canvas

    def log(self, msg):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    def status_line(self):
        s = self.metrics.stats()
        c = self.controller
        idle = (f"rx: nothing from {self.peer_media[0]} yet -- the other laptop must "
                f"send to {self.local_ip}:{self.args.listen_port}")
        rx = (idle if not s["active"] else
              f"rx[{s['mode']}]: loss {s['loss_pct']:.1f}%  r={s['r']}/k={s['k']}  "
              f"recovered {s['recovery_pct']:.0f}%  "
              + (f"PSNR {s['psnr']:.1f} dB  SSIM {s['ssim']:.3f}  " if s["psnr"] else "")
              + (f"latency {s['latency_ms']:.0f} ms" if s["latency_ms"] else ""))
        tx = f"tx[{self.args.fec}]: r={self.tx['r']}/k={self.tx['k']}"
        if c.reported is not None:
            tx += f"  peer sees {100 * c.reported:.1f}% loss"
        return f"{rx}  |  {tx}"

    def run(self):
        threads = [threading.Thread(target=self.recv_loop, name="recv", daemon=True),
                   threading.Thread(target=self.control_loop, name="ctrl", daemon=True)]
        if not self.args.no_send:
            threads.append(threading.Thread(target=self._guard(self.send_loop),
                                            name="send", daemon=True))
        for t in threads:
            t.start()
        if self.args.dashboard:
            from dashboard.server import start_dashboard
            start_dashboard(self, self.args.dashboard_host, self.args.dashboard_port)
            self.log(f"dashboard: http://{self.args.dashboard_host}:"
                     f"{self.args.dashboard_port}")
        if not self.args.quiet:
            self.log(f"listening on {self.local_ip}:{self.args.listen_port} "
                     f"(control {self.args.listen_port + 1}), sending to "
                     f"{self.args.peer_ip}:{self.args.peer_port}, fec={self.args.fec}, "
                     f"backend={fec.BACKEND}")
        title = f"FEC call - {self.args.name or self.args.listen_port}"
        last_status = time.time()
        try:
            while not self.stop.is_set():
                now = time.time()
                if self.args.duration and now - self.started > self.args.duration:
                    break
                if not self.args.quiet and now - last_status > 2.0:
                    last_status = now
                    self.log(self.status_line())
                if self.args.no_window:
                    time.sleep(0.05)
                    continue
                cv2.imshow(title, self.compose())
                key = cv2.waitKey(15) & 0xFF
                if key in (ord("q"), 27):
                    break
                try:
                    if cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                        break
                except cv2.error:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.stop.set()
            for t in threads:
                t.join(timeout=2)
            self.metrics.close()
            if not self.args.no_window:
                cv2.destroyAllWindows()

    def _guard(self, fn):
        def wrapped():
            try:
                fn()
            except Exception as e:           # surface capture errors, then quit
                self.log(f"send loop stopped: {e}")
                self.stop.set()
        return wrapped


def _local_ip(peer_ip):
    """The address of the interface used to reach the peer."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer_ip, 9))          # UDP connect sends nothing
        return s.getsockname()[0]
    except OSError:
        return "this machine's IP"
    finally:
        s.close()


def _interrupt(*_):
    raise KeyboardInterrupt


def main(argv=None):
    args = parse_args(argv)
    signal.signal(signal.SIGTERM, _interrupt)
    Peer(args).run()


if __name__ == "__main__":
    main()
