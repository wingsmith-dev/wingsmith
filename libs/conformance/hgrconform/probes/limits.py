"""Rate limiting: whether the endpoint shapes bursts, and how it says so.

This probe observes the *shape* of the rate-limit policy and never searches for its
ceiling. Finding the ceiling is a load question, it belongs to the benchmark suite,
and an endpoint serving real traffic must not be pushed into refusal by a
conformance run. So the burst is small, fixed, and made of the cheapest requests the
profile allows.

Nothing counted here is comparable except as a shape. Which requests succeeded, how
many, and how fast, all differ between two honest runs against one endpoint. What
does not differ is whether the endpoint throttles at all, which rate-limit headers it
publishes, and whether it says when to retry — so those are what the diff sees, and
the rest is evidence.
"""

from __future__ import annotations

import re

from typing import Any
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait

from hgrconform.session import CHAT_PATH
from hgrconform.session import Session
from hgrconform.probes.registry import probe


BURST_REQUESTS = 20
BURST_CONCURRENCY = 10
BURST_MAX_TOKENS = 4
RATE_LIMIT_HEADER = re.compile(r"^(x-ratelimit-.*|ratelimit-.*|retry-after)$")


def _rate_limit_headers(headers: dict[str, str]) -> list[str]:
    """The names of the rate-limit headers, never their values.

    A remaining-quota value differs on every run and between any two endpoints. The
    set of header names is the policy the provider publishes, and that is stable.
    """
    return sorted(name.lower() for name in headers if RATE_LIMIT_HEADER.match(name.lower()))


def _status_classes(statuses: list[int]) -> list[str]:
    """The kinds of answer the burst met, not the codes themselves.

    An exact code set drifts for reasons that are not policy — one run catches a
    transient 503, another a 502 from a proxy in front of the same endpoint — and a
    diff on that is noise. Whether the burst met success, refusal, or server error
    is the comparable answer; the raw codes stay in `observed` as evidence.
    """
    classes = set()
    for status in statuses:
        if 200 <= status < 300:
            classes.add("2xx")
        elif 400 <= status < 500:
            classes.add("4xx")
        elif 500 <= status < 600:
            classes.add("5xx")
        else:
            classes.add("other")
    return sorted(classes)


@probe(
    id="limits.burst",
    title="What a short burst of concurrent requests meets",
    requirement="Whether burst or ramp-up limits are applied, and whether 429 follows the rate limit",
    compare=(
        "throttled",
        "rate_limit_headers",
        "retry_after_on_throttle",
        "status_classes",
    ),
)
def burst(session: Session) -> dict[str, Any]:
    """Send a small fixed burst and record what came back, as shapes not counts.

    How many of the burst succeeded, how fast, and whether one connection timed out
    are properties of the run and of the machine it ran on, so they are kept as
    evidence and not compared. Whether any request was refused, which rate-limit
    headers the endpoint publishes, whether a refusal carried `Retry-After`, and the
    kinds of answer the burst met are properties of the policy.
    """
    body = session.body(max_tokens=BURST_MAX_TOKENS)

    statuses: list[int] = []
    header_names: set[str] = set()
    throttle_headers: set[str] = set()
    errors: list[str] = []
    tally = Lock()

    def send() -> None:
        response = session.post(CHAT_PATH, body)
        names = _rate_limit_headers(response.headers)
        with tally:
            statuses.append(response.status)
            header_names.update(names)
            if response.status == 0:
                errors.append(response.text.split(":")[0])
            if response.status == 429:
                throttle_headers.update(names)

    with ThreadPoolExecutor(max_workers=BURST_CONCURRENCY) as pool:
        wait([pool.submit(send) for _ in range(BURST_REQUESTS)])

    throttled = 429 in statuses
    return {
        "throttled": throttled,
        "rate_limit_headers": sorted(header_names),
        "retry_after_on_throttle": "retry-after" in throttle_headers if throttled else None,
        "status_classes": _status_classes(statuses),
        "status_codes": sorted(set(statuses)),
        "transport_errors": sorted(set(errors)),
        "requests_sent": BURST_REQUESTS,
        "concurrency": BURST_CONCURRENCY,
        "accepted_count": sum(1 for status in statuses if 200 <= status < 300),
        "throttled_count": sum(1 for status in statuses if status == 429),
    }
