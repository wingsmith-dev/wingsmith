"""Read schemas to manifests. Pure: every name a uid maps to is handed in as `Refs`.

The HTTP read schemas name related objects by uid; a manifest names them the way a person writes
them (artifact handle, cluster name, deployment object name, project slug), so it can be applied
back unchanged.
"""

from dataclasses import field
from dataclasses import dataclass

from app.modules.artifacts.schemas import ArtifactSchema
from app.modules.endpoints.schemas import EndpointSchema
from app.modules.workloads.schemas import WorkloadSchema

from .schemas import NOTE_ANNOTATION
from .schemas import ScaleSpec
from .schemas import ObjectMeta
from .schemas import RuntimeSpec
from .schemas import ArtifactSpec
from .schemas import EndpointSpec
from .schemas import PlacementSpec
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


@dataclass
class Refs:
    """uid to the name a manifest uses. A uid missing here is written as the uid itself."""

    projects: dict[str, str] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    clusters: dict[str, str] = field(default_factory=dict)
    deployments: dict[str, str] = field(default_factory=dict)

    def project(self, uid: str | None) -> str | None:
        return None if uid is None else self.projects.get(uid, uid)

    def artifact(self, uid: str | None) -> str | None:
        return None if uid is None else self.artifacts.get(uid, uid)

    def cluster(self, uid: str | None) -> str | None:
        return None if uid is None else self.clusters.get(uid, uid)

    def deployment(self, uid: str) -> str:
        return self.deployments.get(uid, uid)


@dataclass
class Placement:
    """The workload's own placement policy, which the workload read does not carry."""

    allowed_regions: list[str] = field(default_factory=list)
    allowed_clusters: list[str] = field(default_factory=list)


def _labels(labels: dict) -> dict[str, str]:
    return {str(k): str(v) for k, v in labels.items()}


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
            scale=ScaleSpec(min=w.scale.min, max=w.scale.max, desired=w.scale.desired),
            placement=PlacementSpec(
                strategy=w.strategy,
                cluster_floors={refs.cluster(k): v for k, v in w.cluster_floors.items()},
                max_clusters=w.max_clusters,
                allowed_regions=list(placement.allowed_regions),
                allowed_clusters=list(placement.allowed_clusters),
            ),
            runtime=RuntimeSpec(
                kind="dynamo",
                engine=s.engine,
                version=s.engine_runtime_version,
                image=s.engine_runtime_image,
                frontend_image=s.frontend_image,
                args=s.engine_args,
            ),
            serving=WorkloadServingSpec(
                artifact=refs.artifact(s.artifact_id),
                adapters=[refs.artifact(a) for a in w.adapter_artifact_ids],
                shape=s.shape,
                context_length=s.context_length,
                max_batch_tokens=s.max_batch_tokens,
                quantization=s.quantization,
                dtype=s.dtype,
                parallelism=s.parallelism,
                cache=s.cache,
                api=s.serving,
                capabilities=s.capabilities,
                resources=s.resources,
                overlay=s.overlay,
                topology=s.topology.serving_topology,
                roles=s.topology.roles,
                router=s.router,
            ),
            overrides=w.overrides,
        ),
        status=WorkloadStatus(
            phase=w.phase,
            served_model_name=w.served_model_name,
            serving_digest=w.serving_digest,
            resolved_engine_args=s.resolved_engine_args,
            imported_args=s.imported_args,
            topology=s.topology,
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
            artifact_tag=w.artifact_tag,
            started_at=w.started_at,
            finished_at=w.finished_at,
        ),
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
                    weight=t.weight,
                    role=t.role,
                    enabled=t.enabled,
                    served_model_name=t.served_model_name,
                )
                for t in e.targets
            ],
            failover_order=[refs.deployment(d) for d in e.failover_order],
            surface=e.surface,
        ),
        status=EndpointStatus(
            targets=[
                EndpointTargetStatus(deployment=refs.deployment(t.deployment_id), deployment_id=t.deployment_id)
                for t in e.targets
            ]
        ),
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
            locations=a.locations,
            upstream=a.upstream,
        ),
        status=ArtifactStatus(
            verified=a.verified,
            residency=sorted({loc.region for loc in a.locations if loc.region}),
        ),
    )
