"""Read schemas to manifests. Pure: every name a uid maps to is handed in as `Refs`.

The HTTP read schemas name related objects by uid; a manifest names them the way a person writes
them (artifact handle, cluster name, deployment object name, project slug), so it can be applied
back unchanged.
"""

from dataclasses import field
from dataclasses import dataclass

import msgspec

from app.core.serving import OPENAI_CHAT
from app.modules.artifacts.schemas import ArtifactSchema
from app.modules.endpoints.schemas import EndpointSchema
from app.modules.endpoints.schemas import WorkloadRefSchema
from app.modules.endpoints.schemas import EndpointCreateRequest
from app.modules.endpoints.schemas import EndpointSurfaceSchema as _EndpointSurfaceSchema
from app.modules.endpoints.schemas import EndpointTargetRequest
from app.modules.workloads.schemas import RoleSchema as _RoleSchema
from app.modules.workloads.schemas import CacheSchema as _CacheSchema
from app.modules.workloads.schemas import ShapeSchema as _ShapeSchema
from app.modules.workloads.schemas import EnvVarSchema as _EnvVarSchema
from app.modules.workloads.schemas import RouterSchema as _RouterSchema
from app.modules.workloads.schemas import OverlaySchema as _OverlaySchema
from app.modules.workloads.schemas import RuntimeSchema
from app.modules.workloads.schemas import ServingSchema as _ServingSchema
from app.modules.workloads.schemas import TopologySchema
from app.modules.workloads.schemas import WorkloadSchema
from app.modules.workloads.schemas import ContainerSchema
from app.modules.workloads.schemas import ResourcesSchema as _ResourcesSchema
from app.modules.workloads.schemas import ParallelismSchema as _ParallelismSchema
from app.modules.workloads.schemas import CapabilitiesSchema as _CapabilitiesSchema
from app.modules.workloads.schemas import ContainerPortSchema as _ContainerPortSchema
from app.modules.workloads.schemas import WorkloadCreateRequest
from app.modules.workloads.schemas import ContainerMetricsSchema as _ContainerMetricsSchema
from app.modules.workloads.schemas import WorkloadServingRequest

from .schemas import READY
from .schemas import UNSCHEDULABLE
from .schemas import NOTE_ANNOTATION
from .schemas import PLACEMENT_DRIFT
from .schemas import Scale
from .schemas import Condition
from .schemas import ScaleSpec
from .schemas import ObjectMeta
from .schemas import RuntimeSpec
from .schemas import ScaleStatus
from .schemas import WorkloadRef
from .schemas import ArtifactSpec
from .schemas import EndpointSpec
from .schemas import PlacementSpec
from .schemas import ScaleReplicas
from .schemas import ArtifactStatus
from .schemas import EndpointStatus
from .schemas import WorkloadStatus
from .schemas import ArtifactManifest
from .schemas import EndpointManifest
from .schemas import WorkloadManifest
from .schemas import EndpointTargetSpec
from .schemas import WorkloadScaleStatus
from .schemas import WorkloadServingSpec
from .schemas import EndpointTargetStatus
from .schemas import WorkloadManifestSpec
from .schemas import WorkloadDeploymentStatus
from .schemas import camel_value as _c


@dataclass
class Refs:
    """uid to the name a manifest uses. A uid missing here is written as the uid itself."""

    projects: dict[str, str] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    clusters: dict[str, str] = field(default_factory=dict)
    deployments: dict[str, str] = field(default_factory=dict)
    workload_deployments: dict[str, list[str]] = field(default_factory=dict)
    """A workload ref's name to the uids of its current deployments; absent when it names none."""
    ready: dict[str, int] = field(default_factory=dict)
    """A deployment uid to its ready replicas across clusters; absent reads as none."""

    def project(self, uid: str | None) -> str | None:
        return None if uid is None else self.projects.get(uid, uid)

    def artifact(self, uid: str | None) -> str | None:
        return None if uid is None else self.artifacts.get(uid, uid)

    def cluster(self, uid: str | None) -> str | None:
        return None if uid is None else self.clusters.get(uid, uid)

    def deployment(self, uid: str | None) -> str | None:
        return None if uid is None else self.deployments.get(uid, uid)


@dataclass
class Placement:
    """The workload's own placement policy, which the workload read does not carry."""

    allowed_regions: list[str] = field(default_factory=list)
    allowed_clusters: list[str] = field(default_factory=list)


