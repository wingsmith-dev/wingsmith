import datetime as dt

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType


class ProjectTenantSchema(Struct):
    """The tenant a project belongs to, embedded rather than linked.

    `libs/packages/contract/API.md` §6 has no `/v1/tenants` collection, so the tenant is a field
    on the project instead of a resource of its own. Inventing the collection here
    would put a shape in the API that no client was promised.
    """

    id: str
    slug: str
    name: str
    enabled: bool


class ProjectSchema(Struct):
    """A project: the tenancy unit `X-Hangar-Project` names.

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
    tenant: str
    slug: str
    name: str
    default_priority_class: str | None = None
    enabled: bool = True


class ProjectUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    `tenant` and `slug` are absent by design. A project's slug is what
    `X-Hangar-Project` and every quota row point at, and moving a project between
    tenants would move its billing without moving its allocations.
    """

    name: str | UnsetType = UNSET
    default_priority_class: str | None | UnsetType = UNSET
    enabled: bool | UnsetType = UNSET
