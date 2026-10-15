import datetime as dt

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType


class NotificationChannelSchema(Struct):
    """A configured destination for firing alerts.

    A Slack incoming webhook is a bearer credential, so it is never returned. `webhook_hint` is
    the host and the last few characters, enough to tell two webhooks apart; changing the
    webhook means writing a new one.
    """

    id: str
    name: str
    kind: str
    enabled: bool
    min_severity: str
    webhook_hint: str | None
    created_at: dt.datetime
    updated_at: dt.datetime | None


class NotificationChannelCreateRequest(Struct):
    """`webhook_url` is write-only and required for `slack`, the only kind supported."""

    name: str
    webhook_url: str
    kind: str = "slack"
    enabled: bool = True
    min_severity: str = "warning"


class NotificationChannelUpdateRequest(Struct):
    name: str | UnsetType = UNSET
    webhook_url: str | UnsetType = UNSET
    enabled: bool | UnsetType = UNSET
    min_severity: str | UnsetType = UNSET


class NotificationTestResult(Struct):
    """What `POST /{ref}/test` answers: 200, with `delivered: false` and Slack's reason in `detail`
    when the webhook refused the message, since that failure is Slack's and not the plane's.
    """

    delivered: bool
    detail: str | None = None