def _labels(labels: dict) -> dict[str, str]:
    return {str(k): str(v) for k, v in labels.items()}


def workload_conditions(w: WorkloadSchema) -> list[Condition]:
    """Derived from the scale rollup, so it can never disagree with it.

    A paused workload is placed nowhere by design and is not unschedulable. `Ready` is serving:
    every desired replica ready, like a Deployment's Available.
    """
    return [_ready(w), _unschedulable(w)]


def _ready(w: WorkloadSchema) -> Condition:
    if w.paused:
        return Condition(type=READY, status="False", reason="Paused", message="the workload is paused")
    if w.scale.ready >= w.scale.desired:
        return Condition(
            type=READY, status="True", reason="ReplicasReady", message=f"{w.scale.ready} of {w.scale.desired} ready"
        )
    return Condition(
        type=READY, status="False", reason="ReplicasNotReady", message=f"{w.scale.ready} of {w.scale.desired} ready"
    )


def _unschedulable(w: WorkloadSchema) -> Condition:
    if w.paused or w.scale.placed >= w.scale.desired:
        return Condition(type=UNSCHEDULABLE, status="False", reason="Placed")
    return Condition(
        type=UNSCHEDULABLE,
        status="True",
        reason="InsufficientCapacity",
        message=f"placement placed {w.scale.placed} of {w.scale.desired} replicas",
    )


def endpoint_ready(targets: list[EndpointTargetStatus], refs: Refs, enabled: bool) -> Condition:
    """`Ready` is routable: enabled, with at least one resolved target deployment serving."""
    if not enabled:
        return Condition(type=READY, status="False", reason="Disabled", message="the endpoint is disabled")
    serving = sum(1 for t in targets if refs.ready.get(t.deployment_id, 0) > 0)
    if serving:
        return Condition(
            type=READY, status="True", reason="Routable", message=f"{serving} of {len(targets)} targets serving"
        )
    return Condition(
        type=READY,
        status="False",
        reason="NoReadyTargets",
        message=f"none of {len(targets)} targets has a ready replica",
    )


def placement_drift(deployments, allowed_clusters: list[str]) -> Condition | None:
    """`PlacementDrift=True` while replicas sit on a cluster outside `allowed_clusters`.

    Narrowing the allowed set never moves running replicas: they stay until the workload is
    redeployed or scaled there, and this condition is how that is visible. No condition without
    an allowed set to drift from.
    """
    if not allowed_clusters:
        return None
    allowed = set(allowed_clusters)
    outside = sorted(
        {
            d.cluster_name
            for d in deployments
            if d.cluster_name not in allowed and (d.desired_replicas or d.replicas.ready)
        }
    )
    if not outside:
        return Condition(type=PLACEMENT_DRIFT, status="False", reason="WithinAllowedClusters")
    return Condition(
        type=PLACEMENT_DRIFT,
        status="True",
        reason="OutsideAllowedClusters",
        message=f"replicas run on {', '.join(outside)}, outside spec.placement.allowedClusters; "
        "they are not moved automatically",
    )


def declared_replicas(spec: WorkloadManifestSpec) -> int | None:
    """The replica count a manifest asks for. `spec.scale.desired` is a read-only alias and
    never counts; `None` leaves `desired_scale` where it is."""
    return spec.replicas


def scale_view(w: WorkloadSchema, namespace: str | None) -> Scale:
    return Scale(
        metadata=ObjectMeta(
            name=w.name,
            namespace=namespace,
            uid=w.id,
            generation=w.generation,
            creation_timestamp=w.created_at,
            resource_version=w.revision,
        ),
        spec=ScaleReplicas(replicas=w.scale.desired),
        status=ScaleStatus(replicas=w.scale.placed, ready_replicas=w.scale.ready),
    )


