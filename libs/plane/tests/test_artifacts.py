import uuid
import decimal
import inspect
import datetime as dt

import pytest

from sqlalchemy.exc import IntegrityError

from app.core import devices
from app.core.ids import ARTIFACT
from app.core.ids import ARTIFACT_BASELINE
from app.core.ids import new_uid
from app.core.vram import extract_shape
from app.core.vram import shape_metadata
from app.db.models import Artifact
from app.db.models import ArtifactBaseline
from app.db.models import ArtifactLocation
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.exceptions import PlaneError
from app.integrations.huggingface import HuggingFaceGated
from app.integrations.huggingface import HuggingFaceModel
from app.integrations.huggingface import HuggingFaceNotFound
from app.integrations.huggingface import HuggingFaceWeightFile
from app.integrations.huggingface import HuggingFaceUnavailable
from app.integrations.huggingface import content_digest
from app.modules.artifacts.routes import ArtifactRoutes
from app.modules.artifacts.resolve import ServingDefaults
from app.modules.artifacts.resolve import slug_of
from app.modules.artifacts.resolve import resolved
from app.modules.artifacts.resolve import normalise_source
from app.modules.artifacts.schemas import ArtifactSchema
from app.modules.artifacts.schemas import ArtifactUsageSchema
from app.modules.artifacts.schemas import ArtifactCreateRequest
from app.modules.artifacts.schemas import ArtifactUpdateRequest
from app.modules.artifacts.schemas import ArtifactFitmentRequest
from app.modules.artifacts.schemas import ArtifactLocationSchema
from app.modules.artifacts.schemas import ArtifactLocationRequest
from app.modules.artifacts.schemas import ArtifactUpstreamRequest
from app.modules.artifacts.schemas import ArtifactBaselineCreateRequest
from app.modules.artifacts.schemas import ArtifactBaselineUpdateRequest
from app.modules.artifacts.services import KINDS
from app.modules.artifacts.services import DEFAULT_FITMENT_LIMIT
from app.modules.artifacts.services import ArtifactService
from app.modules.artifacts.services import ArtifactBaselineService
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
        "resolve_artifact",
        "artifact_usage",
        "artifact_fitment",
        "refresh_artifact",
        "list_artifact_baselines",
        "create_artifact_baseline",
        "update_artifact_baseline",
        "delete_artifact_baseline",
    }


def test_base_artifact_filter_selects_the_derived_children(repo):
    text = sql(repo.list_query(limit=10, base_artifact="llama-70b"))
    assert "demand_artifact.base_artifact_id IN" in text


def test_downstream_count_is_children_that_name_this_artifact_as_base(repo):
    text = sql(repo.downstream_query(Artifact(uid=new_uid(ARTIFACT))))
    assert "count(demand_artifact.id)" in text
    assert "demand_artifact.base_artifact_id" in text


def test_workload_usage_unions_the_served_weights_and_the_adapter_set(repo):
    """One count over both ways a workload can reach these weights.

    The serving configuration is a column on the workload now, so the weights it
    serves are `demand_workload.artifact_id` and there is no second table to join
    through. A workload naming the artifact as both still counts once.
    """
    text = sql(repo.workload_usage_query(Artifact(uid=new_uid(ARTIFACT))))
    assert "count(distinct(demand_workload.id))" in text
    assert "demand_workload.artifact_id" in text
    assert "demand_workload.adapter_artifact_ids @>" in text


def test_usage_schema_carries_the_two_derived_counts():
    """One kind of dependant, not two: nothing stands between a workload and the weights."""
    assert set(ArtifactUsageSchema.__struct_fields__) == {
        "artifact_id",
        "workload_count",
        "downstream_count",
    }


def test_format_filter_is_exposed_as_format_not_artifact_format():
    parameter = inspect.signature(ArtifactRoutes.list_artifacts.fn).parameters["artifact_format"]
    assert parameter.default.query == "format"


def test_conditional_update_reads_if_match_from_the_header():
    parameter = inspect.signature(ArtifactRoutes.update_artifact.fn).parameters["if_match"]
    assert parameter.default.header == "If-Match"


class RestrictedDelete:
    """A session and repository whose delete trips the RESTRICT foreign key a workload holds."""

    def __init__(self) -> None:
        self.rolled_back = False
        self.committed = False

    async def delete(self, row) -> None:
        from sqlalchemy.exc import IntegrityError

        raise IntegrityError("DELETE", {}, Exception("still referenced"))

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


async def test_delete_refuses_while_a_workload_still_names_the_artifact():
    from app.core.exceptions import Conflict
    from app.modules.artifacts.services import ArtifactService

    fake = RestrictedDelete()
    service = ArtifactService(db=fake)
    service.repo = fake

    async def one(ref):
        return object(), None, None

    service._one = one

    with pytest.raises(Conflict, match="still referenced by a workload"):
        await service.delete("llama")
    assert fake.rolled_back and not fake.committed


GPT_OSS_CONFIG = {
    "architectures": ["GptOssForCausalLM"],
    "model_type": "gpt_oss",
    "torch_dtype": "bfloat16",
    "num_hidden_layers": 24,
    "num_attention_heads": 64,
    "num_key_value_heads": 8,
    "head_dim": 64,
    "hidden_size": 2880,
    "max_position_embeddings": 131072,
    "num_local_experts": 32,
    "num_experts_per_tok": 4,
    "intermediate_size": 2880,
    "quantization_config": {"quant_method": "mxfp4"},
}


def hf_model(**kwargs) -> HuggingFaceModel:
    weights = [
        HuggingFaceWeightFile(path="model-00001-of-00002.safetensors", size=8_000_000_000, oid="a" * 64),
        HuggingFaceWeightFile(path="model-00002-of-00002.safetensors", size=2_400_000_000, oid="b" * 64),
    ]
    defaults = {
        "slug": "openai/gpt-oss-20b",
        "revision": "c" * 40,
        "digest": content_digest(weights),
        "content_addressable": True,
        "size_bytes": sum(w.size for w in weights),
        "parameter_count": 20_914_757_632,
        "format": "safetensors",
        "licence": "apache-2.0",
        "gated": False,
        "config": GPT_OSS_CONFIG,
        "weight_files": tuple(weights),
        "unaddressable_files": (),
    }
    return HuggingFaceModel(**{**defaults, **kwargs})


class FakeHub:
    """Stands in for the Hub so no test touches the network."""

    def __init__(self, model=None, error=None):
        self.model = model
        self.error = error
        self.slugs = []

    async def inspect(self, slug: str):
        self.slugs.append(slug)
        if self.error is not None:
            raise self.error
        return self.model or hf_model()


class FakeRow:
    def __init__(self, artifact):
        self._artifact = artifact
        self.base_uid = None
        self.project_uid = None

    def __getitem__(self, index):
        return self._artifact


