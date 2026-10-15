#!/usr/bin/env python3
"""Generate samples/sample.mp4: a deterministic, detailed test clip.

The clip has fine texture, moving objects, text and a frame counter so that
damaged or frozen frames show up clearly in both the picture and PSNR/SSIM.
Both peers read the same file, which gives exact ground truth.
"""

import argparse
import math
import os

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))


def render(i, n, w, h, texture):
    t = i / n
    x = np.linspace(0, 1, w, dtype=np.float32)[None, :]
    y = np.linspace(0, 1, h, dtype=np.float32)[:, None]
    base = np.empty((h, w, 3), np.float32)
    base[..., 0] = 120 + 90 * np.sin(2 * math.pi * (x + t))
    base[..., 1] = 110 + 70 * np.cos(2 * math.pi * (y * 1.5 - t))
    base[..., 2] = 130 + 80 * np.sin(2 * math.pi * (x + y + 2 * t))
    shift = int(t * w * 2) % w
    frame = np.clip(base + np.roll(texture, shift, axis=1), 0, 255).astype(np.uint8)

    # A checkerboard panel: sharp edges make corruption obvious.
    cs = 20
    for r in range(4):
        for c in range(6):
            if (r + c + i // 10) % 2 == 0:
                cv2.rectangle(frame, (40 + c * cs, 40 + r * cs),
                              (40 + (c + 1) * cs, 40 + (r + 1) * cs), (245, 245, 245), -1)
    for k in range(5):
        a = 2 * math.pi * (t * (k + 1) + k / 5)
        cx = int(w / 2 + (w / 3) * math.cos(a))
        cy = int(h / 2 + (h / 3) * math.sin(1.3 * a))
        color = [(40, 90, 230), (40, 200, 120), (230, 140, 40), (200, 60, 200),
                 (30, 200, 230)][k]
        cv2.circle(frame, (cx, cy), 26 + 6 * k, color, -1, cv2.LINE_AA)
    center = (int(w * 0.72), int(h * 0.3))
    box = cv2.boxPoints((center, (130, 70), 360 * t * 2))
    cv2.fillPoly(frame, [box.astype(np.int32)], (20, 20, 20), cv2.LINE_AA)
    cv2.putText(frame, "UNBREAKABLE CALL", (int(w - (t * 2 * w) % (w + 420)), h - 40),
                cv2.FONT_HERSHEY_DUPLEX, 1.1, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(frame, f"frame {i:03d}", (w - 190, 50), cv2.FONT_HERSHEY_SIMPLEX,
                0.9, (255, 255, 255), 2, cv2.LINE_AA)
    return frame


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default=os.path.join(HERE, "samples", "sample.mp4"))
    p.add_argument("--seconds", type=float, default=8)
    p.add_argument("--fps", type=float, default=20)
    p.add_argument("--size", default="640x360")
    a = p.parse_args()
    w, h = map(int, a.size.split("x"))
    n = int(a.seconds * a.fps)
    rng = np.random.default_rng(7)
    texture = cv2.GaussianBlur(rng.normal(0, 28, (h, w, 3)).astype(np.float32), (3, 3), 0)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    out = cv2.VideoWriter(a.out, cv2.VideoWriter_fourcc(*"mp4v"), a.fps, (w, h))
    for i in range(n):
        out.write(render(i, n, w, h, texture))
    out.release()
    print(f"wrote {a.out}: {n} frames, {w}x{h} @ {a.fps:g} fps")


if __name__ == "__main__":
    main()
