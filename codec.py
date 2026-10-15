"""Frame capture and JPEG encode/decode.

Every frame is an independent JPEG, so a lost frame never corrupts the
frames after it -- which keeps the effect of FEC easy to measure.
"""

import os
import sys
import threading
import time

import cv2
import numpy as np

FRAME_SIZE = (640, 360)        # (width, height) sent on the wire


def encode(frame, quality=70):
    # Restart markers let a damaged JPEG resynchronise, so a frame with missing
    # chunks shows localised corruption instead of failing outright.
    params = [cv2.IMWRITE_JPEG_QUALITY, quality,
              cv2.IMWRITE_JPEG_RST_INTERVAL, 4]
    ok, buf = cv2.imencode(".jpg", frame, params)
    if not ok:
        raise RuntimeError("JPEG encode failed")
    return buf.tobytes()


def decode(jpeg):
    """Decode JPEG bytes to a BGR image, or None if undecodable."""
    if not jpeg:
        return None
    return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)


_stderr_lock = threading.Lock()


def decode_damaged(jpeg):
    """Decode a JPEG that has zero-filled holes in it.

    libjpeg prints "Corrupt JPEG data" warnings straight to stderr for every
    damaged frame, so stderr is silenced for the duration of the call.
    """
    with _stderr_lock:
        try:
            saved = os.dup(2)
        except OSError:
            return decode(jpeg)
        try:
            with open(os.devnull, "w") as devnull:
                os.dup2(devnull.fileno(), 2)
                return decode(jpeg)
        finally:
            sys.stderr.flush()
            os.dup2(saved, 2)
            os.close(saved)


def fit(frame, size=FRAME_SIZE):
    if frame.shape[1] != size[0] or frame.shape[0] != size[1]:
        frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    return frame


def to_luma(frame):
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


class FileSource:
    """Plays a video file in a loop at a fixed frame rate.

    Frame i of the stream is always clip frame ``i % len(clip)``, which is what
    lets the receiver look up ground truth for PSNR/SSIM.
    """

    def __init__(self, path, fps):
        self.frames = load_clip(path)
        self.fps = fps
        self.index = 0

    def read(self):
        frame = self.frames[self.index % len(self.frames)]
        src = self.index % len(self.frames)
        self.index += 1
        return frame, src

    def close(self):
        pass


class WebcamSource:
    def __init__(self, device, fps):
        self.cap = cv2.VideoCapture(device)
        if not self.cap.isOpened():
            raise RuntimeError(f"cannot open webcam {device}")
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_SIZE[0])
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_SIZE[1])
        self.fps = fps

    def read(self):
        ok, frame = self.cap.read()
        if not ok:
            time.sleep(0.01)
            return None, None
        return fit(cv2.flip(frame, 1)), None

    def close(self):
        self.cap.release()


def load_clip(path):
    """Load every frame of a clip, resized to the wire resolution."""
    cap = cv2.VideoCapture(path)
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(fit(frame))
    cap.release()
    if not frames:
        raise RuntimeError(f"could not read any frames from {path}")
    return frames