class FakeRepo:
    def __init__(self, existing_digest: str | None = None, largest_device_mib: int | None = None):
        self.existing_digest = existing_digest
        self.added = None
        self.largest_device_mib = largest_device_mib

    async def largest_device_memory_mib(self):
        """What the fleet actually holds. `None` is a plane with no cluster enrolled yet."""
        return self.largest_device_mib

    async def artifact_by_digest(self, digest: str):
        if digest == self.existing_digest:
            return Artifact(uid="art_01jexistingexistingexisti", digest=digest)
        return None

    async def add(self, artifact):
        artifact.created_at = EPOCH
        artifact.updated_at = None
        self.added = artifact
        return artifact

    async def get(self, ref: str):
        return FakeRow(self.added)


class FakeSession:
    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def flush(self):
        return None


def service(hub=None, repo=None) -> ArtifactService:
    svc = ArtifactService(db=FakeSession(), hub=hub or FakeHub())
    svc.repo = repo or FakeRepo()
    return svc


def test_a_source_is_accepted_bare_or_schemed_and_normalises_to_one_form():
    assert normalise_source("openai/gpt-oss-20b") == "hf://openai/gpt-oss-20b"
    assert normalise_source("hf:openai/gpt-oss-20b") == "hf://openai/gpt-oss-20b"
    assert normalise_source(" hf://openai/gpt-oss-20b ") == "hf://openai/gpt-oss-20b"
    assert slug_of("hf://openai/gpt-oss-20b") == "openai/gpt-oss-20b"
    assert slug_of("gpt-oss-20b") == ""


async def test_resolve_returns_a_populated_draft():
    draft = await service().resolve("hf://openai/gpt-oss-20b")
    assert draft.digest.startswith("sha256:")
    assert draft.verified is True
    assert draft.name == "gpt-oss-20b"
    assert draft.kind == "weights"
    assert draft.format == "safetensors"
    assert draft.licence == "apache-2.0"
    assert draft.revision == "c" * 40
    assert draft.size_bytes == 10_400_000_000
    assert draft.parameter_count == 20_914_757_632
    assert draft.source_scheme == "hf"
    assert draft.source_uri == f"hf://openai/gpt-oss-20b@{'c' * 40}"
    assert draft.locations == [ArtifactLocationRequest(scheme="hf", uri=draft.source_uri, region=None, preferred=True)]
    assert draft.existing_id is None


async def test_the_draft_carries_the_shape_read_out_of_the_config():
    draft = await service().resolve("openai/gpt-oss-20b")
    assert draft.metadata["quantization"] == "mxfp4"
    assert draft.metadata["dtype"] == "bfloat16"
    assert draft.metadata["architecture"] == "GptOssForCausalLM"
    assert draft.metadata["is_moe"] is True
    assert draft.metadata["active_parameter_count"] < draft.parameter_count
    assert draft.metadata["context_length"] == 131072


async def test_a_prequantized_repo_is_sized_at_its_native_quantization():
    draft = await service().resolve("openai/gpt-oss-20b")
    assert draft.serving.quantization == "mxfp4"
    assert draft.serving.dtype == "bfloat16"
    assert draft.serving.device_count == 1
    assert 0 < draft.serving.device_memory_mib_min < 40 * 1024


async def test_the_suggested_context_is_the_declared_one_and_devices_follow_from_it():
    """The artifact sizes hardware to the weights, never the weights to hardware."""
    draft = await service().resolve("openai/gpt-oss-20b")
    assert draft.serving.context_length == draft.metadata["context_length"] == 131072
    assert not any("offered instead" in note.message for note in draft.notes)
    kv_at_full_context = 6 * 1024
    assert draft.serving.device_memory_mib_min > kv_at_full_context


async def test_weights_too_large_for_one_device_shard_across_a_power_of_two():
    """Five devices is arithmetically right and cannot be tensor-parallelised.

    The parameter count is derived from the biggest card the contract table names
    rather than written down. The suggestion divides by that card, so a literal here
    would stop testing sharding the week a bigger one is added to the table — it
    would still pass, for the wrong reason, having stopped needing to shard at all.
    """
    largest = devices.table().largest()
    assert largest is not None
    ceiling_gb = largest.memory_mib / 1024
    weights_gb = ceiling_gb * 2.35
    model = hf_model(parameter_count=int(weights_gb * 1024**3 / 0.5))

    draft = await service(hub=FakeHub(model)).resolve("openai/gpt-oss-20b")
    assert draft.serving.device_count == 4
    assert draft.serving.device_memory_mib_min * draft.serving.device_count > weights_gb * 1024
    assert draft.serving.context_length == 131072
    assert any("spreads it across 4 devices" in note.message for note in draft.notes)


async def test_the_suggestion_is_sized_against_the_cards_the_fleet_owns():
    """The failure this exists for, in the shape it actually happened.

    GLM-5.3-Flash needed about 383 GB. Divided by the biggest card the contract table
    names — a 288 GB B300 — that is two devices at a 192 GiB floor. The fleet holds
    H200s at 141 GB, so every one of them failed the floor, placement reported each
    cluster as owning no such device, and the workload was placed nowhere. Divided by
    a card we actually have, the same model spreads over four and asks for a floor
    those cards clear.
    """
    largest = devices.table().largest()
    assert largest is not None
    h200_mib = 143_771
    assert largest.memory_mib > h200_mib, "the table still names a card bigger than an H200"

    weights_gb = h200_mib / 1024 * 2.35
    model = hf_model(parameter_count=int(weights_gb * 1024**3 / 0.5))

    against_the_table = await service(hub=FakeHub(model)).resolve("openai/gpt-oss-20b")
    against_the_fleet = await service(hub=FakeHub(model), repo=FakeRepo(largest_device_mib=h200_mib)).resolve(
        "openai/gpt-oss-20b"
    )

    assert against_the_fleet.serving.device_count == 4
    assert against_the_fleet.serving.device_memory_mib_min <= h200_mib, (
        "a suggestion no card in the fleet can meet is a workload that places nowhere"
    )
    assert against_the_table.serving.device_memory_mib_min > against_the_fleet.serving.device_memory_mib_min


async def test_a_suggestion_falls_back_to_the_table_when_no_cluster_is_enrolled():
    """A plane with no supply yet still suggests something, from the catalogue."""
    model = hf_model(parameter_count=7_000_000_000)
    draft = await service(hub=FakeHub(model), repo=FakeRepo(largest_device_mib=None)).resolve("openai/gpt-oss-20b")
    assert draft.serving.device_count >= 1


async def test_a_serving_suggestion_is_offered_and_never_applied():
    """The draft seeds a workload form. Registering weights declares no way to serve them."""
    draft = await service().resolve("openai/gpt-oss-20b")
    assert isinstance(draft.serving, ServingDefaults)
    assert not {"recipe", "serving"} & set(ArtifactCreateRequest.__struct_fields__)


