import datetime as dt

from typing import Literal
from typing import Annotated

from msgspec import Meta

from app.core.schemas import Struct


NAME = r"^[a-z0-9]([-a-z0-9]{0,126}[a-z0-9])?$"
"""A credential name: a DNS label, so it reads like any other object name."""

SecretRef = Annotated[str, Meta(min_length=1, max_length=253, pattern=r"^[a-z0-9]([-.a-z0-9]*[a-z0-9])?$")]
"""The name of a Secret an admin applied in the operator's namespace on each cluster. Never a value."""


class CredentialSchema(Struct):
    """A project's credential: what it is for and where its secret lives, never the secret."""

    name: str
    project: str
    kind: str
    secret_ref: str
    expires_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime | None


class CredentialPutRequest(Struct):
    """Declares a credential by name, creating or replacing it.

    `kind` is `hf_token`: a Hugging Face token the operator mounts on the prefetch job of the
    project's private artifacts. The Secret `secret_ref` names must carry the token under
    `HF_TOKEN` and the label `wingsmith.dev/project=<project id with - for _>`.
    """

    kind: Literal["hf_token"]
    secret_ref: SecretRef
    expires_at: dt.datetime | None = None