def workload_manifest(
    w: WorkloadSchema, refs: Refs, placement: Placement | None = None, note: str | None = None
) -> WorkloadManifest:
    s = w.serving
    placement = placement or Placement()
    return WorkloadManifest(
        metadata=ObjectMeta(
            name=w.name,
            namespace=refs.project(w.project_id),
            labels=_labels(w.labels),
            annotations={NOTE_ANNOTATION: note} if note else {},
            uid=w.id,
            generation=w.generation,
            creation_timestamp=w.created_at,
            resource_version=w.revision,
        ),
        spec=WorkloadManifestSpec(
            kind=w.kind,
            gang_size=w.gang_size,
            cluster=refs.cluster(w.pinned_cluster_id),
            priority_class=w.priority_class,
            preemptible=w.preemptible,
            paused=w.paused,
            deadline_at=w.deadline_at,
            replicas=w.scale.desired,
            scale=ScaleSpec(min=w.scale.min, max=w.scale.max, desired=w.scale.desired),
            placement=PlacementSpec(
                strategy=w.strategy,
                cluster_floors={refs.cluster(k): v for k, v in w.cluster_floors.items()},
                max_clusters=w.max_clusters,
                allowed_regions=list(placement.allowed_regions),
                allowed_clusters=list(placement.allowed_clusters),
            ),
            runtime=runtime_spec(w),
            serving=WorkloadServingSpec(
                artifact=refs.artifact(s.artifact_id),
                adapters=[refs.artifact(a) for a in w.adapter_artifact_ids],
                shape=_c(s.shape),
                context_length=s.context_length,
                max_batch_tokens=s.max_batch_tokens,
                quantization=s.quantization,
                dtype=s.dtype,
                parallelism=_c(s.parallelism),
                cache=_c(s.cache),
                api=_c(s.serving),
                capabilities=_c(s.capabilities),
                resources=_c(s.resources),
                overlay=_c(s.overlay),
                topology=s.topology.serving_topology,
                roles=[_c(r) for r in s.topology.roles],
                router=_c(s.router),
            ),
            overrides=w.overrides,
        ),
        status=WorkloadStatus(
            phase=w.phase,
            served_model_name=w.served_model_name,
            serving_digest=w.serving_digest,
            resolved_engine_args=s.resolved_engine_args,
            imported_args=s.imported_args,
            topology=_c(s.topology),
            runtime=runtime_spec(w, resolved=True),
            scale=WorkloadScaleStatus(placed=w.scale.placed, ready=w.scale.ready),
            deployments=[
                WorkloadDeploymentStatus(
                    name=d.name,
                    cluster=d.cluster_name,
                    admission_phase=d.admission_phase,
                    authority=d.authority,
                    enabled=d.enabled,
                    desired_replicas=d.desired_replicas,
                    ready_replicas=d.replicas.ready,
                )
                for d in w.deployments
            ],
            conditions=workload_conditions(w)
            + [c for c in [placement_drift(w.deployments, placement.allowed_clusters)] if c],
            artifact_tag=w.artifact_tag,
            started_at=w.started_at,
            finished_at=w.finished_at,
        ),
    )


def runtime_spec(w: WorkloadSchema, resolved: bool = False) -> RuntimeSpec | None:
    s = w.serving
    c = s.runtime.resolved if resolved else s.runtime.container
    if resolved and c is None:
        return None
    if s.runtime.kind == "container" and c is not None:
        return RuntimeSpec(
            kind="container",
            image=c.image or None,
            command=list(c.command),
            args=list(c.args),
            env=[_c(e) for e in c.env],
            ports=[_c(p) for p in c.ports],
            protocol=c.protocol,
            readiness_probe=c.readiness_probe,
            liveness_probe=c.liveness_probe,
            startup_probe=c.startup_probe,
            metrics=_c(c.metrics) if c.metrics is not None else None,
            preset=None if resolved else c.preset,
            version=(c.version or None) if c.preset and not resolved else None,
        )
    return RuntimeSpec(
        kind="dynamo",
        engine=s.engine,
        version=s.engine_runtime_version,
        image=s.engine_runtime_image,
        frontend_image=s.frontend_image,
        args=s.engine_args,
    )


def endpoint_manifest(e: EndpointSchema, refs: Refs) -> EndpointManifest:
    return EndpointManifest(
        metadata=ObjectMeta(
            name=e.name,
            namespace=refs.project(e.project_id),
            labels=_labels(e.labels),
            uid=e.id,
            generation=e.generation,
            creation_timestamp=e.created_at,
            resource_version=e.revision,
        ),
        spec=EndpointSpec(
            slug=e.slug,
            visibility=e.visibility,
            routing_strategy=e.routing_strategy,
            enabled=e.enabled,
            aliases=e.aliases,
            targets=[
                EndpointTargetSpec(
                    deployment=refs.deployment(t.deployment_id),
                    workload_ref=WorkloadRef(name=t.workload_ref.name) if t.workload_ref is not None else None,
                    weight=t.weight,
                    role=t.role,
                    enabled=t.enabled,
                    served_model_name=t.served_model_name,
                )
                for t in e.targets
            ],
            failover_order=[refs.deployment(d) for d in e.failover_order],
            surface=_c(e.surface),
        ),
        status=_endpoint_status(e, refs),
    )


