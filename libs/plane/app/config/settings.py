import os

from typing import Any
from typing import get_args
from typing import get_origin
from dataclasses import fields
from dataclasses import dataclass


ENV_PREFIX = "HANGAR_"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass
class Settings:
    """Typed process configuration, loaded from the environment.

    Every field is read from `HANGAR_<FIELD>` first and then from the bare
    `<FIELD>` name, so standard variables such as `DATABASE_URL` work unprefixed.
    """

    APP_ENV: str = "local"
    APP_DEBUG: bool = False
    APP_LOG_LEVEL: str = "INFO"
    APP_LOG_FORMAT: str = "console"

    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000

    HUB_HOST: str = "0.0.0.0"
    HUB_PORT: int = 8001
    HUB_PATH: str = "/v1/hub/sync"

    DATABASE_URL: str = "postgresql+psycopg://hangar:hangar@localhost:5442/hangar"
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 10
    DB_POOL_MAX_OVERFLOW: int = 10
    DB_POOL_PRE_PING: bool = True

    OPERATOR_TOKEN: str = "hangar-local-operator-token"
    """Single static bearer token accepted by both processes.

    Auth is deliberately stubbed: there is one caller identity in the system and
    this token names it. The seam is `app.core.auth.resolve_principal`.
    """


def _coerce(raw: str, target: Any) -> Any:
    if get_origin(target) is not None:
        args = [a for a in get_args(target) if a is not type(None)]
        target = args[0] if args else str
    if target is bool:
        lowered = raw.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise ValueError(f"cannot read {raw!r} as a boolean")
    if target is int:
        return int(raw)
    if target is float:
        return float(raw)
    return raw


def load(env: dict[str, str] | None = None) -> Settings:
    """Builds a Settings instance from the given environment mapping."""
    source = os.environ if env is None else env
    values: dict[str, Any] = {}
    for spec in fields(Settings):
        raw = source.get(f"{ENV_PREFIX}{spec.name}") or source.get(spec.name)
        if raw is None or raw == "":
            continue
        values[spec.name] = _coerce(raw, spec.type)
    return Settings(**values)


settings = load()
