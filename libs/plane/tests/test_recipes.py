import inspect
import datetime as dt

import pytest
import msgspec

from app.core.ids import RECIPE
from app.core.ids import ARTIFACT
from app.core.ids import new_uid
from app.db.models import Recipe
from app.core.exceptions import Invalid
from app.modules.recipes.routes import RecipeRoutes
from app.modules.recipes.schemas import RecipeSchema
from app.modules.recipes.schemas import RecipeCacheSchema
from app.modules.recipes.schemas import RecipeShapeSchema
from app.modules.recipes.schemas import RecipeCreateRequest
from app.modules.recipes.schemas import RecipeUpdateRequest
from app.modules.recipes.services import _revision
from app.modules.recipes.services import check_shape
from app.modules.recipes.services import check_footprint
from app.modules.recipes.repository import RecipeRepository


EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


@pytest.fixture
def repo():
    return RecipeRepository(db=None)


def sql(query) -> str:
    return str(query)


def shape(**kwargs) -> dict:
    base = {
        "device_class": "h100",
        "device_count": 8,
        "tensor_parallel": 8,
        "pipeline_parallel": 1,
        "expert_parallel": 1,
        "context_length": 32768,
        "kv_cache_fraction": None,
    }
    return base | kwargs


def test_a_shape_without_a_device_class_is_not_a_shape():
    """Tensor-parallel-8 means nothing spread across an A100 and an H100."""
    with pytest.raises(Invalid):
        check_shape(**shape(device_class=""))
    with pytest.raises(Invalid):
        check_shape(**shape(device_class="   "))


def test_device_class_is_required_at_the_request_boundary_too():
    fields = RecipeCreateRequest.__struct_fields__
    assert "device_class" in fields
    with pytest.raises(TypeError):
        RecipeCreateRequest(artifact="a", name="n", engine="vllm", engine_version="0.6", context_length=8192)


def test_device_class_cannot_be_cleared_by_a_patch():
    assert RecipeUpdateRequest.__annotations__["device_class"] == str | msgspec.UnsetType
    assert type(None) not in (RecipeUpdateRequest.__annotations__["device_class"]).__args__


def test_a_placeable_shape_is_accepted():
    assert check_shape(**shape()) is None
    assert check_shape(**shape(device_count=16, tensor_parallel=8, pipeline_parallel=2)) is None


def test_a_device_count_that_does_not_divide_the_parallel_layout_is_refused():
    with pytest.raises(Invalid):
        check_shape(**shape(device_count=6, tensor_parallel=4))


def test_expert_parallelism_cannot_exceed_the_devices_there_are():
    with pytest.raises(Invalid):
        check_shape(**shape(expert_parallel=16))


@pytest.mark.parametrize("field", ["device_count", "tensor_parallel", "pipeline_parallel", "context_length"])
def test_the_counts_that_must_be_at_least_one(field):
    with pytest.raises(Invalid):
        check_shape(**shape(**{field: 0}))


@pytest.mark.parametrize("fraction", [0, -0.1, 1.5])
def test_kv_cache_fraction_outside_zero_to_one_is_refused(fraction):
    with pytest.raises(Invalid):
        check_shape(**shape(kv_cache_fraction=fraction))


def test_kv_cache_fraction_is_optional_and_a_whole_device_is_allowed():
    assert check_shape(**shape(kv_cache_fraction=None)) is None
    assert check_shape(**shape(kv_cache_fraction=1)) is None


def test_negative_footprint_hints_are_refused():
    assert check_footprint(cpu_cores=None, memory_mib=8, disk_gib=0) is None
    with pytest.raises(Invalid):
        check_footprint(device_memory_mib_min=-1)
    with pytest.raises(Invalid):
        check_footprint(max_batch_tokens=-8)


def test_context_length_lives_on_the_recipe_not_on_a_catalog_entry():
    assert "context_length" in RecipeSchema.__struct_fields__
    assert "context_length" in RecipeCreateRequest.__struct_fields__


