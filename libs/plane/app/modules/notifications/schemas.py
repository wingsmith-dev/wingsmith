import datetime as dt

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType


class NotificationChannelSchema(Struct):
    """A configured destination for firing alerts.

    `webhook_hint` where a `webhook_url` might be expected, and that is the whole
    point of it: a Slack incoming webhook is a bearer credential wearing a URL's
    clothes, and anyone who can read it can post into the channel forever. The hint
    is the host and the last few characters — enough to tell two webhooks apart in a
    list, not enough to use. There is no route that returns the real value; changing
    it means writing a new one.
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
    """`webhook_url` is write-only and required for `slack`, which is the only kind today."""

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
    """What `POST /{ref}/test` answers.

    A 200 with `delivered: false` rather than a 5xx, because the failure being
    reported is Slack's and not the plane's — and because the reason is the useful
    part. A caller pasting a webhook in wants to be told `slack answered 404`, not
    handed an error page about Hangar.
    """

    delivered: bool
    detail: str | None = None
