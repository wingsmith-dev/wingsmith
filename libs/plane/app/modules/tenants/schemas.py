import datetime as dt

from typing import Literal
from typing import Annotated

from msgspec import Meta

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType


# msgspec matches with `re.search`, and Python's `$` also matches before a trailing
# newline; the lookahead keeps "acme\n" from passing as "acme".
DNS_LABEL = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$(?!\n)"
HOSTNAME = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*$(?!\n)"

Slug = Annotated[
    str,
    Meta(
        min_length=1,
        max_length=63,
        pattern=DNS_LABEL,
        description="A DNS label. Tenant slugs name a login and a gateway host; project slugs follow the same rule.",
    ),
]
Name = Annotated[str, Meta(min_length=1, max_length=200)]
Host = Annotated[
    str,
    Meta(
        min_length=1,
        max_length=253,
        pattern=HOSTNAME,
        description="The lowercase hostname the gateway's public listener matches against `Host`.",
    ),
]
Ingress = Literal["internal", "public"]
Auth = Literal["none", "api_key"]
CredentialName = Annotated[str, Meta(max_length=100)]


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
    ingress: str
    auth: str
    host: str | None
    project_count: int
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str


class TenantCreateRequest(Struct):
    """A new tenant. `auth` defaults to `api_key`: a tenant that wants no key
    check says so, and if it is reachable on the public listener the plane
    must also allow that (`TENANT_ALLOW_PUBLIC_NO_AUTH`)."""

    slug: Slug
    name: Name
    enabled: bool = True
    ingress: Ingress = "internal"
    auth: Auth = "api_key"
    host: Host | None = None


class TenantUpdateRequest(Struct):
    """A partial update. An absent field is untouched.

    `slug` is absent by design: it is the tenant's handle, it namespaces every
    project slug beneath it, and renaming it would move projects without moving
    anything that points at them.
    """

    name: Name | UnsetType = UNSET
    enabled: bool | UnsetType = UNSET
    ingress: Ingress | UnsetType = UNSET
    auth: Auth | UnsetType = UNSET
    host: Host | None | UnsetType = UNSET


class TenantAPIKeySchema(Struct):
    """An inference credential for one tenant, as it reads back after minting.

    `key` carries the plaintext exactly once, on the response to the mint, and
    is null on every later read — same shape as `TenantTokenSchema.token`, and
    for the same reason: only the sha256 is ever stored.
    """

    id: str
    tenant: str
    name: str
    key: str | None
    last_used_at: dt.datetime | None
    revoked_at: dt.datetime | None
    created_at: dt.datetime


class TenantAPIKeyCreateRequest(Struct):
    """`name` is for the human who has to decide later whether to revoke it."""

    name: CredentialName = "default"


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

    name: CredentialName = "console"
