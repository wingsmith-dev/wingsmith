import datetime as dt

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType


class PolicyAttachmentSchema(Struct):
    """One subject a policy binds to.

    `subject` is the public reference — a prefixed id where the subject type has
    one, and the tenant slug where it does not. `subject_id` is the raw key and
    is carried alongside because `PolicyAttachment.subject_id` is polymorphic and
    has no foreign key, so a subject that has since been deleted still has a row
    here and would otherwise serialise as a subject with no identity at all.
    """

    subject_type: str
    subject_id: str
    subject: str | None
    subject_name: str | None


class PolicyAttachmentRequest(Struct):
    subject_type: str
    subject: str


class PolicySchema(Struct):
    """A placement constraint and everything it currently binds to.

    Allow-lists and deny-lists are separate fields rather than one signed set:
    an empty allow-list means "no opinion", not "nothing allowed", and a denial
    has to survive somebody widening the allow-list later.

    `attachments` is on the detail response because a policy with no subjects
    constrains nothing, and that is the first thing anyone asks about one.
    """

    id: str
    name: str
    description: str | None
    allowed_regions: list[str]
    allowed_residencies: list[str]
    allowed_device_classes: list[str]
    allowed_clusters: list[str]
    denied_clusters: list[str]
    max_hourly_cost: float | None
    rules: dict
    enabled: bool
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str
    attachments: list[PolicyAttachmentSchema]


class PolicyCreateRequest(Struct):
    name: str
    description: str | None = None
    allowed_regions: list[str] = []
    allowed_residencies: list[str] = []
    allowed_device_classes: list[str] = []
    allowed_clusters: list[str] = []
    denied_clusters: list[str] = []
    max_hourly_cost: float | None = None
    rules: dict = {}
    enabled: bool = True
    attachments: list[PolicyAttachmentRequest] = []


class PolicyUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    `attachments`, when present, is the complete set and replaces what is there.
    An empty list detaches everything; leaving the field out leaves the bindings
    alone. Per-attachment add and remove would be a nested sub-resource, and
    `contract/API.md` §6 addresses sub-resources by filter rather than by nesting.
    """

    name: str | UnsetType = UNSET
    description: str | None | UnsetType = UNSET
    allowed_regions: list[str] | UnsetType = UNSET
    allowed_residencies: list[str] | UnsetType = UNSET
    allowed_device_classes: list[str] | UnsetType = UNSET
    allowed_clusters: list[str] | UnsetType = UNSET
    denied_clusters: list[str] | UnsetType = UNSET
    max_hourly_cost: float | None | UnsetType = UNSET
    rules: dict | UnsetType = UNSET
    enabled: bool | UnsetType = UNSET
    attachments: list[PolicyAttachmentRequest] | UnsetType = UNSET
