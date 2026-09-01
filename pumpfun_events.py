"""pumpfun_events.py - Decode pump.fun Anchor events straight out of the
`Program data:` log lines, so token discovery needs ZERO extra RPC calls.

Why this exists
---------------
The old discovery path was: logsSubscribe -> getTransaction(sig) -> heuristically
guess which account key is the new mint. That cost one RPC round-trip per
notification and the guess was fragile (it scanned preTokenBalances, then fell
back to "the last account key").

Every pump.fun instruction emits an Anchor event, and Anchor writes those into
the transaction logs as `Program data: <base64>`. The payload is
`discriminator(8) || borsh(fields)`, where the discriminator is
`sha256("event:<EventName>")[:8]`. So a token creation is fully described by the
log frame we already receive - name, symbol, uri and the mint pubkey included.
No getTransaction, no guessing.

Borsh subset needed here: String = u32 LE byte-length + UTF-8 bytes,
Pubkey = 32 raw bytes (base58 on the way out).

Reference: pump.fun IDL, CreateEvent { name, symbol, uri, mint, bondingCurve,
user, ... }. Trailing fields vary between program revisions, so we decode only
the prefix we need and ignore whatever follows.
"""
import base64
import hashlib
import struct

# base58 (bitcoin alphabet) - avoids a dependency just for pubkey formatting
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

LOG_DATA_PREFIX = "Program data: "


def _discriminator(event_name: str) -> bytes:
    return hashlib.sha256(f"event:{event_name}".encode()).digest()[:8]


CREATE_EVENT = _discriminator("CreateEvent")
TRADE_EVENT = _discriminator("TradeEvent")
COMPLETE_EVENT = _discriminator("CompleteEvent")
SET_PARAMS_EVENT = _discriminator("SetParamsEvent")

# Events we recognise and can therefore dismiss without fetching the tx.
KNOWN_EVENTS = frozenset(
    (CREATE_EVENT, TRADE_EVENT, COMPLETE_EVENT, SET_PARAMS_EVENT)
)


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = _B58[rem] + out
    # each leading zero byte encodes as a single '1'
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


class _Reader:
    """Minimal Borsh cursor. Raises ValueError on truncated/implausible data."""

    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes):
        self.buf, self.pos = buf, 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.buf):
            raise ValueError("borsh: out of bounds")
        chunk = self.buf[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def string(self, cap: int = 512) -> str:
        (n,) = struct.unpack("<I", self.take(4))
        if n > cap:  # guards against reading garbage as a huge length
            raise ValueError(f"borsh: string length {n} exceeds cap {cap}")
        return self.take(n).decode("utf-8", "replace")

    def pubkey(self) -> str:
        return b58encode(self.take(32))


def iter_program_data(logs):
    """Yield every decoded base64 `Program data:` payload from a log line list."""
    for line in logs or []:
        if not line.startswith(LOG_DATA_PREFIX):
            continue
        blob = line[len(LOG_DATA_PREFIX):].strip()
        try:
            yield base64.b64decode(blob + "=" * (-len(blob) % 4))  # tolerate no padding
        except Exception:
            continue


def decode_create_event(payload: bytes) -> dict | None:
    """Return {name, symbol, uri, mint, bonding_curve, user} or None.

    None means "not a CreateEvent" or "malformed" - callers treat both the same.
    """
    if len(payload) < 8 or payload[:8] != CREATE_EVENT:
        return None
    r = _Reader(payload[8:])
    try:
        name, symbol, uri = r.string(), r.string(), r.string(2048)
        mint, bonding_curve, user = r.pubkey(), r.pubkey(), r.pubkey()
    except ValueError:
        return None
    return {
        "name": name,
        "symbol": symbol,
        "uri": uri,
        "mint": mint,
        "bonding_curve": bonding_curve,
        "user": user,
    }


def extract_new_mint(logs) -> dict | None:
    """Scan a logsNotification's log lines for a token creation.

    Returns the decoded CreateEvent (mint included) or None if this transaction
    did not create a token. This is the whole discovery step - no RPC needed.
    """
    for payload in iter_program_data(logs):
        ev = decode_create_event(payload)
        if ev and ev["mint"]:
            return ev
    return None


def has_unknown_event(logs) -> bool:
    """True if a `Program data:` blob carries a discriminator we do not know.

    Callers use this to decide whether a frame is worth an RPC fallback: a
    recognised non-create event (a trade, a curve completion) is definitively
    *not* a launch, so fetching the transaction would be wasted. An unknown tag
    means the program was upgraded and we should not silently miss the launch.
    """
    return any(
        len(p) >= 8 and p[:8] not in KNOWN_EVENTS for p in iter_program_data(logs)
    )