async def test_unquantized_weights_suggest_no_quantization_but_still_size_as_fp16():
    """A repo that declares no quantization must not be offered one.

    The suggestion becomes the workload's `--quantization`, and an engine takes only
    real quantization methods there — `fp16` is a dtype and is rejected before the
    worker binds a port. Sizing still needs a bytes-per-param bucket, and fp16 is
    the right one for unquantized weights, so the two answers differ here.
    """
    config = {k: v for k, v in GPT_OSS_CONFIG.items() if k != "quantization_config"}
    model = hf_model(slug="nvidia/Nemotron-30B-A3B-BF16", config=config)

    draft = await service(hub=FakeHub(model)).resolve(model.slug)

    assert draft.serving.quantization is None
    assert draft.metadata["quantization"] is None
    assert draft.serving.dtype == "bfloat16"

    quantized = await service().resolve("openai/gpt-oss-20b")
    assert quantized.serving.quantization == "mxfp4"
    assert draft.serving.device_memory_mib_min > quantized.serving.device_memory_mib_min


async def test_weights_the_hub_cannot_address_are_a_named_digest_and_unverified():
    model = hf_model(
        content_addressable=False,
        digest="hf:openai/gpt-oss-20b@" + "c" * 40,
        unaddressable_files=("model.bin",),
    )
    draft = await service(hub=FakeHub(model)).resolve("openai/gpt-oss-20b")
    assert draft.verified is False
    assert draft.digest.startswith("hf:")
    assert any("not content-addressable" in note.message for note in draft.notes)
    assert any("model.bin" in note.message for note in draft.notes)
    assert all(note.level == "warn" for note in draft.notes)


async def test_resolve_surfaces_an_artifact_the_catalogue_already_holds():
    digest = hf_model().digest
    draft = await service(repo=FakeRepo(existing_digest=digest)).resolve("openai/gpt-oss-20b")
    assert draft.existing_id == "art_01jexistingexistingexisti"


async def test_the_draft_carries_what_the_publisher_claims_attributed_to_the_hub():
    draft = await service().resolve("openai/gpt-oss-20b")
    assert draft.publisher.provider == "openai"
    assert draft.publisher.provider_model_id == "openai/gpt-oss-20b"
    assert draft.publisher.context_length == 131072
    assert draft.publisher.licence == "apache-2.0"
    assert draft.publisher.input_modalities == ["text"]
    assert draft.publisher.recorded_by == "huggingface"
    assert draft.publisher.source_uri == "https://huggingface.co/openai/gpt-oss-20b"


async def test_a_config_that_says_nothing_about_thinking_or_tools_claims_neither():
    """Null is the publisher not having said, and it is not `false`.

    A `false` here would deny a capability nobody denied, and the workload that
    seeds its claims from this block would be refused a tool call parser the
    engine recipe names.
    """
    draft = await service().resolve("openai/gpt-oss-20b")
    assert draft.publisher.reasoning is None
    assert draft.publisher.tool_calling is None
    assert draft.publisher.structured_output_modes == []


async def test_a_vision_tower_in_the_checkpoint_is_a_second_input_modality():
    config = {**GPT_OSS_CONFIG, "vision_config": {"hidden_size": 1152}}
    model = hf_model(slug="qwen/qwen3-vl", config=config)
    draft = await service(hub=FakeHub(model)).resolve(model.slug)
    assert draft.publisher.input_modalities == ["text", "image"]


async def test_an_unknown_model_is_not_found():
    with pytest.raises(NotFound):
        await service(hub=FakeHub(error=HuggingFaceNotFound("nope"))).resolve("openai/nope")


async def test_a_gated_model_names_the_token_problem():
    with pytest.raises(Invalid) as caught:
        await service(hub=FakeHub(error=HuggingFaceGated("gated"))).resolve("meta-llama/Llama-3-70B")
    assert "token" in caught.value.message
    assert caught.value.detail["token"] == "HANGAR_HF_TOKEN"


async def test_the_hub_being_down_is_not_the_callers_fault():
    with pytest.raises(PlaneError) as caught:
        await service(hub=FakeHub(error=HuggingFaceUnavailable("down"))).resolve("openai/gpt-oss-20b")
    assert caught.value.status == 500


class RefreshRepo:
    """One already-registered artifact, read back however it is asked for."""

    def __init__(self, artifact: Artifact, existing_digest: str | None = None, largest_device_mib: int | None = None):
        self.artifact = artifact
        self.existing_digest = existing_digest
        self.largest_device_mib = largest_device_mib

    async def largest_device_memory_mib(self):
        """What the fleet actually holds. `None` is a plane with no cluster enrolled yet."""
        return self.largest_device_mib

    async def get(self, ref: str):
        return FakeRow(self.artifact)

    async def artifact_by_digest(self, digest: str):
        if digest == self.existing_digest:
            return Artifact(uid="art_01jexistingexistingexisti", digest=digest)
        return None


def registered(**kwargs) -> Artifact:
    """An artifact as it sits in the table, defaulting to one registered before shapes."""
    stale = {
        "source": "huggingface",
        "huggingface_id": "openai/gpt-oss-20b",
        "revision": "c" * 40,
        "context_length": None,
        "shape": None,
    }
    defaults = {
        "uid": "art_01jgptoss20bgptoss20bgpt",
        "digest": hf_model().digest,
        "name": "gpt-oss-20b",
        "kind": "weights",
        "format": "safetensors",
        "size_bytes": 10_400_000_000,
        "licence": "apache-2.0",
        "parameter_count": 20_914_757_632,
        "verified": True,
        "metadata_": stale,
        "locations": [],
    }
    row = Artifact(**{**defaults, **kwargs})
    row.created_at = EPOCH
    row.updated_at = None
    return row


def refresh_service(artifact: Artifact, hub=None, existing_digest: str | None = None) -> ArtifactService:
    svc = ArtifactService(db=FakeSession(), hub=hub or FakeHub())
    svc.repo = RefreshRepo(artifact, existing_digest=existing_digest)
    return svc


async def test_a_refresh_records_what_the_plane_has_since_learned_to_read():
    """The case the route exists for: an artifact registered before shapes were extracted."""
    artifact = registered()
    result = await refresh_service(artifact).refresh("gpt-oss-20b")

    assert result.source == "hf://openai/gpt-oss-20b"
    assert "metadata.shape" in result.changed
    assert "metadata.context_length" in result.changed
    assert artifact.metadata_["shape"]["num_hidden_layers"] == GPT_OSS_CONFIG["num_hidden_layers"]
    assert artifact.metadata_["context_length"] == 131072


async def test_a_refresh_that_finds_nothing_new_changes_nothing_and_says_so():
    """No write, so nobody's `If-Match` goes stale because somebody pressed the button."""
    artifact = registered()
    await refresh_service(artifact).refresh("gpt-oss-20b")
    before = _revision(artifact)

    result = await refresh_service(artifact).refresh("gpt-oss-20b")
    assert result.changed == []
    assert _revision(artifact) == before


