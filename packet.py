"""Wire formats for the media and control channels.

Media packets (UDP, binary, one FEC chunk each)::

    0      1      2      3      4                8                12
    +------+------+------+------+----------------+----------------+
    |magic | type |flags | rsv  |   frame_id     |   src_index    |
    +------+------+------+------+-------+--------+-------+--------+
    | chunk_index | k           | total_chunks  | redundancy     |
    +-------------+-------------+---------------+----------------+
    |  frame_len (u32)          |  send_timestamp (f64, seconds) |
    +---------------------------+--------------------------------+
    |                   chunk payload (chunk_size bytes)          |

Control packets (UDP, small JSON objects) carry loss feedback and the
ping/pong exchange used to estimate the clock offset between peers.
"""

import json
import struct

MAGIC = 0x56          # 'V'
TYPE_MEDIA = 1

# flags: bits 0-1 = FEC mode of the sending peer, bit 2 = src_index is valid
FEC_MODES = ("off", "fixed", "adaptive")
FLAG_HAS_TRUTH = 0x04

HEADER = struct.Struct("!BBBBIIHHHHId")
HEADER_SIZE = HEADER.size   # 32 bytes
NO_SOURCE = 0xFFFFFFFF


class MediaHeader:
    __slots__ = ("flags", "frame_id", "src_index", "chunk_index", "k",
                 "total_chunks", "redundancy", "frame_len", "send_ts")

    def __init__(self, flags, frame_id, src_index, chunk_index, k,
                 total_chunks, redundancy, frame_len, send_ts):
        self.flags = flags
        self.frame_id = frame_id
        self.src_index = src_index
        self.chunk_index = chunk_index
        self.k = k
        self.total_chunks = total_chunks
        self.redundancy = redundancy
        self.frame_len = frame_len
        self.send_ts = send_ts

    @property
    def fec_mode(self):
        return FEC_MODES[self.flags & 0x03]

    @property
    def has_truth(self):
        return bool(self.flags & FLAG_HAS_TRUTH)


def make_flags(fec_mode, has_truth):
    return FEC_MODES.index(fec_mode) | (FLAG_HAS_TRUTH if has_truth else 0)


def pack_media(h, payload):
    return HEADER.pack(MAGIC, TYPE_MEDIA, h.flags, 0, h.frame_id, h.src_index,
                       h.chunk_index, h.k, h.total_chunks, h.redundancy,
                       h.frame_len, h.send_ts) + payload


def unpack_media(data):
    """Return (MediaHeader, payload) or None if the datagram is not ours."""
    if len(data) < HEADER_SIZE:
        return None
    (magic, ptype, flags, _rsv, frame_id, src_index, chunk_index, k,
     total, r, frame_len, send_ts) = HEADER.unpack_from(data)
    if magic != MAGIC or ptype != TYPE_MEDIA:
        return None
    return (MediaHeader(flags, frame_id, src_index, chunk_index, k, total, r,
                        frame_len, send_ts),
            data[HEADER_SIZE:])


def pack_control(msg):
    return b"C" + json.dumps(msg, separators=(",", ":")).encode()


def unpack_control(data):
    if not data.startswith(b"C"):
        return None
    try:
        return json.loads(data[1:])
    except ValueError:
        return None
