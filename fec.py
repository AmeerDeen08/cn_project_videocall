"""Systematic packet-level erasure coding.

A frame of ``frame_len`` bytes is split into ``k`` equal data chunks (the last
one zero-padded) and ``r`` parity chunks are added, giving ``n = k + r``
chunks.  Because the code is MDS, the receiver rebuilds the frame from *any*
``k`` of the ``n`` chunks.  Because it is systematic, chunks ``0..k-1`` are the
raw data, so a frame with no losses needs no decoding at all.

``zfec`` (a fast C Reed-Solomon implementation) is used when available; a
pure-Python ``reedsolo`` fallback keeps the project installable anywhere.
"""

import math

try:
    import zfec
    BACKEND = "zfec"
except ImportError:          # pragma: no cover - exercised only without zfec
    zfec = None
    import reedsolo
    BACKEND = "reedsolo"

MAX_CHUNKS = 255             # field size limit for both backends
DEFAULT_CHUNK_SIZE = 1200    # keeps header + payload well under a 1500 B MTU
MAX_K = 120                  # caps k so there is always room for parity


def chunk_size_for(frame_len, base=DEFAULT_CHUNK_SIZE):
    """Chunk size that keeps k <= MAX_K for very large frames."""
    return max(base, math.ceil(frame_len / MAX_K))


def split(data, chunk_size):
    k = max(1, math.ceil(len(data) / chunk_size))
    padded = data.ljust(k * chunk_size, b"\0")
    return [padded[i * chunk_size:(i + 1) * chunk_size] for i in range(k)]


_encoders = {}
_decoders = {}


def encode(data, r, chunk_size):
    """Return the list of k + r chunks for ``data``."""
    blocks = split(data, chunk_size)
    k = len(blocks)
    r = max(0, min(r, MAX_CHUNKS - k))
    if r == 0:
        return blocks
    n = k + r
    if zfec is not None:
        enc = _encoders.get((k, n))
        if enc is None:
            enc = _encoders[(k, n)] = zfec.Encoder(k, n)
        return [bytes(b) for b in enc.encode(blocks)]
    return blocks + _rs_parity(blocks, r)


def decode(chunks, k, n, frame_len):
    """Rebuild the frame from a {chunk_index: bytes} map.

    Returns the frame bytes, or None if fewer than k chunks arrived.
    """
    if len(chunks) < k:
        return None
    if all(i in chunks for i in range(k)):          # systematic fast path
        data = b"".join(chunks[i] for i in range(k))
        return data[:frame_len]
    if zfec is not None:
        dec = _decoders.get((k, n))
        if dec is None:
            dec = _decoders[(k, n)] = zfec.Decoder(k, n)
        idx = sorted(chunks)[:k]
        blocks = dec.decode([chunks[i] for i in idx], idx)
        return b"".join(bytes(b) for b in blocks)[:frame_len]
    return _rs_decode(chunks, k, n)[:frame_len]


def zero_fill(chunks, k, chunk_size, frame_len):
    """Best-effort reassembly of an unrecoverable frame: holes become zeros."""
    hole = b"\0" * chunk_size
    return b"".join(chunks.get(i, hole) for i in range(k))[:frame_len]


# --- reedsolo fallback: one RS codeword per byte column across the chunks ---

def _rs_parity(blocks, r):
    codec = reedsolo.RSCodec(r)
    size = len(blocks[0])
    parity = [bytearray(size) for _ in range(r)]
    for col in range(size):
        msg = bytes(b[col] for b in blocks)
        code = codec.encode(msg)
        for j in range(r):
            parity[j][col] = code[len(blocks) + j]
    return [bytes(p) for p in parity]


def _rs_decode(chunks, k, n):
    codec = reedsolo.RSCodec(n - k)
    size = len(next(iter(chunks.values())))
    erasures = [i for i in range(n) if i not in chunks]
    out = [bytearray(size) for _ in range(k)]
    for col in range(size):
        word = bytearray(chunks[i][col] if i in chunks else 0 for i in range(n))
        msg = codec.decode(word, erase_pos=erasures)[0]
        for i in range(k):
            out[i][col] = msg[i]
    return b"".join(bytes(b) for b in out)