async def test_a_refresh_leaves_the_labels_a_person_put_on_the_artifact():
    artifact = registered(metadata_={"huggingface_id": "openai/gpt-oss-20b", "team": "inference"})
    await refresh_service(artifact).refresh("gpt-oss-20b")
    assert artifact.metadata_["team"] == "inference"
    assert artifact.metadata_["shape"] is not None


async def test_a_refresh_fills_the_claims_that_are_still_blank():
    artifact = registered()
    result = await refresh_service(artifact).refresh("gpt-oss-20b")
    assert "upstream.provider" in result.changed
    assert artifact.upstream["provider_model_id"] == "openai/gpt-oss-20b"
    assert artifact.upstream["context_length"] == 131072


async def test_a_refresh_never_overwrites_a_claim_somebody_asserted():
    """The Hub speaking twice does not outrank a person who asserted a capability."""
    artifact = registered(upstream={"provider": "our-fork", "tool_calling": True})
    await refresh_service(artifact).refresh("gpt-oss-20b")
    assert artifact.upstream["provider"] == "our-fork"
    assert artifact.upstream["tool_calling"] is True
    assert artifact.upstream["context_length"] == 131072


async def test_a_refresh_does_not_rewrite_what_a_person_decided_the_weights_are_for():
    artifact = registered(name="our-house-model", kind="weights")
    await refresh_service(artifact).refresh("our-house-model")
    assert artifact.name == "our-house-model"
    assert artifact.digest == hf_model().digest


async def test_a_refresh_reads_the_source_off_a_location_when_the_metadata_has_none():
    """Artifacts registered before `huggingface_id` existed still have their uri."""
    artifact = registered(
        metadata_={},
        locations=[ArtifactLocation(scheme="hf", uri=f"hf://openai/gpt-oss-20b@{'c' * 40}", preferred=True)],
    )
    result = await refresh_service(artifact).refresh("gpt-oss-20b")
    assert result.source == "hf://openai/gpt-oss-20b"
    assert result.changed


async def test_an_artifact_with_no_source_cannot_be_refreshed():
    artifact = registered(metadata_={}, locations=[ArtifactLocation(scheme="s3", uri="s3://bucket/weights")])
    with pytest.raises(Invalid) as caught:
        await refresh_service(artifact).refresh("gpt-oss-20b")
    assert "no source to re-read" in caught.value.message


async def test_a_source_that_now_resolves_to_other_bytes_is_a_new_artifact_not_an_edit():
    """Applying a new size over this digest would describe weights nobody can fetch."""
    artifact = registered(digest="sha256:" + "0" * 64, parameter_count=1)
    with pytest.raises(Conflict) as caught:
        await refresh_service(artifact).refresh("gpt-oss-20b")

    assert caught.value.detail["resolved_digest"] == hf_model().digest
    assert artifact.parameter_count == 1
    assert artifact.metadata_["shape"] is None


async def test_a_moved_source_points_at_the_artifact_that_already_holds_the_new_bytes():
    artifact = registered(digest="sha256:" + "0" * 64)
    svc = refresh_service(artifact, existing_digest=hf_model().digest)
    with pytest.raises(Conflict) as caught:
        await svc.refresh("gpt-oss-20b")
    assert caught.value.detail["existing_id"] == "art_01jexistingexistingexisti"


async def test_a_refresh_is_what_verifies_an_artifact_adopted_by_reference():
    """`verified` is the one derived field `PATCH` refuses, because resolving is an operation."""
    artifact = registered(verified=False)
    result = await refresh_service(artifact).refresh("gpt-oss-20b")
    assert "verified" in result.changed
    assert artifact.verified is True
    assert "verified" not in ArtifactUpdateRequest.__struct_fields__


async def test_a_refresh_carries_the_sources_own_warnings_through():
    model = hf_model(gated=True)
    artifact = registered()
    result = await refresh_service(artifact, hub=FakeHub(model)).refresh("gpt-oss-20b")
    assert any(note.level == "warn" and "gated" in note.message for note in result.notes)


async def test_a_source_that_is_not_a_slug_is_rejected():
    with pytest.raises(Invalid):
        await service().resolve("hf://gpt-oss-20b")


async def test_creating_from_a_source_alone_derives_the_digest():
    svc = service()
    result = await svc.create(ArtifactCreateRequest(source="hf://openai/gpt-oss-20b"))
    assert result.digest == hf_model().digest
    assert result.name == "gpt-oss-20b"
    assert result.verified is True
    assert result.size_bytes == 10_400_000_000
    assert result.licence == "apache-2.0"
    assert result.format == "safetensors"
    assert [loc.uri for loc in result.locations] == [f"hf://openai/gpt-oss-20b@{'c' * 40}"]


async def test_creating_from_a_source_records_the_publishers_claims_and_when():
    """The provenance triple is what separates a claim from a guess, so it is stamped."""
    result = await service().create(ArtifactCreateRequest(source="hf://openai/gpt-oss-20b"))
    assert result.upstream.provider == "openai"
    assert result.upstream.context_length == 131072
    assert result.upstream.recorded_by == "huggingface"
    assert result.upstream.recorded_at is not None


async def test_claims_the_caller_sent_are_not_replaced_by_the_hubs():
    claims = ArtifactUpstreamRequest(provider="our-fork", tool_calling=True, recorded_by="operator")
    result = await service().create(ArtifactCreateRequest(source="openai/gpt-oss-20b", upstream=claims))
    assert result.upstream.provider == "our-fork"
    assert result.upstream.tool_calling is True
    assert result.upstream.recorded_by == "operator"


async def test_a_source_and_a_digest_are_two_ways_to_disagree():
    with pytest.raises(Invalid):
        await service().create(ArtifactCreateRequest(source="openai/gpt-oss-20b", digest="sha256:abc", name="x"))


async def test_what_the_caller_stated_wins_over_what_was_resolved():
    svc = service()
    result = await svc.create(
        ArtifactCreateRequest(
            source="openai/gpt-oss-20b",
            name="house-gpt-oss",
            kind="adapter",
            metadata={"team": "inference"},
        )
    )
    assert result.name == "house-gpt-oss"
    assert result.kind == "adapter"
    assert result.metadata["team"] == "inference"
    assert result.metadata["huggingface_id"] == "openai/gpt-oss-20b"


async def test_an_artifact_with_neither_a_source_nor_a_digest_has_no_identity():
    with pytest.raises(Invalid) as caught:
        await service().create(ArtifactCreateRequest(name="mystery"))
    assert "identity" in caught.value.message


def test_the_resolve_path_is_registered_before_the_ref_path():
    paths = [
        path
        for handler in ArtifactRoutes.__dict__.values()
        if hasattr(handler, "handler_name")
        for path in handler.paths
    ]
    assert paths.index("/resolve") < paths.index("/{ref:str}")