def test_kv_cache_is_recipe_configuration_and_not_the_weight_cache():
    fields = set(RecipeCacheSchema.__struct_fields__)
    assert fields == {"kv_cache_dtype", "kv_cache_fraction", "prefix_cache_enabled", "prefix_cache_config"}
    assert not [f for f in RecipeSchema.__struct_fields__ if "warm" in f or "resident" in f]


def test_the_shape_is_returned_as_one_thing_class_included():
    assert "device_class" in RecipeShapeSchema.__struct_fields__
    assert "device_class" not in RecipeSchema.__struct_fields__


def test_the_weights_a_recipe_serves_cannot_be_repointed_by_a_patch():
    fields = RecipeUpdateRequest.__struct_fields__
    assert "artifact" not in fields
    assert "version" not in fields


def test_update_request_leaves_absent_fields_unset():
    data = RecipeUpdateRequest(engine_version="0.7.0")
    assert data.to_dict(exclude_unset=True) == {"engine_version": "0.7.0"}
    assert RecipeUpdateRequest(quantization=None).to_dict(exclude_unset=True) == {"quantization": None}


def test_ids_are_prefixed_and_lowercase():
    uid = new_uid(RECIPE)
    assert uid.startswith("rec_") and uid == uid.lower()


def test_label_selector_becomes_a_predicate_per_key(repo):
    """One `->>` comparison per key, so two labels narrow rather than widen."""
    text = sql(repo.list_query(limit=10, labels={"tier": "gold", "team": "search"}))
    assert text.count("demand_recipe.labels ->>") == 2
    assert "->>" not in sql(repo.list_query(limit=10))


def test_revision_is_derived_from_the_last_write_not_stored():
    assert _revision(Recipe(created_at=EPOCH, updated_at=None)) == str(int(EPOCH.timestamp() * 1_000_000))


def test_list_query_filters_compile_into_one_statement(repo):
    text = sql(
        repo.list_query(
            limit=10,
            after_seq=41,
            artifact="llama-70b",
            engine="vllm",
            device_class="h100",
            search="fp8",
        )
    )
    assert "FROM demand_recipe JOIN demand_artifact" in text
    assert "demand_recipe.seq > " in text
    assert "demand_artifact.name = " in text
    assert "demand_recipe.engine = " in text
    assert "demand_recipe.device_class = " in text
    assert "lower(demand_recipe.name) LIKE lower(" in text
    assert "ORDER BY demand_recipe.seq ASC" in text


def test_artifact_filter_resolves_an_id_against_uid_not_name(repo):
    assert "demand_artifact.uid = " in sql(repo.list_query(limit=10, artifact=new_uid(ARTIFACT)))


def test_list_query_over_fetches_one_row_for_the_cursor(repo):
    assert repo.list_query(limit=10).compile().params["param_1"] == 11


def test_get_query_resolves_a_name_or_an_id(repo):
    assert "demand_recipe.name = " in sql(repo.get_query("llama-70b-fp8"))
    assert "demand_recipe.uid = " in sql(repo.get_query(new_uid(RECIPE)))


def test_a_bare_name_names_a_family_and_resolves_to_its_newest_version(repo):
    assert "ORDER BY demand_recipe.version DESC" in sql(repo.get_query("llama-70b-fp8"))


def test_controller_exposes_full_crud():
    handlers = {h.handler_name for h in RecipeRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert handlers == {"list_recipes", "create_recipe", "get_recipe", "update_recipe", "delete_recipe"}


def test_class_filter_is_exposed_as_class_not_device_class():
    parameter = inspect.signature(RecipeRoutes.list_recipes.fn).parameters["device_class"]
    assert parameter.default.query == "class"


def test_conditional_update_reads_if_match_from_the_header():
    parameter = inspect.signature(RecipeRoutes.update_recipe.fn).parameters["if_match"]
    assert parameter.default.header == "If-Match"


def test_delete_refuses_while_a_deployment_still_names_the_recipe():
    from app.modules.recipes.services import RecipeService

    assert "Refuses" in (RecipeService.delete.__doc__ or "")
