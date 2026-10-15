"""Per-frame metrics: CSV log plus a live, thread-safe stats object.

Every frame slot the receiver finalises becomes one ``FrameRecord``.  Its
outcome is one of

    intact   - every data chunk arrived, no FEC decoding needed
    rescued  - some data chunks were lost but FEC rebuilt the frame
    damaged  - too many chunks lost; the holes were zero-filled and the
               corrupted JPEG was shown as-is
    lost     - nothing usable arrived; the previous frame stays frozen

PSNR / SSIM compare what the viewer actually sees in that slot (including
damaged or frozen pictures) against the original clip frame, on the luma
channel.  SSIM is computed at half resolution to keep it real-time.
"""

import collections
import csv
import dataclasses
import math
import queue
import threading
import time

import cv2
from skimage.metrics import peak_signal_noise_ratio, structural_similarity

import codec

PSNR_CAP = 60.0
LIVE_WINDOW_S = 3.0
HISTORY_STEP_S = 0.5
HISTORY_LEN = 240


@dataclasses.dataclass
class FrameRecord:
    t: float
    frame_id: int
    src_index: int
    mode: str
    k: int
    n: int
    r: int
    received: int
    outcome: str
    latency_ms: float = math.nan
    fec_decode_ms: float = 0.0
    psnr: float = math.nan
    ssim: float = math.nan

    @property
    def loss_pct(self):
        return 100.0 * (1 - self.received / self.n) if self.n else 100.0

    @property
    def overhead_pct(self):
        return 100.0 * self.r / self.k if self.k else 0.0

    @property
    def recovered(self):
        return self.outcome in ("intact", "rescued")


CSV_FIELDS = ["t", "frame_id", "src_index", "mode", "k", "n", "r", "sent",
              "received", "loss_pct", "recovered", "outcome", "overhead_pct",
              "psnr", "ssim", "latency_ms", "fec_decode_ms"]


class Truth:
    """Ground-truth luma frames of the shared clip, indexed by src_index."""

    def __init__(self, frames):
        self.full = [codec.to_luma(f) for f in frames]
        self.half = [cv2.resize(g, None, fx=0.5, fy=0.5,
                                interpolation=cv2.INTER_AREA) for g in self.full]

    def __len__(self):
        return len(self.full)


def quality(truth, src_index, image):
    luma = codec.to_luma(image)
    ref = truth.full[src_index]
    if luma.shape != ref.shape:
        luma = cv2.resize(luma, (ref.shape[1], ref.shape[0]))
    psnr = min(PSNR_CAP, peak_signal_noise_ratio(ref, luma, data_range=255))
    half = cv2.resize(luma, (truth.half[src_index].shape[1],
                             truth.half[src_index].shape[0]),
                      interpolation=cv2.INTER_AREA)
    ssim = structural_similarity(truth.half[src_index], half, data_range=255)
    return float(psnr), float(ssim)


def _mean(values):
    values = [v for v in values if v is not None and not math.isnan(v)]
    return sum(values) / len(values) if values else None


class Metrics:
    def __init__(self, csv_path=None, truth=None, compute_quality=True):
        self.truth = truth
        self.compute_quality = compute_quality and truth is not None
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.window = collections.deque()
        self.history = collections.deque(maxlen=HISTORY_LEN)
        self.totals = collections.Counter()
        self.start = time.time()
        self.last_sample = 0.0
        self.latest = None
        self._csv_file = None
        self._csv = None
        if csv_path:
            self._csv_file = open(csv_path, "w", newline="")
            self._csv = csv.writer(self._csv_file)
            self._csv.writerow(CSV_FIELDS)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="metrics",
                                        daemon=True)
        self._thread.start()

    def submit(self, record, shown_image):
        self.q.put((record, shown_image))

    def close(self):
        self._stop.set()
        self._thread.join(timeout=5)
        if self._csv_file:
            self._csv_file.close()

    def _run(self):
        while not (self._stop.is_set() and self.q.empty()):
            try:
                record, image = self.q.get(timeout=0.25)
            except queue.Empty:
                self._sample()
                continue
            # Skip quality scoring if the CPU falls behind, never block the call.
            if (self.compute_quality and image is not None
                    and record.src_index >= 0 and self.q.qsize() < 30):
                record.psnr, record.ssim = quality(
                    self.truth, record.src_index % len(self.truth), image)
            self._log(record)
            self._sample()

    def _log(self, rec):
        if self._csv:
            self._csv.writerow([
                f"{rec.t:.3f}", rec.frame_id, rec.src_index, rec.mode, rec.k,
                rec.n, rec.r, rec.n, rec.received, f"{rec.loss_pct:.2f}",
                int(rec.recovered), rec.outcome, f"{rec.overhead_pct:.2f}",
                _fmt(rec.psnr), _fmt(rec.ssim, 4), _fmt(rec.latency_ms),
                _fmt(rec.fec_decode_ms, 3)])
        with self.lock:
            self.window.append(rec)
            self.latest = rec
            self.totals["frames"] += 1
            self.totals[rec.outcome] += 1
            cutoff = time.time() - LIVE_WINDOW_S
            while self.window and self.window[0].t < cutoff:
                self.window.popleft()

    def _sample(self):
        now = time.time()
        if now - self.last_sample < HISTORY_STEP_S:
            return
        self.last_sample = now
        s = self.stats()
        with self.lock:
            self.history.append({
                "t": round(now - self.start, 1),
                "loss": s["loss_pct"], "overhead": s["overhead_pct"],
                "r": s["r"], "psnr": s["psnr"], "ssim": s["ssim"],
                "recovery": s["recovery_pct"],
            })

    def stats(self):
        """Live stats over the last few seconds, rounded for display."""
        with self.lock:
            win = [r for r in self.window if r.t >= time.time() - LIVE_WINDOW_S]
            latest = self.latest
            totals = dict(self.totals)
        sent = sum(r.n for r in win)
        got = sum(r.received for r in win)
        budget = _mean([100.0 * r.r / r.n for r in win if r.n])

        def rnd(v, nd=1):
            return None if v is None else round(v, nd)

        return {
            "active": bool(win),
            "mode": latest.mode if latest else None,
            "frames": len(win),
            "fps": rnd(len(win) / LIVE_WINDOW_S),
            "loss_pct": rnd(100.0 * (1 - got / sent)) if sent else None,
            "r": latest.r if latest else None,
            "k": latest.k if latest else None,
            "overhead_pct": rnd(_mean([r.overhead_pct for r in win])),
            "budget_pct": rnd(budget),
            "recovery_pct": rnd(100.0 * sum(r.recovered for r in win) / len(win))
            if win else None,
            "rescued_pct": rnd(100.0 * sum(r.outcome == "rescued" for r in win)
                               / len(win)) if win else None,
            "psnr": rnd(_mean([r.psnr for r in win])),
            "ssim": rnd(_mean([r.ssim for r in win]), 3),
            "latency_ms": rnd(_mean([r.latency_ms for r in win])),
            "outcomes": {o: sum(r.outcome == o for r in win)
                         for o in ("intact", "rescued", "damaged", "lost")},
            "totals": totals,
        }

    def history_list(self):
        with self.lock:
            return list(self.history)


def _fmt(v, nd=2):
    return "" if v is None or math.isnan(v) else f"{v:.{nd}f}"
