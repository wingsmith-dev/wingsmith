"""Delivery. Slack today, and a seam shaped so the next kind is a function, not a rewrite.

Stdlib HTTP for the same reason `app.modules.metrics.client` is stdlib: the plane
carries no HTTP client dependency, and `urllib` on a worker thread costs one
thread per webhook call and nothing else. This runs on the reactor's tick, not on
a request, so the thread is not on anyone's critical path.

Nothing here raises into the loop. A webhook that is wrong, revoked, or rate
limited must not stop conditions from being evaluated: the alert is already
durable in `ops_alert` and the console shows it either way. A delivery failure is
recorded on the alert (`notify_error`) and retried on the next tick, because the
notified marker is only advanced on success.
"""

import json
import asyncio
import urllib.error
import urllib.request

from typing import Any
from logging import getLogger
from dataclasses import dataclass

from app.db.models import Alert
from app.db.models import NotificationChannel

from .rules import at_least


logger = getLogger(__name__)

SLACK = "slack"
KINDS = frozenset({SLACK})

RESOLVED = "resolved"

EMOJI = {"info": ":information_source:", "warning": ":warning:", "error": ":rotating_light:", "critical": ":fire:"}


class DeliveryFailed(Exception):
    """A channel did not accept the message. Caught by the loop, never by the caller of a route."""


@dataclass
class Message:
    """What a channel is asked to deliver, already flattened out of the alert.

    A struct instead of the `Alert` row so that rendering is testable without a
    database, and so a future channel kind cannot quietly start depending on
    columns the alert happens to carry.
    """

    severity: str
    state: str
    title: str
    rule: str
    cluster: str | None
    subject: str | None
    details: dict


def message_for(alert: Alert, cluster_name: str | None) -> Message:
    return Message(
        severity=alert.severity,
        state=alert.state,
        title=alert.title,
        rule=alert.rule,
        cluster=cluster_name,
        subject=alert.subject_uid,
        details=alert.details or {},
    )


def slack_payload(message: Message) -> dict[str, Any]:
    """Blocks, not `text`, so the severity and the subject survive Slack's notification preview.

    The fallback `text` is set as well and is not optional: it is what a mobile
    push and a screen reader get, and a payload with only blocks reaches both as
    an empty notification.
    """
    resolved = message.state == RESOLVED
    icon = ":white_check_mark:" if resolved else EMOJI.get(message.severity, ":warning:")
    headline = f"{icon} {'Resolved' if resolved else message.severity.upper()}: {message.title}"
    context = [f"*rule* `{message.rule}`"]
    if message.cluster:
        context.append(f"*cluster* `{message.cluster}`")
    if message.subject:
        context.append(f"*subject* `{message.subject}`")
    for key, value in sorted(message.details.items()):
        context.append(f"*{key}* `{value}`")
    return {
        "text": headline,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": headline}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "  ·  ".join(context)}]},
        ],
    }


def _post(url: str, payload: dict, timeout: float) -> None:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


async def deliver(channel: NotificationChannel, message: Message, timeout: float) -> None:
    """Sends one message to one channel, raising `DeliveryFailed` on anything but success."""
    if channel.kind != SLACK:
        raise DeliveryFailed(f"unknown channel kind {channel.kind!r}")
    url = (channel.config or {}).get("webhook_url")
    if not url:
        raise DeliveryFailed("channel has no webhook_url")
    try:
        await asyncio.to_thread(_post, url, slack_payload(message), timeout)
    except urllib.error.HTTPError as error:
        raise DeliveryFailed(f"slack answered {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise DeliveryFailed(str(error)) from error


def routed_to(channels: list[NotificationChannel], alert: Alert) -> list[NotificationChannel]:
    """Which channels want this alert.

    A resolution is routed by the severity of the alert being resolved, not by
    some severity of its own: a channel that was told about a `critical` firing
    and then not told it recovered is worse than one that was never told at all.
    """
    return [
        channel
        for channel in channels
        if channel.enabled and channel.kind in KINDS and at_least(alert.severity, channel.min_severity)
    ]
