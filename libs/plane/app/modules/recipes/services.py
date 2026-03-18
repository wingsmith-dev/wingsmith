from msgspec import UNSET
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import RECIPE
from app.core.ids import new_uid
from app.db.models import Recipe
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import RecipeSchema
from .schemas import RecipeCacheSchema
from .schemas import RecipeShapeSchema
from .schemas import RecipeCreateRequest
from .schemas import RecipeUpdateRequest
from .schemas import RecipeResourcesSchema
from .schemas import RecipeParallelismSchema
from .repository import RecipeRepository


def check_footprint(**fields: int | None) -> None:
    """Rejects a negative resource request.

    Separate from the shape check because these are per-replica sizing hints
    rather than placement constraints: none of them decide which devices a recipe
    can run on, and none of them can be below zero either.
    """
    for name, value in fields.items():
        if value is not None and value < 0:
            raise Invalid(f"{name} cannot be negative", {name: value})


def check_shape(
    device_class: str,
    device_count: int,
    tensor_parallel: int,
    pipeline_parallel: int,
    expert_parallel: int,
    context_length: int,
    kv_cache_fraction: float | None,
) -> None:
    """Rejects a shape that cannot be placed.

    Two rules, both from heterogeneity being a scheduling constraint rather than
    an enrolment detail. A shape without a device class is not a shape — you
    cannot tensor-parallel across an A100 and an H100 — and a device count that
    is not a whole multiple of tensor × pipeline parallelism describes a layout
    no scheduler can lay down.
    """
    if not device_class or not device_class.strip():
        raise Invalid("a shape without a device class is not a shape")
    for name, value in (
        ("device_count", device_count),
        ("tensor_parallel", tensor_parallel),
        ("pipeline_parallel", pipeline_parallel),
        ("expert_parallel", expert_parallel),
        ("context_length", context_length),
    ):
        if value < 1:
            raise Invalid(f"{name} must be at least 1", {name: value})
    span = tensor_parallel * pipeline_parallel
    if device_count % span:
        raise Invalid(
            "device_count must be a whole multiple of tensor × pipeline parallelism",
            {"device_count": device_count, "tensor_parallel": tensor_parallel, "pipeline_parallel": pipeline_parallel},
        )
    if expert_parallel > device_count:
        raise Invalid(
            "expert_parallel exceeds the device count",
            {"expert_parallel": expert_parallel, "device_count": device_count},
        )
    if kv_cache_fraction is not None and not 0 < kv_cache_fraction <= 1:
        raise Invalid("kv_cache_fraction must be within (0, 1]", {"kv_cache_fraction": kv_cache_fraction})


def _revision(recipe: Recipe) -> str:
    stamp = recipe.updated_at or recipe.created_at
    return str(int(stamp.timestamp() * 1_000_000))


