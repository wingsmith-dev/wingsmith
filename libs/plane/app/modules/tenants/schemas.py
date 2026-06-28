import datetime as dt

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType


class TenantSchema(Struct):
    """The billing and isolation root. Projects hang off it, quota attaches below it.

    `project_count` is on the read shape because the only destructive question
    anyone asks of a tenant is what deleting it would take with it, and the
    foreign key from `bind_project` cascades.
    """

    id: str
    slug: str
    name: str
    enabled: bool
    project_count: int
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str


class TenantCreateRequest(Struct):
    slug: str
    name: str
    enabled: bool = True


class TenantUpdateRequest(Struct):
    """A partial update. An absent field is untouched.

    `slug` is absent by design: it is the tenant's handle, it namespaces every
    project slug beneath it, and renaming it would move projects without moving
    anything that points at them.
    """

    name: str | UnsetType = UNSET
    enabled: bool | UnsetType = UNSET


class TenantTokenSchema(Struct):
    """A console credential for one tenant, as it reads back after minting.

    `token` carries the plaintext exactly once, on the response to the mint, and
    is null on every later read. There is nowhere it could come from afterwards —
    only the sha256 is stored — and a field that is sometimes the secret is
    clearer than two shapes for one object.
    """

    id: str
    tenant: str
    name: str
    token: str | None
    last_used_at: dt.datetime | None
    revoked_at: dt.datetime | None
    created_at: dt.datetime


class TenantTokenCreateRequest(Struct):
    """`name` is for the human who has to decide later whether to revoke it."""

    name: str = "console"