def test_resolve_is_matched_as_a_literal_path_not_as_a_ref():
    """The router must dispatch the literal path to `resolve_artifact`, not to `get_artifact`.

    Asserting the path merely exists proves nothing: `/artifacts/{ref}` would
    also answer it. This reads the handler the router actually bound to GET.
    """
    from litestar import Litestar

    app = Litestar(route_handlers=[ArtifactRoutes], openapi_config=None)
    by_path = {route.path: route for route in app.routes}

    assert "/artifacts/resolve" in by_path
    assert by_path["/artifacts/resolve"].route_handler_map["GET"][0].handler_name == "resolve_artifact"
    assert by_path["/artifacts/{ref:str}"].route_handler_map["GET"][0].handler_name == "get_artifact"


# ------------------------------------------------------------------ baselines


class FakeBaselineRepo:
    """Stands in for the table, including the unique constraint the service relies on.

    The `(artifact, dataset, metric)` constraint is enforced here so that the
    conflict path is exercised without a database: the point of the test is that
    the service answers a duplicate with a message, and an `IntegrityError` is
    what the real table would hand it.
    """

    def __init__(self, artifact: Artifact) -> None:
        self.artifact = artifact
        self.rows: list[ArtifactBaseline] = []
        self.next_seq = 1000

    async def get(self, ref: str):
        if ref in {self.artifact.uid, self.artifact.name}:
            return FakeRow(self.artifact)
        return None

    async def add_baseline(self, baseline: ArtifactBaseline):
        taken = {(row.artifact_id, row.dataset, row.metric) for row in self.rows}
        if (baseline.artifact_id, baseline.dataset, baseline.metric) in taken:
            raise IntegrityError("INSERT", {}, Exception("uq_demand_artifact_baseline_dataset_metric"))
        baseline.created_at = EPOCH
        baseline.updated_at = None
        baseline.recorded_at = EPOCH
        baseline.seq = self.next_seq
        self.next_seq += 1
        self.rows.append(baseline)
        return baseline

    async def baseline(self, artifact_id, ref: str):
        for row in self.rows:
            if row.artifact_id == artifact_id and row.uid == ref:
                return row
        return None

    async def baseline_list(self, artifact_id, limit, after_seq=None, dataset=None, metric=None):
        kept = [row for row in self.rows if row.artifact_id == artifact_id]
        if after_seq is not None:
            kept = [row for row in kept if row.seq > after_seq]
        if dataset is not None:
            kept = [row for row in kept if row.dataset == dataset]
        if metric is not None:
            kept = [row for row in kept if row.metric == metric]
        return kept[: limit + 1]

    async def delete_baseline(self, baseline: ArtifactBaseline) -> None:
        self.rows.remove(baseline)


def baseline_service(actor: str = "ada@acme") -> ArtifactBaselineService:
    artifact = Artifact(id=uuid.uuid4(), uid=new_uid(ARTIFACT), name="gpt-oss-20b")
    svc = ArtifactBaselineService(db=FakeSession(), actor=actor)
    svc.repo = FakeBaselineRepo(artifact)
    return svc


def baseline_request(**kwargs) -> ArtifactBaselineCreateRequest:
    fields = {"dataset": "mmlu_pro", "metric": "accuracy", "value": 0.7412}
    return ArtifactBaselineCreateRequest(**{**fields, **kwargs})


async def test_a_baseline_round_trips_through_create_list_update_and_delete():
    """The route exists to make a declared floor visible, so the read must return what was written."""
    svc = baseline_service()
    artifact = svc.repo.artifact

    created = await svc.create(artifact.uid, baseline_request(source_uri="https://evals/mmlu_pro/run/12"))
    assert created.id.startswith("abl_")
    assert created.artifact_id == artifact.uid
    assert created.dataset == "mmlu_pro"
    assert created.metric == "accuracy"
    assert created.value == 0.7412
    assert created.tolerance_pct == 3.0
    assert created.source_uri == "https://evals/mmlu_pro/run/12"
    assert created.note is None

    listed = await svc.list(artifact.uid)
    assert [item.id for item in listed.items] == [created.id]
    assert listed.count == 1

    updated = await svc.update(artifact.uid, created.id, ArtifactBaselineUpdateRequest(value=0.75, note="rerun"))
    assert updated.value == 0.75
    assert updated.note == "rerun"
    assert updated.dataset == "mmlu_pro"

    await svc.delete(artifact.uid, created.id)
    assert (await svc.list(artifact.uid)).items == []


async def test_a_second_floor_for_one_dataset_and_metric_is_a_conflict_not_an_integrity_error():
    """Two declared floors for one eval is two standards, and a verdict would grade against either."""
    svc = baseline_service()
    artifact = svc.repo.artifact
    await svc.create(artifact.uid, baseline_request())

    with pytest.raises(Conflict) as caught:
        await svc.create(artifact.uid, baseline_request(value=0.9))

    assert caught.value.status == 409
    assert caught.value.detail == {"dataset": "mmlu_pro", "metric": "accuracy"}


async def test_the_recorder_is_the_principal_and_a_body_value_is_ignored():
    """A signature a caller can type is not a signature, and the table exists to be signed."""
    svc = baseline_service(actor="ada@acme")
    created = await svc.create(svc.repo.artifact.uid, baseline_request(recorded_by="grace@elsewhere"))
    assert created.recorded_by == "ada@acme"

    revised = await svc.update(
        svc.repo.artifact.uid,
        created.id,
        ArtifactBaselineUpdateRequest(value=0.8, recorded_by="grace@elsewhere"),
    )
    assert revised.recorded_by == "ada@acme"


async def test_an_if_match_mismatch_refuses_the_write():
    """Two people revising one floor from two reads is exactly the case the etag is for."""
    svc = baseline_service()
    created = await svc.create(svc.repo.artifact.uid, baseline_request())

    with pytest.raises(Conflict) as caught:
        await svc.update(
            svc.repo.artifact.uid,
            created.id,
            ArtifactBaselineUpdateRequest(value=0.8),
            if_match="0",
        )

    assert caught.value.detail["revision"] == created.revision
    assert (await svc.list(svc.repo.artifact.uid)).items[0].value == 0.7412


async def test_a_matching_if_match_is_accepted():
    svc = baseline_service()
    created = await svc.create(svc.repo.artifact.uid, baseline_request())
    revised = await svc.update(
        svc.repo.artifact.uid,
        created.id,
        ArtifactBaselineUpdateRequest(value=0.8),
        if_match=created.revision,
    )
    assert revised.value == 0.8


async def test_dataset_and_metric_are_the_rows_identity_and_cannot_be_patched():
    """Moving a floor to another eval is a different baseline, not an edit to this one."""
    fields = set(ArtifactBaselineUpdateRequest.__struct_fields__)
    assert "dataset" not in fields
    assert "metric" not in fields


async def test_numeric_columns_are_floats_by_the_time_they_reach_the_wire():
    """`Numeric` meets a float acceptance floor in `verdicts.py`; converting once here is the seam."""
    svc = baseline_service()
    created = await svc.create(svc.repo.artifact.uid, baseline_request())
    row = svc.repo.rows[0]
    row.value = decimal.Decimal("0.741200")
    row.tolerance_pct = decimal.Decimal("3.000")

    read = await svc.get(svc.repo.artifact.uid, created.id)
    assert isinstance(read.value, float)
    assert isinstance(read.tolerance_pct, float)


