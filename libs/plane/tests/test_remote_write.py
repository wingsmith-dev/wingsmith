"""The htpasswd the plane is now the source of, and the pass that writes it.

The failure this guards is not a wrong file — it is a per-cluster manual step
coming back. So the tests here are about the three ways that could happen
quietly: a cluster omitted from the render, a write skipped when it was needed
(or made when it was not), and a reconciler that decides it is not configured and
says nothing about it.
"""

import logging

import bcrypt
import pytest

from sqlalchemy import text

from app.config import settings
from app.reactor import remote_write
from app.auth import REMOTE_WRITE_BCRYPT_ROUNDS
from app.auth import AuthManager
from app.auth import AuthManager

from tests.conftest import seed_cluster


class FakeSecrets:
    """A `SecretStore` over a dict, counting the writes it is asked to make."""

    def __init__(self, value: str | None = None) -> None:
        self.value = value
        self.patches: list[str] = []

    async def read_secret_key(self, namespace: str, name: str, key: str) -> str | None:
        return self.value

    async def patch_secret_key(self, namespace: str, name: str, key: str, value: str) -> None:
        self.patches.append(value)
        self.value = value


class BrokenSecrets(FakeSecrets):
    async def read_secret_key(self, namespace: str, name: str, key: str) -> str | None:
        raise remote_write.KubernetesError("the API server said no")


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "REMOTE_WRITE_AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "REMOTE_WRITE_AUTH_NAMESPACE", "platform")
    monkeypatch.setattr(settings, "REMOTE_WRITE_AUTH_SECRET", "prom-remote-write-auth")
    monkeypatch.setattr(settings, "REMOTE_WRITE_AUTH_KEY", "users")
    monkeypatch.setattr(remote_write, "_announced", False)


def test_htpasswd_is_one_line_per_cluster_holding_a_hash():
    out = remote_write.render_htpasswd([("alpha", "$2b$08$aaa"), ("beta", "$2b$08$bbb")])
    assert out == "alpha:$2b$08$aaa\nbeta:$2b$08$bbb\n"


def test_htpasswd_omits_clusters_with_no_hash():
    """A cluster that never issued a token has no credential to authenticate with."""
    out = remote_write.render_htpasswd([("alpha", "$2b$08$aaa"), ("beta", None), ("gamma", "")])
    assert out == "alpha:$2b$08$aaa\n"


def test_htpasswd_is_ordered_by_cluster_name_whatever_the_row_order():
    """Determinism is what makes the unchanged comparison mean anything."""
    forwards = remote_write.render_htpasswd([("alpha", "one"), ("beta", "two"), ("gamma", "three")])
    backwards = remote_write.render_htpasswd([("gamma", "three"), ("alpha", "one"), ("beta", "two")])
    assert forwards == backwards
    assert forwards.splitlines() == ["alpha:one", "beta:two", "gamma:three"]


def test_htpasswd_of_an_empty_fleet_is_empty():
    assert remote_write.render_htpasswd([]) == ""


async def test_reconcile_is_a_noop_when_the_flag_is_off(monkeypatch, caplog):
    monkeypatch.setattr(settings, "REMOTE_WRITE_AUTH_ENABLED", False)
    monkeypatch.setattr(remote_write, "_announced", False)
    caplog.set_level(logging.INFO, logger=remote_write.__name__)
    store = FakeSecrets()

    assert await remote_write.reconcile(db=None, store=store) is False
    assert await remote_write.reconcile(db=None, store=store) is False
    assert store.patches == []
    assert len([r for r in caplog.records if "remote_write.inactive" in r.getMessage()]) == 1


async def test_reconcile_is_a_noop_when_the_secret_is_unnamed(monkeypatch):
    monkeypatch.setattr(settings, "REMOTE_WRITE_AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "REMOTE_WRITE_AUTH_SECRET", "")
    monkeypatch.setattr(remote_write, "_announced", False)
    store = FakeSecrets()

    assert await remote_write.reconcile(db=None, store=store) is False
    assert store.patches == []


async def test_reconcile_is_a_noop_with_no_service_account(monkeypatch):
    """Local dev and tests have no projected token; that must not be a failure."""
    monkeypatch.setattr(settings, "REMOTE_WRITE_AUTH_ENABLED", True)
    monkeypatch.setattr(remote_write, "_announced", False)
    monkeypatch.setattr(remote_write, "in_cluster", lambda: None)

    assert await remote_write.reconcile(db=None) is False


def test_bcrypt_hash_verifies_against_the_token_that_minted_it():
    token, _ = AuthManager.mint_enrolment_token()
    digest = AuthManager.hash_remote_write_token(token)
    assert bcrypt.checkpw(token.encode(), digest.encode())
    assert not bcrypt.checkpw(b"another-token", digest.encode())


def test_bcrypt_cost_is_the_one_the_decision_records():
    """Traefik verifies this on every remote-write request; the cost is a choice."""
    digest = AuthManager.hash_remote_write_token("hgro1.whatever")
    assert digest.startswith(f"$2b${REMOTE_WRITE_BCRYPT_ROUNDS:02d}$")


@pytest.mark.database
async def test_reconcile_writes_once_and_skips_an_unchanged_fleet(sessions, enabled):
    """The fleet changes on register and rotate; every other tick must not write."""
    await seed_cluster(sessions, "clu_01j9x2bbbbbbbbbbbbbbbbbbbb", "alpha")
    async with sessions.begin() as db:
        await db.execute(text("UPDATE supply_cluster SET remote_write_hash = '$2b$08$aaa' WHERE name = 'alpha'"))

    store = FakeSecrets()
    async with sessions() as db:
        assert await remote_write.reconcile(db, store) is True
        assert store.patches == ["alpha:$2b$08$aaa\n"]

        assert await remote_write.reconcile(db, store) is False
        assert len(store.patches) == 1


@pytest.mark.database
async def test_reconcile_writes_again_once_the_fleet_changes(sessions, enabled):
    await seed_cluster(sessions, "clu_01j9x2bbbbbbbbbbbbbbbbbbbb", "alpha")
    async with sessions.begin() as db:
        await db.execute(text("UPDATE supply_cluster SET remote_write_hash = '$2b$08$aaa' WHERE name = 'alpha'"))

    store = FakeSecrets()
    async with sessions() as db:
        await remote_write.reconcile(db, store)

    await seed_cluster(sessions, "clu_01j9x2cccccccccccccccccccc", "beta")
    async with sessions.begin() as db:
        await db.execute(text("UPDATE supply_cluster SET remote_write_hash = '$2b$08$bbb' WHERE name = 'beta'"))

    async with sessions() as db:
        assert await remote_write.reconcile(db, store) is True
    assert store.patches[-1] == "alpha:$2b$08$aaa\nbeta:$2b$08$bbb\n"


@pytest.mark.database
async def test_reconcile_leaves_the_file_alone_when_the_api_server_fails(sessions, enabled):
    await seed_cluster(sessions, "clu_01j9x2bbbbbbbbbbbbbbbbbbbb", "alpha")
    store = BrokenSecrets(value="stale:$2b$08$zzz\n")

    async with sessions() as db:
        assert await remote_write.reconcile(db, store) is False
    assert store.patches == []
    assert store.value == "stale:$2b$08$zzz\n"
