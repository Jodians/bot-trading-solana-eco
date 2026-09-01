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

# pump.fun program id (constant on mainnet). Lives here rather than in
# ws_listener because blob attribution (below) needs it.
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Every other event the program is known to emit. None of these is a launch, so
# recognising them keeps has_unknown_event() from paying for a getTransaction.
# The original four were not enough: sampling live pump.fun transactions turned
# up ExtendAccountEvent and CloseUserVolumeAccumulatorEvent among them, and with
# only four tags recognised 83% of event-carrying frames looked "possibly a
# launch" - which is what produced 7358 `enrich queue full` lines in one run.
_OTHER_EVENT_NAMES = (
    "ExtendAccountEvent",
    "CloseUserVolumeAccumulatorEvent",
    "SyncUserVolumeAccumulatorEvent",
    "CompletePumpAmmMigrationEvent",
    "CollectCreatorFeeEvent",
    "SetCreatorEvent",
    "SetMetaplexCreatorEvent",
    "UpdateGlobalAuthorityEvent",
    "AdminSetCreatorEvent",
    "AdminUpdateTokenIncentivesEvent",
    "ClaimTokenIncentivesEvent",
)

# Tags observed live, repeatedly, in transactions that created nothing, but whose
# event name we could not recover (a discriminator is a one-way hash; 1950
# candidate names were brute-forced without a match). Listing the raw tag is
# still correct: the ONLY purpose of KNOWN_EVENTS is "definitely not a launch,
# do not spend an RPC call". A real CreateEvent decodes directly, so a stable,
# high-frequency non-create tag belongs here regardless of its name.
_OBSERVED_NON_CREATE_TAGS = (
    bytes.fromhex("e2d6f62107f293e5"),  # 25 occurrences, 0 creates
)

# Events we recognise and can therefore dismiss without fetching the tx.
KNOWN_EVENTS = frozenset(
    (CREATE_EVENT, TRADE_EVENT, COMPLETE_EVENT, SET_PARAMS_EVENT)
    + tuple(_discriminator(n) for n in _OTHER_EVENT_NAMES)
    + _OBSERVED_NON_CREATE_TAGS
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


def iter_pumpfun_program_data(logs):
    """Yield only the `Program data:` payloads pump.fun itself emitted.

    Solana interleaves inner CPI programs into one flat log list, so a router, a
    fee hook or the pump AMM can drop its own event blob into a pump.fun
    transaction. To has_unknown_event() such a blob is indistinguishable from an
    unrecognised pump.fun event, and it then buys a getTransaction call for a tx
    that created nothing.

    Attribution walks the `Program <id> invoke [depth]` / `Program <id> success`
    markers to track which program is executing. A blob emitted while no program
    is on the stack cannot be attributed, so it is yielded (conservative: better
    to pay for one RPC call than to miss a launch).
    """
    stack: list[str] = []
    for line in logs or []:
        if line.startswith("Program ") and " invoke [" in line:
            parts = line.split()
            if len(parts) >= 2:
                stack.append(parts[1])
            continue
        if line.startswith("Program ") and (
            line.endswith(" success") or " failed" in line
        ):
            if stack:
                stack.pop()
            continue
        if not line.startswith(LOG_DATA_PREFIX):
            continue
        if stack and stack[-1] != PUMP_PROGRAM:
            continue  # someone else's event, riding along in this transaction
        blob = line[len(LOG_DATA_PREFIX):].strip()
        try:
            yield base64.b64decode(blob + "=" * (-len(blob) % 4))
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
    """True if this frame *might* be a launch we failed to decode.

    Used to decide whether a frame is worth an RPC fallback. Three ways to be
    sure it is not worth one:

      * the blob belongs to a DIFFERENT program - inner CPI programs (routers,
        fee hooks, the pump AMM) write their own event data into the same flat
        log list, and those tell us nothing about a pump.fun launch. Only blobs
        attributed to pump.fun are considered.
      * it carries a recognised event (TradeEvent / CompleteEvent / SetParams /
        the account-maintenance events) - those are definitively not launches.
        This is the common case by a wide margin.
      * every blob is recognised - nothing to resolve.

    Only a frame whose pump.fun blobs are *entirely* unrecognised suggests the
    program was upgraded and a launch could be hiding in it.
    """
    payloads = [p for p in iter_pumpfun_program_data(logs) if len(p) >= 8]
    if not payloads:
        return False
    return all(p[:8] not in KNOWN_EVENTS for p in payloads)