async def test_a_baseline_of_another_artifact_is_not_found_under_this_one():
    """The uid is global but the sub-resource is not, and a floor read off the wrong artifact grades the wrong weights.

    The row has to exist and belong to somebody else for this to mean anything —
    looking up a uid nothing owns passes whether or not the read is scoped at all.
    """
    svc = baseline_service()
    stranger = ArtifactBaseline(
        id=uuid.uuid4(),
        uid=new_uid(ARTIFACT_BASELINE),
        artifact_id=uuid.uuid4(),
        dataset="mmlu_pro",
        metric="accuracy",
        value=decimal.Decimal("0.75"),
    )
    svc.repo.rows.append(stranger)

    with pytest.raises(NotFound):
        await svc.get(svc.repo.artifact.uid, stranger.uid)


async def test_an_unknown_artifact_has_no_baseline_collection():
    svc = baseline_service()
    with pytest.raises(NotFound):
        await svc.list("art_01jnothingnothingnothing")


def test_baselines_are_read_scoped_to_the_artifact(repo):
    text = sql(repo.baseline_list_query(artifact_id=uuid.uuid4(), limit=10, dataset="mmlu_pro"))
    assert "demand_artifact_baseline.artifact_id = " in text
    assert "demand_artifact_baseline.dataset = " in text
    assert "ORDER BY demand_artifact_baseline.seq ASC" in text


def test_the_baselines_sub_path_is_not_read_as_a_ref():
    """`/{ref}` is registered on this controller too, and a shadowed sub-path is the 404 this item fixes."""
    from litestar import Litestar

    app = Litestar(route_handlers=[ArtifactRoutes], openapi_config=None)
    by_path = {route.path: route for route in app.routes}

    assert by_path["/artifacts/{ref:str}/baselines"].route_handler_map["GET"][0].handler_name == (
        "list_artifact_baselines"
    )
    assert by_path["/artifacts/{ref:str}/baselines"].route_handler_map["POST"][0].handler_name == (
        "create_artifact_baseline"
    )
    handlers = by_path["/artifacts/{ref:str}/baselines/{baseline:str}"].route_handler_map
    assert handlers["PATCH"][0].handler_name == "update_artifact_baseline"
    assert handlers["DELETE"][0].handler_name == "delete_artifact_baseline"


def test_the_baseline_patch_reads_if_match_from_the_header():
    parameter = inspect.signature(ArtifactRoutes.update_artifact_baseline.fn).parameters["if_match"]
    assert parameter.default.header == "If-Match"


class FleetRow:
    def __init__(self, device_class, memory_mib, total, allocatable, clusters):
        self.device_class = device_class
        self.memory_mib = memory_mib
        self.devices_total = total
        self.devices_allocatable = allocatable
        self.cluster_count = clusters


class FitmentRepo:
    """A stored artifact and a reported fleet, which is all a fitment reads."""

    def __init__(self, artifact, fleet=None, domains=None):
        self.artifact = artifact
        self.fleet = fleet if fleet is not None else [FleetRow("h100-80gb-sxm", 81559, 16, 16, 2)]
        self.domains = domains or {}

    async def get(self, ref: str):
        return FakeRow(self.artifact)

    async def fleet_classes(self):
        return self.fleet

    async def devices_per_node(self):
        """What the fleet has observed, which is nothing unless a test says otherwise.

        An empty map is the normal case for a class nobody has racked, and it is what
        makes the caller's `domain_size` (or the default) apply.
        """
        return self.domains


def sized_artifact(
    *,
    parameter_count=70_000_000_000,
    quantization="fp16",
    context_length=8192,
    shape=True,
    declare_context=True,
):
    model_shape = extract_shape(
        {
            "num_hidden_layers": 80,
            "num_attention_heads": 64,
            "num_key_value_heads": 8,
            "hidden_size": 8192,
            "max_position_embeddings": context_length,
        }
    )
    assert model_shape is not None
    metadata = {"quantization": quantization}
    if declare_context:
        metadata["context_length"] = context_length
    if shape:
        metadata["shape"] = shape_metadata(model_shape)
    artifact = Artifact(
        uid=new_uid(ARTIFACT),
        digest="sha256:abc",
        name="llama-70b",
        parameter_count=parameter_count,
        created_at=EPOCH,
    )
    artifact.metadata_ = metadata
    return artifact


def fitment_service(artifact=None, fleet=None, domains=None) -> ArtifactService:
    svc = ArtifactService(db=FakeSession(), hub=FakeHub())
    svc.repo = FitmentRepo(artifact or sized_artifact(), fleet, domains)
    return svc


async def test_a_fitment_fits_the_fleet_by_default_and_carries_what_we_have_of_it():
    result = await fitment_service().fitment("llama-70b", ArtifactFitmentRequest())
    assert result.scope == "fleet"
    assert result.sizable
    assert [item.device_class for item in result.items] == ["h100-80gb-sxm"]

    fit = result.items[0]
    assert fit.fits
    assert fit.device_count == 2
    assert fit.weights_gb < fit.required_gb <= fit.aggregate_gb
    assert fit.devices_total == 16
    assert fit.devices_allocatable == 16
    assert fit.cluster_count == 2
    assert fit.in_catalog
    assert fit.label == "NVIDIA H100 80GB SXM"


async def test_a_row_carries_the_other_counts_that_serve_and_not_only_the_recommendation():
    """Half a node and the whole node are both deployable; the row offers both."""
    result = await fitment_service().fitment("llama-70b", ArtifactFitmentRequest())

    fit = result.items[0]
    counts = [layout.device_count for layout in fit.layouts]
    assert counts == sorted(counts)
    assert fit.device_count in counts
    assert counts[-1] > fit.device_count
    assert all(layout.tensor_parallel * layout.pipeline_parallel == layout.device_count for layout in fit.layouts)


async def test_a_layout_the_fleet_holds_too_few_cards_for_says_so_on_its_own():
    """A wider layout can be short on a class the recommendation is fine on."""
    fleet = [FleetRow("h100-80gb-sxm", 81559, 2, 2, 1)]
    result = await fitment_service(fleet=fleet).fitment("llama-70b", ArtifactFitmentRequest())

    fit = result.items[0]
    assert not fit.supply_short
    assert [layout.supply_short for layout in fit.layouts if layout.device_count > 2]
    assert all(layout.supply_short for layout in fit.layouts if layout.device_count > 2)


async def test_a_class_that_fits_nowhere_carries_no_layouts():
    huge = sized_artifact(parameter_count=2_800_000_000_000)
    fleet = [FleetRow("l4-24gb-pcie", 24576, 8, 8, 1)]
    result = await fitment_service(huge, fleet=fleet).fitment("llama-70b", ArtifactFitmentRequest(max_devices=8))

    fit = result.items[0]
    assert not fit.fits
    assert fit.layouts == []


