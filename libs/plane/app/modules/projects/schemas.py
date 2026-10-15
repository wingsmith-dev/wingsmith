import datetime as dt

from typing import Annotated

from msgspec import Meta

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType
from app.modules.tenants.schemas import Name
from app.modules.tenants.schemas import Slug


Ref = Annotated[str, Meta(min_length=1, max_length=64)]
"""An id or a slug/name of something that already exists; looked up, never stored."""


class ProjectTenantSchema(Struct):
    """The tenant a project belongs to, embedded rather than linked.

    Embedded so that no reader of a project has to join for the two facts it always
    needs — which tenant, and whether that tenant is switched on. `/v1/tenants` is
    the collection; this is the copy that travels with the project.
    """

    id: str
    slug: str
    name: str
    enabled: bool


class ProjectSchema(Struct):
    """A project: the unit quota and priority hang off, beneath a tenant.

    `enabled` is declared on both halves of the hierarchy and they are not folded
    together: a project inside a disabled tenant is still a project someone
    enabled, and `effective_enabled` answers the scheduling question without
    losing which of the two switches is off.
    """

    id: str
    tenant: ProjectTenantSchema
    slug: str
    name: str
    default_priority_class: str | None
    enabled: bool
    effective_enabled: bool
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str


class ProjectCreateRequest(Struct):
    tenant: Ref
    slug: Slug
    name: Name
    default_priority_class: Ref | None = None
    enabled: bool = True


class ProjectUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    `tenant` and `slug` are absent by design. A project's slug is what every
    quota row points at, and moving a project between
    tenants would move its billing without moving its allocations.
    """

    name: Name | UnsetType = UNSET
    default_priority_class: Ref | None | UnsetType = UNSET
    enabled: bool | UnsetType = UNSET
