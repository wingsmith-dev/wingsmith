"""The operator install one-liner, served by the hub.

A freshly registered cluster has a row in the plane and nothing running in it.
Closing that gap used to mean the person onboarding had the repo, `just`, and a
kube-context named after the cluster. This serves the install instead: a
short-lived, single-cluster bootstrap token (`app.core.auth.issue_install_token`)
authorises fetching a small shell script and, through it, a rendered operator
manifest carrying the runtime credential — so nobody ever handles the fleet
secret by hand.

It lives on the hub because the hub owns the one surface that is already public
(`infra/70-hub-ingress.yaml`); the API has no ingress. The hub answers these
paths as plain HTTP ahead of the WebSocket upgrade — see
`app.hub.server._process_request`. There is no request database session here, so
the manifest is rendered from a template baked into the image and the cluster's
identity comes from the token, not a lookup.
"""

import http
import pathlib

from logging import getLogger

from app.config import settings
from app.core.auth import verify_install_token
from app.core.auth import effective_operator_token


logger = getLogger(__name__)

_TEMPLATE_PATH = pathlib.Path(__file__).parent / "manifests" / "operator.yaml.tmpl"
_TEMPLATE = _TEMPLATE_PATH.read_text()

_PREFIX = "/install/"


class InstallUnavailable(Exception):
    """The install endpoint is reachable but cannot render — e.g. no pinned image."""


def _public_host(request_host: str) -> str:
    """The host used to build operator-facing URLs.

    Configured `PUBLIC_HOST` wins so a command minted behind a port-forward
    still names the real origin; otherwise the request's own `Host` header,
    which is what local development and tests present.
    """
    return settings.PUBLIC_HOST or request_host


def _hub_url(request_host: str) -> str:
    if settings.HUB_PUBLIC_URL:
        return settings.HUB_PUBLIC_URL
    return f"wss://{_public_host(request_host)}{settings.HUB_PATH}"


def render_manifest(cluster: str, request_host: str) -> str:
    """The operator manifest for `cluster`, ready to `kubectl apply -f -`.

    Substitutes the four values that vary — image, cluster id, hub url, and the
    runtime enrolment token — into the baked template. The token is injected
    into the Secret so the operator boots with a credential the human never saw.
    """
    if not settings.OPERATOR_IMAGE:
        raise InstallUnavailable("OPERATOR_IMAGE is not configured")
    out = _TEMPLATE
    out = out.replace("${OPERATOR_IMAGE}", settings.OPERATOR_IMAGE)
    out = out.replace("${CLUSTER_ID}", cluster)
    out = out.replace("${HUB_URL}", _hub_url(request_host))
    out = out.replace("${ENROLMENT_TOKEN}", effective_operator_token())
    return out


def render_script(token: str, request_host: str) -> str:
    """The `curl … | sh` bootstrap that fetches and applies the manifest.

    Carries no secret itself: it re-fetches the manifest over TLS with the same
    bootstrap token, pipes it straight to `kubectl`, and writes nothing to disk.
    """
    manifest_url = f"https://{_public_host(request_host)}{_PREFIX}{token}/operator.yaml"
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        "# Hangar operator installer. Applies the rendered manifest for this cluster\n"
        "# against whatever cluster kubectl is currently pointed at.\n"
        f'echo "hangar: applying operator manifest to $(kubectl config current-context)"\n'
        f'curl -fsSL "{manifest_url}" | kubectl apply -f -\n'
        'echo "hangar: applied. watch it come up with:"\n'
        'echo "  kubectl -n hangar-system rollout status deploy/hangar-operator"\n'
    )


def install_response(connection, path: str, request_host: str):
    """Answers the two install paths, or returns `None` if `path` is not one.

    Returned to `websockets`' `process_request` hook as a plain-HTTP `Response`;
    `None` lets the caller fall through to its own routing (health, the hub
    upgrade, or 404).
    """
    if not path.startswith(_PREFIX):
        return None
    rest = path[len(_PREFIX) :]

    if rest.endswith("/operator.yaml"):
        token = rest[: -len("/operator.yaml")]
        cluster = verify_install_token(token)
        if cluster is None:
            return connection.respond(http.HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        try:
            body = render_manifest(cluster, request_host)
        except InstallUnavailable as exc:
            logger.warning("hub.install.unavailable cluster=%s reason=%s", cluster, exc)
            return connection.respond(http.HTTPStatus.SERVICE_UNAVAILABLE, f"{exc}\n")
        return connection.respond(http.HTTPStatus.OK, body)

    if rest.endswith(".sh"):
        token = rest[: -len(".sh")]
        cluster = verify_install_token(token)
        if cluster is None:
            return connection.respond(http.HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        return connection.respond(http.HTTPStatus.OK, render_script(token, request_host))

    return connection.respond(http.HTTPStatus.NOT_FOUND, "not found\n")