async def test_the_catalog_scope_answers_a_procurement_question_the_fleet_cannot():
    """A class we own none of is a fit worth showing, not a row worth filtering."""
    fleet = await fitment_service().fitment("llama-70b", ArtifactFitmentRequest())
    catalog = await fitment_service().fitment("llama-70b", ArtifactFitmentRequest(scope="catalog"))

    assert len(catalog.items) > len(fleet.items)
    unowned = [item for item in catalog.items if item.devices_total == 0 and item.fits]
    assert unowned, "the catalog scope should surface classes that fit and that we do not have"
    assert all(item.devices_allocatable == 0 for item in unowned)


async def test_a_fitment_is_ranked_with_the_cheapest_workable_class_first():
    catalog = await fitment_service().fitment("llama-70b", ArtifactFitmentRequest(scope="catalog"))
    fits = [item for item in catalog.items if item.fits]
    assert catalog.items[0].fits
    assert fits[0].device_count <= fits[-1].device_count


async def test_a_shapeless_artifact_gets_a_matrix_of_floors_not_a_blank_panel():
    """The weights still bound which cards are too small; only the window is uncosted."""
    result = await fitment_service(sized_artifact(shape=False)).fitment("llama-70b", ArtifactFitmentRequest())
    assert result.sizable
    assert len(result.items) == 1
    assert result.items[0].kv_counted is False
    assert result.items[0].context_length == 0
    assert result.note is not None
    assert "floor" in result.note


async def test_an_artifact_with_no_parameter_count_is_reported_rather_than_refused():
    """Nothing at all can be sized from it, and that is a note rather than an error."""
    artifact = sized_artifact(parameter_count=None)
    result = await fitment_service(artifact).fitment("llama-70b", ArtifactFitmentRequest())
    assert not result.sizable
    assert result.items == []
    assert result.note is not None
    assert "no parameter count" in result.note


async def test_a_full_sizing_counts_the_cache_and_says_so():
    result = await fitment_service().fitment("llama-70b", ArtifactFitmentRequest())
    assert all(item.kv_counted for item in result.items)
    assert result.note is None


async def test_the_artifacts_own_context_is_the_default_window():
    """Trimming it here would make more cards look workable by answering another question."""
    artifact = sized_artifact(context_length=131072)
    result = await fitment_service(artifact).fitment("llama-70b", ArtifactFitmentRequest())
    assert result.context_length == 131072

    asked = await fitment_service(artifact).fitment("llama-70b", ArtifactFitmentRequest(context_length=8192))
    assert asked.context_length == 8192
    assert asked.items[0].device_count <= result.items[0].device_count


async def test_quantization_can_be_asked_about_without_registering_anything():
    heavy = await fitment_service().fitment("llama-70b", ArtifactFitmentRequest())
    light = await fitment_service().fitment("llama-70b", ArtifactFitmentRequest(quantization="int4"))
    assert heavy.quantization == "fp16"
    assert light.quantization == "int4"
    assert light.items[0].weights_gb < heavy.items[0].weights_gb
    assert light.items[0].device_count < heavy.items[0].device_count


async def test_hardware_the_contract_table_never_named_is_still_fitted():
    """`app.core.devices` slugifies an unknown card; a fit uses what the cluster reported."""
    fleet = [FleetRow("some-new-accelerator-192gb", 196608, 8, 4, 1)]
    result = await fitment_service(fleet=fleet).fitment("llama-70b", ArtifactFitmentRequest())

    fit = result.items[0]
    assert fit.device_class == "some-new-accelerator-192gb"
    assert not fit.in_catalog
    assert fit.label == "some-new-accelerator-192gb"
    assert fit.memory_mib_per_device == 196608
    assert fit.fits


async def test_a_fit_the_fleet_holds_too_few_cards_for_is_marked_short():
    """The 4TB-on-five-cards case: arithmetically a fit, and nothing anyone can run."""
    fleet = [FleetRow("rtx-pro-6000-blackwell", 98304, 5, 5, 1)]
    huge = sized_artifact(parameter_count=2_000_000_000_000)
    result = await fitment_service(huge, fleet=fleet).fitment("llama-70b", ArtifactFitmentRequest())

    fit = result.items[0]
    assert fit.fits
    assert fit.device_count > fit.devices_total
    assert fit.supply_short


async def test_a_class_we_own_enough_of_outranks_one_we_do_not():
    """Supply beats the window: a truncated fit that runs today beats a full one we cannot."""
    fleet = [
        FleetRow("h100-80gb-sxm", 81559, 1, 1, 1),
        FleetRow("h200-141gb-sxm", 144384, 16, 16, 1),
    ]
    result = await fitment_service(fleet=fleet).fitment("llama-70b", ArtifactFitmentRequest())

    assert [item.device_class for item in result.items] == ["h200-141gb-sxm", "h100-80gb-sxm"]
    assert not result.items[0].supply_short
    assert result.items[1].supply_short


async def test_a_class_we_own_none_of_is_a_purchase_and_not_a_shortfall():
    """Zero total is the procurement answer the catalog scope exists to give."""
    result = await fitment_service().fitment("llama-70b", ArtifactFitmentRequest(scope="catalog", limit=0))

    unowned = [item for item in result.items if item.devices_total == 0]
    assert unowned
    assert not any(item.supply_short for item in unowned)


async def test_an_empty_fleet_says_so_rather_than_returning_an_empty_matrix():
    result = await fitment_service(fleet=[]).fitment("llama-70b", ArtifactFitmentRequest())
    assert result.sizable
    assert result.items == []
    assert result.note is not None
    assert "no cluster has reported a device" in result.note


@pytest.mark.parametrize(
    "request_",
    [
        ArtifactFitmentRequest(scope="everything"),
        ArtifactFitmentRequest(concurrency=0),
        ArtifactFitmentRequest(max_devices=0),
        ArtifactFitmentRequest(context_length=0),
    ],
)
async def test_a_fitment_refuses_terms_it_cannot_fit_against(request_):
    with pytest.raises(Invalid):
        await fitment_service().fitment("llama-70b", request_)


async def test_a_fitment_reads_and_reserves_nothing():
    """A view, not a decision: placement is still the thing that can say no."""
    svc = fitment_service()
    await svc.fitment("llama-70b", ArtifactFitmentRequest())
    assert not hasattr(svc.repo, "added") or svc.repo.added is None


async def test_an_artifact_carrying_neither_a_shape_nor_a_window_still_fits():
    """The real shapeless case: no persisted shape and no declared context either.

    Every artifact registered before the shape was persisted looks like this, and
    reaching for `sizing.shape.context_length` to default the window turned the whole
    panel into a 500 for exactly those.
    """
    artifact = sized_artifact(shape=False, declare_context=False)
    result = await fitment_service(artifact).fitment("llama-70b", ArtifactFitmentRequest())

    assert result.sizable
    assert result.context_length == 8192
    assert len(result.items) == 1
    assert result.items[0].kv_counted is False


