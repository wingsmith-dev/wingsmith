import base64
import binascii

from app.core.exceptions import Invalid


DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if limit < 1:
        raise Invalid("limit must be positive", {"limit": limit})
    return min(limit, MAX_LIMIT)


def encode_cursor(seq: int) -> str:
    """Wraps a row's `seq` in an opaque token.

    Opaque so that the pagination key can change without becoming a breaking API
    change, and so that a client cannot construct one by hand and depend on the
    shape of it.
    """
    return base64.urlsafe_b64encode(str(seq).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int | None:
    if cursor is None:
        return None
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        return int(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, binascii.Error) as exc:
        raise Invalid("cursor is not valid", {"cursor": cursor}) from exc


def page[T](rows: list[T], limit: int, seq_of) -> tuple[list[T], str | None]:
    """Trims an over-fetched page and mints the cursor for the next one.

    Callers select `limit + 1` rows; the extra row is how we know another page
    exists without a second count query.
    """
    if len(rows) <= limit:
        return rows, None
    kept = rows[:limit]
    return kept, encode_cursor(seq_of(kept[-1]))