class RecipeService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = RecipeRepository(db)

    def schema(self, recipe: Recipe, artifact_uid: str) -> RecipeSchema:
        fraction = recipe.kv_cache_fraction
        return RecipeSchema(
            id=recipe.uid,
            artifact_id=artifact_uid,
            name=recipe.name,
            version=recipe.version,
            engine=recipe.engine,
            engine_version=recipe.engine_version,
            engine_image=recipe.engine_image,
            engine_args=recipe.engine_args,
            quantization=recipe.quantization,
            dtype=recipe.dtype,
            parallelism=RecipeParallelismSchema(
                tensor=recipe.tensor_parallel,
                pipeline=recipe.pipeline_parallel,
                expert=recipe.expert_parallel,
            ),
            context_length=recipe.context_length,
            max_batch_tokens=recipe.max_batch_tokens,
            cache=RecipeCacheSchema(
                kv_cache_dtype=recipe.kv_cache_dtype,
                kv_cache_fraction=None if fraction is None else float(fraction),
                prefix_cache_enabled=recipe.prefix_cache_enabled,
                prefix_cache_config=recipe.prefix_cache_config,
            ),
            shape=RecipeShapeSchema(
                device_class=recipe.device_class,
                device_count=recipe.device_count,
                device_memory_mib_min=recipe.device_memory_mib_min,
                requires_single_topology_domain=recipe.requires_single_topology_domain,
                topology_domain_kind=recipe.topology_domain_kind,
            ),
            resources=RecipeResourcesSchema(
                cpu_cores=recipe.cpu_cores,
                memory_mib=recipe.memory_mib,
                disk_gib=recipe.disk_gib,
            ),
            labels=recipe.labels,
            created_at=recipe.created_at,
            updated_at=recipe.updated_at,
            revision=_revision(recipe),
        )

    async def _one(self, ref: str) -> tuple[Recipe, str]:
        row = await self.repo.get(ref)
        if row is None:
            raise NotFound(f"no recipe {ref!r}")
        return row[0], row.artifact_uid

    async def get(self, ref: str) -> RecipeSchema:
        return self.schema(*await self._one(ref))

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        artifact: str | None = None,
        engine: str | None = None,
        device_class: str | None = None,
        search: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> ListResponse[RecipeSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            artifact=artifact,
            engine=engine,
            device_class=device_class,
            search=search,
            labels=labels,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = [self.schema(r[0], r.artifact_uid) for r in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def create(self, data: RecipeCreateRequest) -> RecipeSchema:
        check_shape(
            device_class=data.device_class,
            device_count=data.device_count,
            tensor_parallel=data.tensor_parallel,
            pipeline_parallel=data.pipeline_parallel,
            expert_parallel=data.expert_parallel,
            context_length=data.context_length,
            kv_cache_fraction=data.kv_cache_fraction,
        )
        check_footprint(
            device_memory_mib_min=data.device_memory_mib_min,
            max_batch_tokens=data.max_batch_tokens,
            cpu_cores=data.cpu_cores,
            memory_mib=data.memory_mib,
            disk_gib=data.disk_gib,
        )
        if data.version < 1:
            raise Invalid("version must be at least 1", {"version": data.version})
        artifact = await self.repo.artifact_by_ref(data.artifact)
        if artifact is None:
            raise NotFound(f"no artifact {data.artifact!r}")

        recipe = Recipe(
            uid=new_uid(RECIPE),
            artifact_id=artifact.id,
            name=data.name,
            version=data.version,
            engine=data.engine,
            engine_version=data.engine_version,
            engine_image=data.engine_image,
            engine_args=data.engine_args,
            quantization=data.quantization,
            dtype=data.dtype,
            tensor_parallel=data.tensor_parallel,
            pipeline_parallel=data.pipeline_parallel,
            expert_parallel=data.expert_parallel,
            context_length=data.context_length,
            max_batch_tokens=data.max_batch_tokens,
            kv_cache_dtype=data.kv_cache_dtype,
            kv_cache_fraction=data.kv_cache_fraction,
            prefix_cache_enabled=data.prefix_cache_enabled,
            prefix_cache_config=data.prefix_cache_config,
            device_class=data.device_class,
            device_count=data.device_count,
            device_memory_mib_min=data.device_memory_mib_min,
            requires_single_topology_domain=data.requires_single_topology_domain,
            topology_domain_kind=data.topology_domain_kind,
            cpu_cores=data.cpu_cores,
            memory_mib=data.memory_mib,
            disk_gib=data.disk_gib,
            labels=data.labels,
        )
        try:
            await self.repo.add(recipe)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"recipe {data.name!r} version {data.version} already exists") from exc
        return await self.get(recipe.uid)

    async def update(self, ref: str, data: RecipeUpdateRequest, if_match: str | None = None) -> RecipeSchema:
        recipe, _ = await self._one(ref)
        if if_match is not None and if_match != _revision(recipe):
            raise Conflict("recipe changed since it was read", {"revision": _revision(recipe)})

        fields = {k: v for k, v in data.to_dict().items() if v is not UNSET}
        check_shape(
            device_class=fields.get("device_class", recipe.device_class),
            device_count=fields.get("device_count", recipe.device_count),
            tensor_parallel=fields.get("tensor_parallel", recipe.tensor_parallel),
            pipeline_parallel=fields.get("pipeline_parallel", recipe.pipeline_parallel),
            expert_parallel=fields.get("expert_parallel", recipe.expert_parallel),
            context_length=fields.get("context_length", recipe.context_length),
            kv_cache_fraction=fields.get("kv_cache_fraction", recipe.kv_cache_fraction),
        )
        check_footprint(
            **{
                name: fields[name]
                for name in ("device_memory_mib_min", "max_batch_tokens", "cpu_cores", "memory_mib", "disk_gib")
                if name in fields
            }
        )
        for key, value in fields.items():
            setattr(recipe, key, value)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("recipe conflicts with an existing one") from exc
        return await self.get(recipe.uid)

    async def delete(self, ref: str) -> None:
        """Refuses while a deployment still names the recipe.

        `demand_deployment.recipe_id` is `RESTRICT`: a running deployment must
        keep the description of what it is running.
        """
        recipe, _ = await self._one(ref)
        try:
            await self.repo.delete(recipe)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"recipe {ref!r} is still referenced by a deployment") from exc
