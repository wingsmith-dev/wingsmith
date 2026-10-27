"""The narrowest possible Kubernetes client: read a Secret key, patch a Secret key.

The plane is not a Kubernetes controller and must not start looking like one. It
knows nothing about Deployments, rule 2 of the project guide, and the operator is
the only thing that talks to a cluster's API. This module is the single exception,
and it exists for exactly one object: the htpasswd Secret behind the central
metrics store's write ingress, which lives in the plane's *own* cluster and which
the plane is now the source of (ADR 0012).

Hand-rolled over `httpx` instead of adding `kubernetes-asyncio`. The
whole surface is two verbs on one resource kind; a generated client would bring a
model hierarchy, a watch implementation and a discovery cache for that.

Authentication is the in-cluster service account and nothing else: the projected
token at `TOKEN_PATH`, the cluster CA at `CA_PATH`, and the API address from the
environment Kubernetes injects. `in_cluster()` returns `None` when any of the
three is missing, which is what makes local development and the test suite a
no-op instead of a failure; see `app.reactor.remote_write`.
"""

from __future__ import annotations

import os
import base64

from typing import Protocol
from pathlib import Path

import httpx


TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

TIMEOUT = 10.0

MERGE_PATCH = "application/strategic-merge-patch+json"
"""Patch content type used for the write.

A merge patch on `data` sets one key and leaves every other key on the Secret
alone. A full `PUT` would require reading and resending the object, and would
race anything else holding a key there.
"""


class KubernetesError(Exception):
    """Any failure talking to the API server, typed so callers never see httpx.

    The reactor logs and retries on the next tick; nothing about this is worth
    escalating past that, and an `httpx.HTTPStatusError` would carry the bearer
    token's request headers into whatever logged it.
    """


class SecretStore(Protocol):
    """What the reconciler actually needs, so it can be tested without a cluster.

    Two methods, both keyed by namespace/name/key, so the reconciler's render, compare,
    skip-or-write logic is testable with a three-line fake instead of an API server.
    """

    async def read_secret_key(self, namespace: str, name: str, key: str) -> str | None:
        """The decoded value at `key`, or `None` if the Secret or key is absent."""
        ...

    async def patch_secret_key(self, namespace: str, name: str, key: str, value: str) -> None:
        """Sets `key` to `value`, leaving every other key on the Secret untouched."""
        ...


class KubernetesApi:
    """A `SecretStore` backed by the real API server and a service-account token."""

    def __init__(self, base_url: str, token: str, verify: str | bool) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.verify = verify

    def _url(self, namespace: str, name: str) -> str:
        return f"{self.base_url}/api/v1/namespaces/{namespace}/secrets/{name}"

    async def read_secret_key(self, namespace: str, name: str, key: str) -> str | None:
        async with httpx.AsyncClient(verify=self.verify, timeout=TIMEOUT) as client:
            try:
                response = await client.get(
                    self._url(namespace, name),
                    headers={"Authorization": f"Bearer {self.token}"},
                )
            except httpx.HTTPError as error:
                raise KubernetesError(f"GET secret {namespace}/{name} failed: {type(error).__name__}") from None
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise KubernetesError(f"GET secret {namespace}/{name} returned {response.status_code}")
        encoded = (response.json().get("data") or {}).get(key)
        if encoded is None:
            return None
        try:
            return base64.b64decode(encoded).decode()
        except (ValueError, UnicodeDecodeError):
            return None

    async def patch_secret_key(self, namespace: str, name: str, key: str, value: str) -> None:
        payload = {"data": {key: base64.b64encode(value.encode()).decode()}}
        async with httpx.AsyncClient(verify=self.verify, timeout=TIMEOUT) as client:
            try:
                response = await client.patch(
                    self._url(namespace, name),
                    headers={"Authorization": f"Bearer {self.token}", "Content-Type": MERGE_PATCH},
                    json=payload,
                )
            except httpx.HTTPError as error:
                raise KubernetesError(f"PATCH secret {namespace}/{name} failed: {type(error).__name__}") from None
        if response.status_code >= 400:
            raise KubernetesError(f"PATCH secret {namespace}/{name} returned {response.status_code}")


def in_cluster() -> KubernetesApi | None:
    """A client from the ambient service account, or `None` when there is not one.

    Returns, not raises on every missing piece. Outside Kubernetes there is
    no token file and no `KUBERNETES_SERVICE_HOST`, and the correct behaviour for
    the caller is to do nothing at all: a plane that refused to boot without a
    service account would be a plane that cannot run locally.
    """
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not host:
        return None
    try:
        token = TOKEN_PATH.read_text().strip()
    except OSError:
        return None
    if not token:
        return None
    verify: str | bool = str(CA_PATH) if CA_PATH.exists() else True
    return KubernetesApi(base_url=f"https://{host}:{port}", token=token, verify=verify)
