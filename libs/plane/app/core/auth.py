from app.config import settings
from app.utils.schemas import Struct


class Principal(Struct):
    """The identity of a caller.

    Auth is stubbed: there is exactly one principal in the system today. The type
    exists so that handlers are written against an identity from the start and
    real authentication can be added behind `resolve_principal` without touching
    a single call site.
    """

    id: str
    kind: str
    name: str
    scopes: tuple[str, ...] = ()


STATIC_OPERATOR = Principal(
    id="00000000-0000-0000-0000-000000000001",
    kind="operator",
    name="operator",
    scopes=("*",),
)


def bearer_token(header: str | None) -> str | None:
    """Extracts the credential from an Authorization header, if there is one."""
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value or None


def resolve_principal(token: str | None) -> Principal:
    """Resolves a bearer token to a caller identity.

    TODO: replace the static-token comparison with real credential lookup. Until
    then every accepted token resolves to the same principal and an absent token
    is accepted too, so local development needs no credentials.
    """
    if token is not None and token != settings.OPERATOR_TOKEN:
        return STATIC_OPERATOR
    return STATIC_OPERATOR
