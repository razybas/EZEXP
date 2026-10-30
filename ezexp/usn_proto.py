"""Named-pipe protocol between the EzExp app and the USN helper service.

Shared by ezexp/usn_client.py (app side) and usn_helper_main.py (helper
side); imports ONLY the standard library. Framing is a 4-byte
little-endian length followed by UTF-8 JSON. Every message is a dict
with a "type" key from the fixed vocabulary below:

  app  -> helper:  subscribe{drive,start_usn}  enum{drive}  ping
  helper -> app:   ready{drives:{drive:probe}}
                   journal_info{drive,journal_id,first_usn,next_usn}
                   enum_batch{drive,rows:[[frn,parent_frn,name],...]}
                   enum_done{drive,journal_id,next_usn}
                   records{drive,records:[[frn,parent_frn,usn,reason,
                                           attrs,name],...],next_usn}
                   stale{drive}   error{drive,error}
"""
import json
import struct

PIPE_NAME = "\\\\.\\pipe\\EzExpUsn.v1"

APP_TYPES = ("subscribe", "enum", "ping")
HELPER_TYPES = ("ready", "journal_info", "enum_batch", "enum_done",
                "records", "stale", "error")
ALL_TYPES = APP_TYPES + HELPER_TYPES

MAX_FRAME = 32 << 20  # refuses absurd lengths on corrupt streams


def frame(msg):
    """Encode one message dict as 4-byte LE length + UTF-8 JSON."""
    payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_FRAME:
        raise ValueError("message too large: %d bytes" % len(payload))
    return struct.pack("<I", len(payload)) + payload


class FrameDecoder:
    """Incremental stream decoder: feed() raw bytes, pop_messages() the
    complete messages decoded so far."""

    def __init__(self):
        self._buf = b""

    def feed(self, data):
        self._buf += data

    def pop_messages(self):
        out = []
        while len(self._buf) >= 4:
            (n,) = struct.unpack_from("<I", self._buf, 0)
            if n > MAX_FRAME:
                raise ValueError("frame length %d exceeds limit" % n)
            if len(self._buf) < 4 + n:
                break
            msg = json.loads(self._buf[4:4 + n].decode("utf-8"))
            if not isinstance(msg, dict) or msg.get("type") not in ALL_TYPES:
                raise ValueError("bad protocol message: %r" % (msg,))
            out.append(msg)
            self._buf = self._buf[4 + n:]
        return out


def parse_frame(buf):
    """Decode a single complete frame (tests / one-shot reads)."""
    dec = FrameDecoder()
    dec.feed(buf)
    msgs = dec.pop_messages()
    if len(msgs) != 1 or dec._buf:
        raise ValueError("not exactly one frame in buffer")
    return msgs[0]


# --- message builders --------------------------------------------------------

def m_ping():
    return {"type": "ping"}


def m_subscribe(drive, start_usn):
    return {"type": "subscribe", "drive": drive, "start_usn": int(start_usn)}


def m_enum(drive):
    return {"type": "enum", "drive": drive}


def m_ready(drives):
    return {"type": "ready", "drives": dict(drives)}


def m_journal_info(drive, info):
    return {"type": "journal_info", "drive": drive,
            "journal_id": int(info.journal_id),
            "first_usn": int(info.first_usn), "next_usn": int(info.next_usn)}


def m_enum_batch(drive, rows):
    return {"type": "enum_batch", "drive": drive,
            "rows": [[int(f), int(p), n] for f, p, n in rows]}


def m_enum_done(drive, journal_id, next_usn):
    return {"type": "enum_done", "drive": drive,
            "journal_id": int(journal_id), "next_usn": int(next_usn)}


def m_records(drive, records, next_usn):
    rows = []
    for r in records:
        if hasattr(r, "frn"):  # USNRecord namedtuple
            rows.append([r.frn, r.parent_frn, r.usn, r.reason, r.attrs, r.name])
        else:  # already a [frn, parent_frn, usn, reason, attrs, name] row
            f, p, u, rs, a, nm = r
            rows.append([f, p, u, rs, a, nm])
    return {"type": "records", "drive": drive, "records": rows,
            "next_usn": int(next_usn)}


def m_stale(drive):
    return {"type": "stale", "drive": drive}


def m_error(drive, error):
    return {"type": "error", "drive": drive, "error": str(error)}
