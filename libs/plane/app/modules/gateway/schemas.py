from app.core.schemas import Struct


class GatewayTenantSchema(Struct):
    """One tenant, as the gateway needs to trust it (docs/decisions/0016).

    `key_hashes` carries only the live ones: a revoked key is dropped from
    here, and that drop is the whole mechanism a revoke has. It still moves
    `GatewayTenantTableSchema.revision`, so a gateway sitting behind a `304`
    never keeps honouring a hash somebody just revoked.
    """

    id: str
    slug: str
    enabled: bool
    ingress: str
    auth: str
    host: str | None
    key_hashes: list[str]


class GatewayTenantTableSchema(Struct):
    """Every enabled tenant, resolved for the gateway. Unpaginated, like the
    routing table it sits beside: a caller that holds this in memory needs the
    whole thing or none of it. A disabled tenant is absent, not flagged."""

    revision: str
    tenants: list[GatewayTenantSchema]