async def test_a_frontier_model_is_answered_as_a_rack_order():
    """The user-facing shape of the answer: "2 domains of 8× B300", not a red row.

    A 2.8T artifact fits in no single chassis on the market. Before the fit could cross
    a scale-up domain, every row here came back as a refusal, which is the opposite of
    useful for the one case where knowing the hardware bill matters most.
    """
    kimi = sized_artifact(parameter_count=2_800_000_000_000, quantization="fp8", shape=False)
    fleet = [FleetRow("b300-288gb-sxm", 294912, 32, 32, 2), FleetRow("b200-180gb-sxm", 183359, 64, 64, 4)]
    result = await fitment_service(kimi, fleet).fitment("kimi", ArtifactFitmentRequest())

    by_class = {item.device_class: item for item in result.items}
    b300, b200 = by_class["b300-288gb-sxm"], by_class["b200-180gb-sxm"]

    assert b300.fits and b300.device_count == 16 and b300.node_count == 2
    assert (b300.tensor_parallel, b300.pipeline_parallel) == (8, 2)
    assert b200.fits and b200.device_count == 32 and b200.node_count == 4
    assert result.items[0].device_class == "b300-288gb-sxm", "fewer domains ranks first"


async def test_an_observed_nvl72_node_beats_the_assumed_chassis():
    """A class the fleet runs is laid out the way the fleet actually racks it."""
    kimi = sized_artifact(parameter_count=2_800_000_000_000, quantization="fp8", shape=False)
    fleet = [FleetRow("b300-288gb-sxm", 294912, 144, 144, 2)]

    chassis = await fitment_service(kimi, fleet).fitment("kimi", ArtifactFitmentRequest())
    rack = await fitment_service(kimi, fleet, {"b300-288gb-sxm": 72}).fitment("kimi", ArtifactFitmentRequest())

    assert chassis.items[0].devices_per_node == 8
    assert chassis.items[0].node_count == 2
    assert rack.items[0].devices_per_node == 72
    assert rack.items[0].node_count == 1
    assert rack.items[0].pipeline_parallel == 1


async def test_a_stated_node_size_applies_only_where_nothing_was_observed():
    """An observed figure is real; a stated one is an assumption and must not overrule it."""
    kimi = sized_artifact(parameter_count=2_800_000_000_000, quantization="fp8", shape=False)
    fleet = [FleetRow("b300-288gb-sxm", 294912, 144, 144, 2)]
    result = await fitment_service(kimi, fleet, {"b300-288gb-sxm": 8}).fitment(
        "kimi", ArtifactFitmentRequest(scope="catalog", devices_per_node=72, limit=0)
    )

    by_class = {item.device_class: item for item in result.items}
    assert by_class["b300-288gb-sxm"].devices_per_node == 8
    assert by_class["b200-180gb-sxm"].devices_per_node == 72


async def test_a_fitment_returns_the_ranked_head_and_says_how_many_it_ranked():
    """A wall of nineteen classes buries the answer; a silent cap reads as the whole world."""
    result = await fitment_service().fitment("llama-70b", ArtifactFitmentRequest(scope="catalog"))

    assert len(result.items) == DEFAULT_FITMENT_LIMIT
    assert result.considered > DEFAULT_FITMENT_LIMIT
    assert result.items[0].fits


async def test_the_head_is_the_best_rows_and_not_the_first_ones_looked_at():
    """Every candidate is fitted and ranked before the cap applies."""
    capped = await fitment_service().fitment("llama-70b", ArtifactFitmentRequest(scope="catalog"))
    every = await fitment_service().fitment("llama-70b", ArtifactFitmentRequest(scope="catalog", limit=0))

    assert len(every.items) == every.considered == capped.considered
    assert [item.device_class for item in capped.items] == [
        item.device_class for item in every.items[: len(capped.items)]
    ]


async def test_asking_for_everything_is_how_the_rest_is_reached():
    result = await fitment_service().fitment("llama-70b", ArtifactFitmentRequest(scope="catalog", limit=0))
    assert len(result.items) == result.considered > DEFAULT_FITMENT_LIMIT


# --------------------------------------------------------- what a frontend needs to serve it

SERVEABLE_FILES = (
    "config.json",
    "tokenizer_config.json",
    "tokenizer.json",
    "chat_template.jinja",
    "model-00001-of-00002.safetensors",
)


def _messages(model) -> str:
    return " | ".join(note.message for note in resolved(model).notes)


def test_a_repo_a_frontend_can_serve_says_nothing():
    """The ordinary case must stay quiet, or the notes stop being read."""
    messages = _messages(hf_model(files=SERVEABLE_FILES))

    assert "chat template" not in messages
    assert "tokenizer_config.json" not in messages


def test_a_repo_with_no_chat_template_is_flagged_at_registration():
    """The `openai/whisper-large-v3` failure, caught before anybody deploys it.

    Its frontend registration fails on `chat_template field is required in the
    tokenizer_config.json file` -- after the weights download and the pod is
    scheduled, on a deployment every count in the cluster calls healthy.
    """
    files = tuple(name for name in SERVEABLE_FILES if name != "chat_template.jinja")
    messages = _messages(hf_model(files=files, tokenizer_config={"model_max_length": 448}))

    assert "no chat template" in messages


def test_a_chat_template_inside_the_tokenizer_config_counts():
    """Older repos carry the template as a key rather than a file, and the frontend reads both."""
    files = tuple(name for name in SERVEABLE_FILES if name != "chat_template.jinja")
    messages = _messages(hf_model(files=files, tokenizer_config={"chat_template": "{% for m in messages %}"}))

    assert "no chat template" not in messages


def test_a_repo_with_no_tokenizer_config_says_so_and_stops():
    """Nothing downstream is worth saying when the prompt formatter cannot exist at all."""
    files = ("config.json", "model-00001-of-00002.safetensors")
    messages = _messages(hf_model(files=files))

    assert "no tokenizer_config.json" in messages
    assert "no chat template" not in messages


def test_a_tokenizer_a_frontend_cannot_read_is_flagged():
    """Kimi K3 ships `tiktoken.model` and custom code rather than `tokenizer.json`.

    That is legal and it serves, but only on a runtime carrying the tokenizer
    itself -- which is a fact worth having before the deployment, not after.
    """
    files = ("config.json", "tokenizer_config.json", "chat_template.jinja")
    messages = _messages(hf_model(files=files, tokenizer_config={"chat_template": "x"}))

    assert "no tokenizer file" in messages

    served = _messages(hf_model(files=files + ("tiktoken.model",), tokenizer_config={"chat_template": "x"}))
    assert "no tokenizer file" not in served


def test_a_repo_whose_files_were_never_read_is_not_guessed_about():
    """An artifact registered before the listing existed must not grow three warnings."""
    messages = _messages(hf_model())

    assert "tokenizer" not in messages
    assert "chat template" not in messages
