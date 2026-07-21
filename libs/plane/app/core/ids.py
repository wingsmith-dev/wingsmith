import os
import time
import threading


CLUSTER = "clu_"
NODE = "nod_"
DEVICE = "dev_"
TOPOLOGY_DOMAIN = "tpd_"
POOL = "pol_"
ARTIFACT = "art_"
DEPLOYMENT = "dep_"
REPLICA = "rep_"
ENDPOINT = "end_"
PROJECT = "prj_"
QUOTA = "qta_"
OPERATION = "opr_"
COMPONENT = "cmp_"
TENANT = "ten_"
PRIORITY_CLASS = "prc_"
POLICY = "plc_"
ALLOCATION = "alc_"
WORKLOAD = "wkl_"
WORKLOAD_REVISION = "wkr_"
EVENT = "evt_"
DISCOVERED_WORKLOAD = "dsw_"
ALERT = "alr_"
NOTIFICATION_CHANNEL = "nch_"
BENCHMARK_RUN = "bmk_"
BENCHMARK_REQUEST = "bmr_"
ARTIFACT_BASELINE = "abl_"
ALERT_THRESHOLD = "alt_"
SERVING_QUOTA = "sqt_"
TENANT_TOKEN = "tkn_"
TENANT_API_KEY = "tak_"

PREFIXES = frozenset(
    {
        CLUSTER,
        NODE,
        DEVICE,
        TOPOLOGY_DOMAIN,
        POOL,
        ARTIFACT,
        DEPLOYMENT,
        REPLICA,
        ENDPOINT,
        PROJECT,
        QUOTA,
        OPERATION,
        COMPONENT,
        TENANT,
        PRIORITY_CLASS,
        POLICY,
        ALLOCATION,
        WORKLOAD,
        WORKLOAD_REVISION,
        EVENT,
        DISCOVERED_WORKLOAD,
        ALERT,
        NOTIFICATION_CHANNEL,
        BENCHMARK_RUN,
        BENCHMARK_REQUEST,
        ARTIFACT_BASELINE,
        ALERT_THRESHOLD,
        SERVING_QUOTA,
        TENANT_TOKEN,
        TENANT_API_KEY,
    }
)

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"

_lock = threading.Lock()
_last: tuple[int, int] = (0, 0)


def _encode(value: int, length: int) -> str:
    out = [""] * length
    for i in range(length - 1, -1, -1):
        out[i] = _ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(out)


def new_uid(prefix: str) -> str:
    """Mints a prefixed, lexicographically sortable identifier.

    The body is a lowercase Crockford base32 ULID: 48 bits of millisecond
    timestamp then 80 bits of randomness. Sortable because a list ordered by id
    and a list ordered by creation are then the same list, which removes a whole
    class of pagination bug. Two ids minted inside one millisecond would break
    that, so within a millisecond the random half is incremented rather than
    redrawn.
    """
    if prefix not in PREFIXES:
        raise ValueError(f"unknown id prefix: {prefix!r}")

    global _last
    with _lock:
        now = int(time.time() * 1000)
        last_ms, last_rand = _last
        if now == last_ms:
            rand = last_rand + 1
        elif now < last_ms:
            now, rand = last_ms, last_rand + 1
        else:
            rand = int.from_bytes(os.urandom(10), "big")
        _last = (now, rand)

    return f"{prefix}{_encode(now, 10)}{_encode(rand, 16)}"


def is_uid(value: str) -> bool:
    """Whether a path segment is an identifier rather than a name.

    Names are free text and could in principle start with anything, so this
    checks the prefix and the shape of the body, not the prefix alone.
    """
    if len(value) != 30 or value[:4] not in PREFIXES:
        return False
    return all(c in _ALPHABET for c in value[4:])
