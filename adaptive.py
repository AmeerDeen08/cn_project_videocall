"""Adaptive FEC: receiver-side loss estimation, sender-side redundancy control.

Receiver
    ``LossEstimator`` keeps a sliding window of finalised frames and reports
    the chunk loss rate  1 - sum(received) / sum(sent)  over that window.  The
    estimate is sent back on the control channel every few frames.

Sender
    ``AdaptiveController`` smooths the reported loss (react fast when loss
    rises, relax slowly when it falls) and then picks, for each frame of ``k``
    data chunks, the *smallest* number of parity chunks ``r`` such that

        P[ at most r of the k + r chunks are lost ]  >=  target

    assuming independent loss at the smoothed rate plus a safety margin.  That
    is "enough parity to cover the measured loss plus a small margin", derived
    from the binomial distribution rather than a hand-tuned table.  ``r`` is
    clamped to [r_min, r_max_ratio * k].
"""

import collections
import math
import threading
import time


class LossEstimator:
    def __init__(self, window=40):
        self.samples = collections.deque(maxlen=window)
        self.lock = threading.Lock()

    def add(self, sent, received):
        with self.lock:
            self.samples.append((sent, received))

    def loss(self):
        with self.lock:
            sent = sum(s for s, _ in self.samples)
            got = sum(r for _, r in self.samples)
        return 0.0 if sent == 0 else max(0.0, 1.0 - got / sent)

    def __len__(self):
        return len(self.samples)


def recovery_probability(k, r, p):
    """P[Binomial(k + r, p) <= r]: chance a frame survives with r parity."""
    n = k + r
    q = 1.0 - p
    return sum(math.comb(n, i) * p ** i * q ** (n - i) for i in range(r + 1))


class AdaptiveController:
    def __init__(self, target=0.995, margin=0.03, r_min=1, r_max_ratio=1.5,
                 alpha_up=0.6, alpha_down=0.15, initial_loss=0.05):
        self.target = target
        self.margin = margin
        self.r_min = r_min
        self.r_max_ratio = r_max_ratio
        self.alpha_up = alpha_up
        self.alpha_down = alpha_down
        self.loss = initial_loss         # smoothed estimate
        self.reported = None             # latest raw report from the receiver
        self.last_feedback = None
        self.history = []                # (time, reported, smoothed, r for k=25)
        self._cache = {}
        self.lock = threading.Lock()

    def on_feedback(self, reported_loss):
        with self.lock:
            a = self.alpha_up if reported_loss > self.loss else self.alpha_down
            self.loss += a * (reported_loss - self.loss)
            self.reported = reported_loss
            self.last_feedback = time.time()
            self.history.append((self.last_feedback, reported_loss, self.loss,
                                 self._redundancy(25)))
            del self.history[:-2000]

    def redundancy(self, k):
        with self.lock:
            return self._redundancy(k)

    def _redundancy(self, k):
        p = min(self.loss + self.margin, 0.6)
        key = (k, round(p, 3))
        r = self._cache.get(key)
        if r is None:
            r_max = max(self.r_min, min(int(self.r_max_ratio * k), 255 - k))
            r = self.r_min
            while r < r_max and recovery_probability(k, r, p) < self.target:
                r += 1
            self._cache[key] = r
        return r
