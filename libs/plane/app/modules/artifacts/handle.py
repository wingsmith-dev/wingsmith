"""The artifact handle: the one name that identifies an artifact, unique across the catalogue.

`slug` is the model id the gateway routes on and collides by design (one repo at two commits is
two artifacts with one slug), and `name` is display text. The handle is what a reference or a
manifest's `metadata.name` uses. It follows the Kubernetes DNS-subdomain rule so it can become
that name unchanged, and it never changes once the artifact exists.

`migrations/versions/1790600000_3b7e5d1c9a42.py` backfills with a frozen copy of `candidates`.
"""

import re
import hashlib


MAX_LENGTH = 253
# Room for `-` and a full sha256 suffix under MAX_LENGTH.
_BASE_LENGTH = 200
PATTERN = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")


def base(slug: str, name: str) -> str:
    """The handle an artifact would get if nothing else held it: its slug, else its name."""
    text = (slug or "").strip().lower() or (name or "").strip().lower()
    text = re.sub(r"[^a-z0-9.]+", "-", text)
    text = re.sub(r"[-.]{2,}", "-", text)
    return text[:_BASE_LENGTH].strip("-.") or "artifact"


def suffixed(handle: str, digest: str, length: int = 8) -> str:
    """`handle` disambiguated by the artifact's digest, which is already unique."""
    return f"{handle}-{hashlib.sha256(digest.encode()).hexdigest()[:length]}"


def candidates(slug: str, name: str, digest: str) -> list[str]:
    """Handles to try in order: the plain one, then longer digest suffixes."""
    plain = base(slug, name)
    return [plain, *(suffixed(plain, digest, n) for n in (8, 16, 64))]


def valid(handle: str) -> bool:
    return len(handle) <= MAX_LENGTH and PATTERN.match(handle) is not None
