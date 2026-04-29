"""Agent token verification: the gateway asking the plane who just dialled it.

The refusals are the interesting half. A serving agent presents the cluster's
enrolment token to the gateway's tunnel listener, the gateway asks here, and an
answer that is wrong in the permissive direction hands a stranger a cluster's
identity on the data plane. So every path that must refuse has a test: a token
nobody issued, the value a rotation replaced, a cluster somebody disabled, and a
caller that is not the gateway.
"""

import time

import pytest

from litestar.di import Provide
from litestar.testing import create_test_client

from app.core.auth import STATIC_OPERATOR
from app.core.auth import admin_principal
from app.core.auth import mint_enrolment_token
from app.core.context import RequestContext
from app.core.handlers import EXCEPTION_HANDLERS
from app.core.throttle import FailureBudget
from app.core.exceptions import NotAuthorized
from app.core.exceptions import TooManyRequests
from app.modules.clusters.routes import ClusterRoutes
from app.modules.clusters.schemas import ClusterCreateRequest
from app.modules.clusters.schemas import ClusterUpdateRequest
from app.modules.clusters.services import TUNNEL_AUTH_GUESSES
from app.modules.clusters.services import ClusterService


@pytest.fixture(autouse=True)
def budget():
    """The failure budget is process-wide, so a test that spends it must not leak it."""
    TUNNEL_AUTH_GUESSES.clear()
    yield
    TUNNEL_AUTH_GUESSES.clear()


def client(principal):
    context = RequestContext(principal=principal, db=None)
    return create_test_client(
        route_handlers=[ClusterRoutes],
        dependencies={"context": Provide(lambda: context, sync_to_thread=False)},
        exception_handlers=EXCEPTION_HANDLERS,
    )


# --- the seam over HTTP -------------------------------------------------------


def test_the_route_is_exposed():
    handlers = {h.handler_name for h in ClusterRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert "verify_tunnel_token" in handlers


def test_a_verified_token_is_answered_with_a_200():
    """A POST defaults to `201`, and `201` refuses every tunnel in the fleet.

    `internal/plane/tunnel.go` treats anything it was not promised as "the plane
    could not be reached", so the whole route answering `201` reads at the
    agent as an unavailable control plane and every dial is refused with no
    mention of a status code anywhere. Nothing is created here; the status is part
    of the seam.
    """
    assert ClusterRoutes.verify_tunnel_token.status_code == 200


def test_an_admin_principal_is_refused():
    """A human credential that can turn tokens into identities can sweep for them."""
    token, _ = mint_enrolment_token()
    with client(admin_principal()) as http:
        response = http.post("/clusters/tunnel-auth", json={"token": token})
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_a_token_that_is_not_enrolment_shaped_is_refused_without_a_lookup():
    """`db=None` in the context is the assertion: nothing reached the database."""
    with client(STATIC_OPERATOR) as http:
        response = http.post("/clusters/tunnel-auth", json={"token": "some-fleet-token"})
    assert response.status_code == 401
    assert response.json()["code"] == "not_authorized"


# --- the failure budget -------------------------------------------------------


async def test_a_run_of_guesses_is_rate_limited():
    service = ClusterService(db=None)
    for _ in range(TUNNEL_AUTH_GUESSES.limit):
        with pytest.raises(NotAuthorized):
            await service.verify_tunnel_token("junk")
    with pytest.raises(TooManyRequests):
        await service.verify_tunnel_token("junk")


def test_the_budget_forgets_failures_older_than_its_window():
    budget = FailureBudget(limit=2, window_seconds=60.0)
    budget._failures.extend([time.monotonic() - 120, time.monotonic() - 90])
    assert not budget.spent()


def test_the_budget_refuses_once_the_window_is_full():
    budget = FailureBudget(limit=2, window_seconds=60.0)
    budget.fail()
    assert not budget.spent()
    budget.fail()
    assert budget.spent()
    budget.clear()
    assert not budget.spent()


# --- resolution, against a real database --------------------------------------


@pytest.fixture
async def svc(sessions):
    async with sessions() as db:
        yield ClusterService(db)


@pytest.mark.database
async def test_a_valid_token_resolves_to_its_own_cluster(svc):
    alpha = await svc.create(ClusterCreateRequest(name="alpha", region="eu-west-1"))
    beta = await svc.create(ClusterCreateRequest(name="beta", region="us-east-1"))
    alpha_token = (await svc.issue_enrolment_token(alpha.id, host="hangar.example")).enrolment_token
    beta_token = (await svc.issue_enrolment_token(beta.id, host="hangar.example")).enrolment_token

    identity = await svc.verify_tunnel_token(alpha_token)
    assert identity.cluster_id == alpha.id
    assert identity.cluster_name == "alpha"

    assert (await svc.verify_tunnel_token(beta_token)).cluster_id == beta.id


@pytest.mark.database
async def test_a_token_nobody_issued_is_refused(svc):
    created = await svc.create(ClusterCreateRequest(name="alpha", region="eu-west-1"))
    await svc.issue_enrolment_token(created.id, host="hangar.example")

    stranger, _ = mint_enrolment_token()
    with pytest.raises(NotAuthorized):
        await svc.verify_tunnel_token(stranger)


@pytest.mark.database
async def test_the_value_a_rotation_replaced_is_refused(svc):
    created = await svc.create(ClusterCreateRequest(name="alpha", region="eu-west-1"))
    old = (await svc.issue_enrolment_token(created.id, host="hangar.example")).enrolment_token
    new = (await svc.issue_enrolment_token(created.id, host="hangar.example")).enrolment_token

    assert (await svc.verify_tunnel_token(new)).cluster_id == created.id
    with pytest.raises(NotAuthorized):
        await svc.verify_tunnel_token(old)


@pytest.mark.database
async def test_a_disabled_cluster_is_refused_and_costs_nothing(svc):
    """Its retry loop must not spend the budget every other cluster's dials need."""
    created = await svc.create(ClusterCreateRequest(name="alpha", region="eu-west-1"))
    token = (await svc.issue_enrolment_token(created.id, host="hangar.example")).enrolment_token
    await svc.update(created.id, ClusterUpdateRequest(phase="disabled"))

    for _ in range(TUNNEL_AUTH_GUESSES.limit + 1):
        with pytest.raises(NotAuthorized):
            await svc.verify_tunnel_token(token)
    assert not TUNNEL_AUTH_GUESSES.spent()


@pytest.mark.database
async def test_a_draining_cluster_still_gets_its_tunnel(svc):
    """Draining removes upstreams from the resolution; the tunnel carries what is in flight."""
    created = await svc.create(ClusterCreateRequest(name="alpha", region="eu-west-1"))
    token = (await svc.issue_enrolment_token(created.id, host="hangar.example")).enrolment_token
    await svc.update(created.id, ClusterUpdateRequest(phase="draining"))

    assert (await svc.verify_tunnel_token(token)).cluster_id == created.id
