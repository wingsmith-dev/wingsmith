import datetime as dt


def parse_ts(value: str | None) -> dt.datetime | None:
    """Parses a wire timestamp into an aware UTC datetime.

    The wire form is RFC 3339 with milliseconds (`contract/PROTOCOL.md`), but a
    peer that sends microseconds or an offset is not worth rejecting a frame
    over, so `fromisoformat` is the fallback. An unparseable value becomes
    `None` rather than raising: a bad timestamp is not a reason to withhold an
    acknowledgement for a frame whose state is otherwise applicable.
    """
    if not value:
        return None
    try:
        moment = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.UTC)
    except ValueError:
        try:
            moment = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC)


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
