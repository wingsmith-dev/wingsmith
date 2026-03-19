"""How a hub process names itself in the cluster-ownership table.

The claim record (`app.hub.ownership`) needs to say which hub instance holds a
cluster, not only which session. `HUB_INSTANCE_ID` is the escape hatch; the
default follows `HOSTNAME`, which is the pod name under a Kubernetes
`StatefulSet` or `Deployment` and is therefore stable for the life of the pod
without any manifest wiring beyond what already exists. A process with neither
gets a random id instead of a shared constant, because two pods claiming under
the same identity would make a displaced session indistinguishable from the
session that displaced it.
"""

import os
import secrets

from app.config import settings


def instance_id() -> str:
    if settings.HUB_INSTANCE_ID:
        return settings.HUB_INSTANCE_ID
    hostname = os.environ.get("HOSTNAME")
    if hostname:
        return hostname
    return f"hub-{secrets.token_hex(6)}"