def _endpoint_status(e: EndpointSchema, refs: Refs) -> EndpointStatus:
    targets = [
        EndpointTargetStatus(deployment=refs.deployment(t.deployment_id), deployment_id=t.deployment_id)
        for t in e.targets
        if t.deployment_id is not None
    ] + [
        EndpointTargetStatus(deployment=refs.deployment(uid), deployment_id=uid, workload=t.workload_ref.name)
        for t in e.targets
        if t.workload_ref is not None
        for uid in refs.workload_deployments.get(t.workload_ref.name, [])
    ]
    conditions = [Condition(type=c.type, status=c.status, reason=c.reason, message=c.message) for c in e.conditions]
    return EndpointStatus(
        targets=targets,
        conditions=[*conditions, endpoint_ready(targets, refs, e.enabled)],
    )


def artifact_manifest(a: ArtifactSchema, refs: Refs) -> ArtifactManifest:
    return ArtifactManifest(
        metadata=ObjectMeta(
            name=a.handle,
            namespace=refs.project(a.owner_project_id),
            uid=a.id,
            creation_timestamp=a.created_at,
            resource_version=a.revision,
        ),
        spec=ArtifactSpec(
            digest=a.digest,
            display_name=a.name,
            slug=a.slug,
            kind=a.kind,
            format=a.format,
            size_bytes=a.size_bytes,
            licence=a.licence,
            parameter_count=a.parameter_count,
            base_artifact=refs.artifact(a.base_artifact_id),
            project=refs.project(a.project_id),
            properties=a.metadata,
            locations=[_c(loc) for loc in a.locations],
            upstream=_c(a.upstream),
        ),
        status=ArtifactStatus(
            verified=a.verified,
            residency=sorted({loc.region for loc in a.locations if loc.region}),
        ),
    )


# --------------------------------------------------------------------------- manifests to requests (PUT)


def _snake(v, cls: type):
    """A manifest's camelCase copy of an HTTP API schema back into the schema itself."""
    return msgspec.convert(v, cls, from_attributes=True)


def topology_request(serving: WorkloadServingSpec) -> TopologySchema:
    return TopologySchema(serving_topology=serving.topology, roles=[_snake(r, _RoleSchema) for r in serving.roles])


SERVING_FIELDS = {
    # WorkloadServingRequest field: the manifest path it is read from, under `spec`.
    "engine": ("runtime", "engine"),
    "engine_runtime_version": ("runtime", "version"),
    "engine_runtime_image": ("runtime", "image"),
    "frontend_image": ("runtime", "frontend_image"),
    "engine_args": ("runtime", "args"),
    "shape": ("serving", "shape"),
    "context_length": ("serving", "context_length"),
    "max_batch_tokens": ("serving", "max_batch_tokens"),
    "quantization": ("serving", "quantization"),
    "dtype": ("serving", "dtype"),
    "parallelism": ("serving", "parallelism"),
    "cache": ("serving", "cache"),
    "serving": ("serving", "api"),
    "capabilities": ("serving", "capabilities"),
    "resources": ("serving", "resources"),
    "overlay": ("serving", "overlay"),
    "router": ("serving", "router"),
}
"""`topology` is built from `serving.topology` and `serving.roles` together."""

_SERVING_TYPES = {
    "shape": _ShapeSchema,
    "parallelism": _ParallelismSchema,
    "cache": _CacheSchema,
    "serving": _ServingSchema,
    "capabilities": _CapabilitiesSchema,
    "resources": _ResourcesSchema,
    "overlay": _OverlaySchema,
    "router": _RouterSchema,
}


def is_container(spec: WorkloadManifestSpec) -> bool:
    return spec.runtime.kind == "container"


DYNAMO_RUNTIME_FIELDS = frozenset(
    {"engine", "engine_runtime_version", "engine_runtime_image", "frontend_image", "engine_args"}
)
"""The serving fields read off `spec.runtime` under `kind: dynamo`, and not read under `container`."""


