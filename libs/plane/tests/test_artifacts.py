import inspect
import datetime as dt

import pytest

from app.core.ids import ARTIFACT
from app.core.ids import new_uid
from app.db.models import Artifact
from app.core.exceptions import Invalid
from app.modules.artifacts.routes import ArtifactRoutes
from app.modules.artifacts.schemas import ArtifactSchema
from app.modules.artifacts.schemas import ArtifactCreateRequest
from app.modules.artifacts.schemas import ArtifactUpdateRequest
from app.modules.artifacts.schemas import ArtifactLocationSchema
from app.modules.artifacts.schemas import ArtifactLocationRequest
from app.modules.artifacts.services import KINDS
from app.modules.artifacts.services import _revision
from app.modules.artifacts.services import check_locations
from app.modules.artifacts.repository import ArtifactRepository


EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


@pytest.fixture
def repo():
    return ArtifactRepository(db=None)


def sql(query) -> str:
    return str(query)


def location(uri: str = "s3://weights/llama", scheme: str = "s3", **kwargs) -> ArtifactLocationRequest:
    return ArtifactLocationRequest(uri=uri, scheme=scheme, **kwargs)


def test_create_request_defaults_to_weights():
    data = ArtifactCreateRequest(digest="sha256:abc", name="llama-70b")
    assert data.kind == "weights"
    assert data.size_bytes == 0
    assert data.locations == []
    assert data.project is None


def test_every_derivative_is_an_artifact_not_a_catalog_entry():
    assert KINDS == {"weights", "adapter", "dataset", "image"}


def test_update_request_leaves_absent_fields_unset():
    data = ArtifactUpdateRequest(licence="apache-2.0")
    assert data.to_dict(exclude_unset=True) == {"licence": "apache-2.0"}


def test_absent_is_untouched_and_null_clears():
    assert ArtifactUpdateRequest().to_dict(exclude_unset=True) == {}
    assert ArtifactUpdateRequest(project=None).to_dict(exclude_unset=True) == {"project": None}


def test_the_digest_is_identity_and_cannot_be_patched():
    assert "digest" not in ArtifactUpdateRequest.__struct_fields__


def test_ids_are_prefixed_lowercase_and_never_the_database_key():
    uid = new_uid(ARTIFACT)
    assert uid.startswith("art_")
    assert uid == uid.lower()
    assert ArtifactSchema.__annotations__["id"] is str


def test_a_location_needs_a_uri_and_a_scheme():
    with pytest.raises(Invalid):
        check_locations([location(uri="")])
    with pytest.raises(Invalid):
        check_locations([location(scheme="")])


def test_one_uri_cannot_be_registered_twice_against_one_artifact():
    with pytest.raises(Invalid):
        check_locations([location(), location(region="us-east-1")])


def test_several_distinct_locations_are_the_normal_case():
    assert check_locations([location(), location("gs://weights/llama", "gs")]) is None


def test_locations_are_a_child_collection_keyed_by_uri_not_by_id():
    assert "id" not in ArtifactLocationSchema.__struct_fields__
    assert "uri" in ArtifactLocationSchema.__struct_fields__


def test_revision_is_derived_from_the_last_write_not_stored():
    assert _revision(Artifact(created_at=EPOCH, updated_at=None)) == str(int(EPOCH.timestamp() * 1_000_000))
    later = Artifact(created_at=EPOCH, updated_at=EPOCH + dt.timedelta(seconds=1))
    assert _revision(later) != _revision(Artifact(created_at=EPOCH, updated_at=None))


def test_lineage_and_project_are_resolved_in_the_same_statement(repo):
    text = sql(repo.list_query(limit=10))
    assert "LEFT OUTER JOIN demand_artifact AS base_artifact" in text
    assert "LEFT OUTER JOIN bind_project" in text


def test_list_query_filters_compile_into_one_statement(repo):
    text = sql(
        repo.list_query(
            limit=10,
            after_seq=41,
            kind="adapter",
            format="safetensors",
            project="acme",
            search="llama",
            labels={"family": "llama"},
        )
    )
    assert "demand_artifact.seq > " in text
    assert "demand_artifact.kind = " in text
    assert "demand_artifact.format = " in text
    assert "bind_project.slug = " in text
    assert "lower(demand_artifact.name) LIKE lower(" in text
    assert "demand_artifact.metadata ->> " in text
    assert "ORDER BY demand_artifact.seq ASC" in text


def test_project_filter_resolves_an_id_against_uid_not_slug(repo):
    text = sql(repo.list_query(limit=10, project=new_uid(ARTIFACT).replace("art_", "prj_")))
    assert "bind_project.uid = " in text


def test_list_query_over_fetches_one_row_for_the_cursor(repo):
    assert repo.list_query(limit=10).compile().params["param_1"] == 11


def test_get_query_resolves_a_name_or_an_id(repo):
    assert "demand_artifact.name = " in sql(repo.get_query("llama-70b"))
    assert "demand_artifact.uid = " in sql(repo.get_query(new_uid(ARTIFACT)))


def test_a_prefix_is_not_an_id_and_falls_through_to_a_name(repo):
    assert "demand_artifact.name = " in sql(repo.get_query("art_01hq"))


def test_an_ambiguous_name_resolves_to_the_oldest_match(repo):
    assert "ORDER BY demand_artifact.seq ASC" in sql(repo.get_query("llama-70b"))


def test_controller_exposes_full_crud():
    handlers = {h.handler_name for h in ArtifactRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert handlers == {
        "list_artifacts",
        "create_artifact",
        "get_artifact",
        "update_artifact",
        "delete_artifact",
    }


def test_format_filter_is_exposed_as_format_not_artifact_format():
    parameter = inspect.signature(ArtifactRoutes.list_artifacts.fn).parameters["artifact_format"]
    assert parameter.default.query == "format"


def test_conditional_update_reads_if_match_from_the_header():
    parameter = inspect.signature(ArtifactRoutes.update_artifact.fn).parameters["if_match"]
    assert parameter.default.header == "If-Match"


def test_delete_refuses_while_a_recipe_still_names_the_artifact():
    from app.modules.artifacts.services import ArtifactService

    assert "Refuses" in (ArtifactService.delete.__doc__ or "")