def runtime_request(spec: WorkloadManifestSpec) -> RuntimeSchema:
    """`spec.runtime` as the HTTP API's `runtime` block."""
    r = spec.runtime
    if not is_container(spec):
        return RuntimeSchema(kind=r.kind or "dynamo")
    if isinstance(r.args, dict) and r.args:
        raise ValueError("spec.runtime.args is a list of strings under kind container")
    return RuntimeSchema(
        kind="container",
        container=ContainerSchema(
            image=r.image or "",
            command=list(r.command),
            args=list(r.args) if isinstance(r.args, list) else [],
            env=[_snake(e, _EnvVarSchema) for e in r.env],
            ports=[_snake(p, _ContainerPortSchema) for p in r.ports],
            protocol=r.protocol or OPENAI_CHAT,
            readiness_probe=r.readiness_probe,
            liveness_probe=r.liveness_probe,
            startup_probe=r.startup_probe,
            metrics=_snake(r.metrics, _ContainerMetricsSchema) if r.metrics is not None else None,
            preset=r.preset,
            version=r.version or "",
        ),
    )


def serving_value(spec: WorkloadManifestSpec, key: str):
    """One `WorkloadServingRequest` field's value, read from the manifest spec."""
    if key == "topology":
        return topology_request(spec.serving)
    if key == "runtime":
        return runtime_request(spec)
    if key in DYNAMO_RUNTIME_FIELDS and is_container(spec):
        return {"engine_args": {}, "engine_runtime_image": None, "frontend_image": None}.get(key, "")
    section, attr = SERVING_FIELDS[key]
    value = getattr(getattr(spec, section), attr)
    return _snake(value, _SERVING_TYPES[key]) if key in _SERVING_TYPES else value


def workload_create(m: WorkloadManifest, project: str) -> WorkloadCreateRequest:
    """The create a manifest stands for. `replicas` omitted starts at `scale.min`; the bounds
    widen to hold it, as `/scale` does (ADR 0025)."""
    s = m.spec
    desired = s.replicas if s.replicas is not None else s.scale.min
    serving = {key: serving_value(s, key) for key in (*SERVING_FIELDS, "topology", "runtime")}
    return WorkloadCreateRequest(
        serving=WorkloadServingRequest(artifact=s.serving.artifact, **serving),
        name=m.metadata.name,
        project=project,
        cluster=s.cluster,
        allowed_regions=list(s.placement.allowed_regions),
        allowed_clusters=list(s.placement.allowed_clusters),
        priority_class=s.priority_class,
        kind=s.kind,
        min_scale=min(s.scale.min, desired),
        max_scale=max(s.scale.max, desired),
        desired_scale=desired,
        strategy=s.placement.strategy,
        cluster_floors=dict(s.placement.cluster_floors),
        max_clusters=s.placement.max_clusters,
        enabled=True if s.enabled is None else s.enabled,
        preemptible=s.preemptible,
        gang_size=s.gang_size,
        deadline_at=s.deadline_at,
        adapter_artifacts=list(s.serving.adapters),
        overrides=s.overrides,
        labels=dict(m.metadata.labels),
        note=m.metadata.annotations.get(NOTE_ANNOTATION),
    )


def endpoint_targets(spec: EndpointSpec) -> list[EndpointTargetRequest]:
    return [
        EndpointTargetRequest(
            deployment=t.deployment,
            workload_ref=WorkloadRefSchema(name=t.workload_ref.name) if t.workload_ref is not None else None,
            weight=t.weight,
            role=t.role,
            enabled=t.enabled,
            served_model_name=t.served_model_name,
        )
        for t in spec.targets
    ]


def endpoint_surface(spec: EndpointSpec) -> dict:
    return msgspec.to_builtins(_snake(spec.surface, _EndpointSurfaceSchema))


def endpoint_create(m: EndpointManifest, project: str) -> EndpointCreateRequest:
    s = m.spec
    return EndpointCreateRequest(
        name=m.metadata.name,
        project=project,
        slug=s.slug,
        visibility=s.visibility,
        routing_strategy=s.routing_strategy,
        enabled=s.enabled,
        aliases=list(s.aliases),
        targets=endpoint_targets(s),
        failover_order=list(s.failover_order),
        labels=dict(m.metadata.labels),
    )
