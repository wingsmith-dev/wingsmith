from __future__ import annotations

import datetime as dt

from uuid import UUID

from sqlalchemy import UUID as DBUUID
from sqlalchemy import Text
from sqlalchemy import Index
from sqlalchemy import String
from sqlalchemy import Boolean
from sqlalchemy import Integer
from sqlalchemy import Numeric
from sqlalchemy import DateTime
from sqlalchemy import BigInteger
from sqlalchemy import ForeignKey
from sqlalchemy import CheckConstraint
from sqlalchemy import UniqueConstraint
from sqlalchemy import text
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import relationship
from sqlalchemy.orm import mapped_column
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import BaseMixin


dUUID = text("gen_random_uuid()")


# =========================
# Supply
# =========================


class Cluster(BaseMixin):
    """A GPU cluster we own and operate, and the root of every observed record.

    The declared half: name, region, phase and the labels placement matches. `phase` is declared
    intent (an operator marks a cluster `draining`); reachability is observed and lives on
    `ClusterSyncState`. There is no address column because the plane never dials a cluster. The
    data-plane serving address lives on `ClusterServing`.
    """

    __tablename__ = "supply_cluster"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    region: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    zone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    residency: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phase: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending", index=True)
    labels: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    capabilities: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    enrolled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True, index=True)
    """sha256 of this cluster's enrolment token. The plaintext is shown once at
    register/rotate and never stored. Null means the cluster has no per-cluster
    token yet and its operator still authenticates with the shared fleet token."""

    remote_write_hash: Mapped[str | None] = mapped_column(String(72), nullable=True)
    """Bcrypt hash of the same enrolment token, checked by the metrics write ingress (basic auth).
    The reactor renders every non-null hash into the ingress htpasswd Secret. Null means the cluster
    has no metrics credential until its token is re-issued; the plaintext cannot be recovered."""

    metrics_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """The `cluster` label this cluster's series carry in the shared metrics store.
    Written by the cluster-setup chart from the cluster id. Null means not observable: metrics report
    no source instead of querying under an unconfirmed label."""

    sync_state: Mapped[ClusterSyncState | None] = relationship(
        "ClusterSyncState", back_populates="cluster", lazy="noload", uselist=False, cascade="all, delete-orphan"
    )
    serving: Mapped[ClusterServing | None] = relationship(
        "ClusterServing", back_populates="cluster", lazy="noload", uselist=False, cascade="all, delete-orphan"
    )
    nodes: Mapped[list[Node]] = relationship("Node", back_populates="cluster", lazy="noload")
    pools: Mapped[list[Pool]] = relationship("Pool", back_populates="cluster", lazy="noload")


class ClusterSyncState(BaseMixin):
    """The observed half of a cluster: is its agent connected and how current is it.

    Split from `Cluster` because only ingestion writes it. `read_only` is the operator's posture:
    true means it applies nothing the plane declares, so a workload placed here never starts.
    `multinode` means the cluster serves a leader-worker backend. `observed_region` is never merged
    into `Cluster.region`; the two disagreeing is a misconfiguration to surface.
    """

    __tablename__ = "supply_cluster_sync_state"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    cluster_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    reachable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    operator_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    distribution: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cloud: Mapped[str | None] = mapped_column(String(32), nullable=True)
    observed_region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    multinode: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    connected_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disconnected_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    cluster: Mapped[Cluster] = relationship("Cluster", back_populates="sync_state", lazy="noload")


class ClusterServing(BaseMixin):
    """Where the data plane reaches a cluster's serving runtime, and with what.

    Kept off `Cluster` because the plane never dials it: only the gateway resolves it into a
    connection. `url` is opaque. `credential_id` is whatever fronts the address, and null when a
    private in-cluster address is fronted by nothing.
    """

    __tablename__ = "supply_cluster_serving"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    cluster_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    credential_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("ops_credential.id", ondelete="SET NULL"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    cluster: Mapped[Cluster] = relationship("Cluster", back_populates="serving", lazy="noload")


class Node(BaseMixin):
    """A machine observed inside a cluster. Never declared: nodes appear, they are not written.

    Attributes are generic because the plane does not know a cluster is Kubernetes. `cordoned` is a
    boolean, since cordoning is done by an `Operation` and draining has its own lifecycle.
    """

    __tablename__ = "supply_node"
    __table_args__ = (UniqueConstraint("cluster_id", "name", name="uq_supply_node_cluster_name"),)

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    cluster_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pool_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("supply_pool.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    condition: Mapped[str] = mapped_column(String(32), nullable=False, server_default="unknown", index=True)
    conditions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    cordoned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    ready: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    instance_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    cloud: Mapped[str | None] = mapped_column(String(32), nullable=True)
    zone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cpu_cores: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_mib: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cpu_arch: Mapped[str | None] = mapped_column(String(32), nullable=True)
    os_image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kernel_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    driver_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operator_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    labels: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    taints: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    joined_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    cluster: Mapped[Cluster] = relationship("Cluster", back_populates="nodes", lazy="noload")
    pool: Mapped[Pool | None] = relationship("Pool", back_populates="nodes", lazy="noload")
    devices: Mapped[list[Device]] = relationship("Device", back_populates="node", lazy="noload")


class TopologyDomain(BaseMixin):
    """A set of devices that share an interconnect: NVLink, NVSwitch, a PCIe root, or a NUMA node.

    A table so that "eight devices in one fabric" is a join. `parent_id` nests domains.
    """

    __tablename__ = "supply_topology_domain"
    __table_args__ = (UniqueConstraint("cluster_id", "kind", "identifier", name="uq_supply_topology_domain_identity"),)

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    cluster_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("supply_topology_domain.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    identifier: Mapped[str] = mapped_column(String(128), nullable=False)
    link_generation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    link_bandwidth_gbps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    device_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    spans_nodes: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    devices: Mapped[list[Device]] = relationship(
        "Device", back_populates="topology_domain", lazy="noload", foreign_keys="Device.topology_domain_id"
    )
    members: Mapped[list[TopologyDomainNode]] = relationship(
        "TopologyDomainNode", back_populates="domain", lazy="noload", cascade="all, delete-orphan"
    )
    nodes: Mapped[list[Node]] = relationship(
        "Node", secondary="supply_topology_domain_node", lazy="noload", viewonly=True
    )


class TopologyDomainNode(BaseMixin):
    """Which nodes a topology domain spans. One row per node, written by ingestion."""

    __tablename__ = "supply_topology_domain_node"
    __table_args__ = (UniqueConstraint("domain_id", "node_id", name="uq_supply_topology_domain_node"),)

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    domain_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_topology_domain.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_node.id", ondelete="CASCADE"), nullable=False, index=True
    )

    domain: Mapped[TopologyDomain] = relationship("TopologyDomain", back_populates="members", lazy="noload")
    node: Mapped[Node] = relationship("Node", lazy="noload")


class Device(BaseMixin):
    """One accelerator, the schedulable unit of the system.

    Health, ECC and throttle state, memory and MIG profile live here. Utilization, temperature and
    power are telemetry and never enter Postgres.
    """

    __tablename__ = "supply_device"
    __table_args__ = (
        UniqueConstraint("cluster_id", "device_uuid", name="uq_supply_device_cluster_uuid"),
        Index("ix_supply_device_class_health", "cluster_id", "device_class", "health"),
    )

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    cluster_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_node.id", ondelete="CASCADE"), nullable=False, index=True
    )
    topology_domain_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("supply_topology_domain.id", ondelete="SET NULL"), nullable=True, index=True
    )
    device_uuid: Mapped[str] = mapped_column(String(128), nullable=False)
    device_index: Mapped[int] = mapped_column(Integer, nullable=False)
    device_class: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    vendor: Mapped[str] = mapped_column(String(32), nullable=False, server_default="nvidia")
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pci_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    memory_mib: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    compute_capability: Mapped[str | None] = mapped_column(String(16), nullable=True)
    health: Mapped[str] = mapped_column(String(32), nullable=False, server_default="unknown", index=True)
    health_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    ecc_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    ecc_volatile_errors: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    ecc_aggregate_errors: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    ecc_pending_retirement: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    throttled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    throttle_reasons: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, server_default="{}")
    mig_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    mig_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mig_parent_device_uuid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    allocatable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    node: Mapped[Node] = relationship("Node", back_populates="devices", lazy="noload")
    topology_domain: Mapped[TopologyDomain | None] = relationship(
        "TopologyDomain", back_populates="devices", lazy="noload", foreign_keys=[topology_domain_id]
    )


class Pool(BaseMixin):
    """A declared provisioning spec that carves a homogeneous slice out of a cluster.

    min/max are a scaling loop's inputs. This assumes the plane provisioned the cluster.
    """

    __tablename__ = "supply_pool"
    __table_args__ = (UniqueConstraint("cluster_id", "name", name="uq_supply_pool_cluster_name"),)

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    cluster_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    device_class: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    devices_per_node: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    node_shape: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    min_nodes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_nodes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    labels: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    taints: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    cluster: Mapped[Cluster] = relationship("Cluster", back_populates="pools", lazy="noload")
    nodes: Mapped[list[Node]] = relationship("Node", back_populates="pool", lazy="noload")


# =========================
# Demand
# =========================


class Artifact(BaseMixin):
    """Immutable, content-addressed bytes: weights, adapters, datasets, images.

    Identity is the digest, so the same weights registered twice are one row. There is no catalog
    in this schema; fine-tunes, quantized derivatives and adapters are all artifacts.
    """

    __tablename__ = "demand_artifact"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    digest: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # False when the digest is a reference, not content: `hf:org/model` for weights we
    # adopted by watching a cluster serve them. The row is a real artifact and workloads may point
    # at it, but nothing should mistake it for content-addressed until the bytes are resolved.
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    """What a person calls these weights. `GPT-OSS 20B`, `Llama 3.1 70B Instruct`. Display only."""
    slug: Mapped[str] = mapped_column(String(255), nullable=False, index=True, server_default="")
    """Machine name, lowercase with its organisation: `hf://openai/gpt-oss-20b` -> `openai/gpt-oss-20b`.
    Derived once from the preferred location. Not unique; identity is the digest."""
    kind: Mapped[str] = mapped_column(String(32), nullable=False, server_default="weights", index=True)
    format: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    licence: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parameter_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    base_artifact_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("demand_artifact.id", ondelete="SET NULL"), nullable=True
    )
    project_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("bind_project.id", ondelete="SET NULL"), nullable=True, index=True
    )
    upstream: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    """Facts about the model as its publisher states them, with who asserted them, from where and when.
    Asserted, never observed: nothing here reaches a cluster or is checked by ingestion."""
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")

    baselines: Mapped[list[ArtifactBaseline]] = relationship(
        "ArtifactBaseline", back_populates="artifact", lazy="noload", cascade="all, delete-orphan"
    )
    locations: Mapped[list[ArtifactLocation]] = relationship(
        "ArtifactLocation", back_populates="artifact", lazy="noload", cascade="all, delete-orphan"
    )


class ArtifactLocation(BaseMixin):
    """Where one artifact's bytes can be fetched from. An artifact may have several.

    Credentials are referenced, never inlined.
    """

    __tablename__ = "demand_artifact_location"
    __table_args__ = (UniqueConstraint("artifact_id", "uri", name="uq_demand_artifact_location_uri"),)

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    artifact_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("demand_artifact.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scheme: Mapped[str] = mapped_column(String(32), nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    credential_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("ops_credential.id", ondelete="SET NULL"), nullable=True
    )
    preferred: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    artifact: Mapped[Artifact] = relationship("Artifact", back_populates="locations", lazy="noload")


class ArtifactBaseline(BaseMixin):
    """The declared quality floor one artifact is held to, one row per dataset and metric.

    Asserted by somebody, with its source recorded. `tolerance_pct` is per-row slack; the acceptance
    floor belongs to the criteria version and is resolved at read. The provenance columns are
    spelled like the triple on `Artifact.upstream`.
    """

    __tablename__ = "demand_artifact_baseline"
    __table_args__ = (
        UniqueConstraint("artifact_id", "dataset", "metric", name="uq_demand_artifact_baseline_dataset_metric"),
    )

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    artifact_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("demand_artifact.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False)
    tolerance_pct: Mapped[float] = mapped_column(Numeric(6, 3), nullable=False, server_default="3.0")
    source_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by: Mapped[str] = mapped_column(String(255), nullable=False)
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    artifact: Mapped[Artifact] = relationship("Artifact", back_populates="baselines", lazy="noload")


class Workload(BaseMixin):
    """What you asked to run, globally. The object a user creates, scales, and deletes.

    It names an artifact, how to serve it, a kind, a priority and the TOTAL scale; placement
    expands it into one `Deployment` per cluster carrying that cluster's share. Only `service` runs
    today (`task` and `gang` are on the roadmap), and only the operator branches on `kind`.

    `engine_args` is declared and rendered. `imported_args` is read off a running process by
    discovery and never rendered, so intent stays distinguishable from capture (ADR 0009).
    `overlay` passes engine and operator configuration through untouched.

    `cluster_floors` is `{cluster_uid: int}`; unknown uids are ignored. `pinned_cluster_id` is
    checked for existence, never capacity, and forces a single deployment. `paused` is declared
    state that placement reads, set by `workload.pause`. `started_at` and `finished_at` are rollups
    derived from the deployments.
    """

    __tablename__ = "demand_workload"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_demand_workload_project_name"),)

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    project_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("bind_project.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    priority_class_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("bind_priority_class.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    artifact_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("demand_artifact.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    serving_digest: Mapped[str] = mapped_column(String(71), nullable=False, index=True)
    """`svg_` + sha256 over the fields that decide what process runs; derived, never accepted.
    Only `app.core.serving` writes it. Benchmark runs point at it."""
    engine: Mapped[str] = mapped_column(String(64), nullable=False)
    """The engine inside the runtime: `vllm`, `sglang` or `trtllm`. It picks the module and the flag
    dialect and carries no version; see `engine_runtime_version`."""
    engine_runtime_version: Mapped[str] = mapped_column(String(64), nullable=False)
    """A tag of the Dynamo runtime image (for example `1.4.1`), never the engine's own version.
    Only used to resolve the image from `ENGINE_RUNTIME_REPOS[engine]`."""
    engine_runtime_image: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """A pinned runtime image address. Null means it is resolved from `engine` and `engine_runtime_version`."""
    frontend_image: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """The image this model's Dynamo frontend runs. Null means the cluster's operator chooses, falling
    back to the runtime image; the plane never derives one."""
    engine_args: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    imported_args: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    quantization: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dtype: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tensor_parallel: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    pipeline_parallel: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    expert_parallel: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    context_length: Mapped[int] = mapped_column(Integer, nullable=False)
    max_batch_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kv_cache_dtype: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kv_cache_fraction: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    prefix_cache_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    prefix_cache_config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    device_class: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    device_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    device_memory_mib_min: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cpu_cores: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_mib: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    disk_gib: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overlay: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    serving: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    """How the served API behaves: thinking mode, reasoning and tool-call parsers, streaming defaults,
    structured output, EOS suppression. Every field renders to a flag the cluster obeys."""
    router: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    """Frontend routing settings (`mode`, `queue_threshold`, `queue_policy`, `kv_overlap_score_credit`),
    each rendered as a `DYN_ROUTER_*` variable. Only declared keys are stored; `{}` declares nothing."""
    capabilities: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    """What this serving configuration can do, asserted and never rendered to a cluster.
    Only a conformance run can falsify it."""
    min_scale: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    max_scale: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    desired_scale: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    strategy: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pack")
    cluster_floors: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    max_clusters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pinned_cluster_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="SET NULL"), nullable=True, index=True
    )
    gang_size: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    preemptible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    adapter_artifact_ids: Mapped[list[UUID]] = mapped_column(ARRAY(DBUUID), nullable=False, server_default="{}")
    overrides: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    labels: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    requires_single_topology_domain: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    topology_domain_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    serving_topology: Mapped[str] = mapped_column(String(16), nullable=False, server_default="aggregated", index=True)
    """`aggregated` or `disaggregated` (prefill and decode as separate processes sharing a KV cache).
    Under `disaggregated`, `min_replicas` counts groups of `roles`. In `serving_digest` only when disaggregated."""
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    deployments: Mapped[list[Deployment]] = relationship("Deployment", back_populates="workload", lazy="noload")
    artifact: Mapped[Artifact] = relationship("Artifact", lazy="noload")
    roles: Mapped[list[WorkloadRole]] = relationship("WorkloadRole", back_populates="workload", lazy="noload")


class WorkloadRole(BaseMixin):
    """One role in a disaggregated workload, and the little that differs about it.

    Two rows (`prefill` and `decode`) for a `disaggregated` workload, none for an `aggregated` one.
    A role is a weight, an optional `max_batch_tokens` and an argument map; everything else is
    shared with the workload. A table because placement computes fit from per-role device counts.
    """

    __tablename__ = "demand_workload_role"
    __table_args__ = (UniqueConstraint("workload_id", "role", name="uq_demand_workload_role_workload_role"),)

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    workload_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("demand_workload.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    """`prefill` or `decode`; a closed set validated on write."""
    weight: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    """Workers of this role per group, positive. A role's process count is `min_replicas * weight`.
    Not in `serving_digest`, so changing a ratio keeps the workload's benchmark history linked."""
    max_batch_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """This role's `--max-num-batched-tokens`. Null inherits the workload's."""
    engine_args: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    """This role's engine arguments, laid over the workload's. May not set a `DERIVED_FLAGS` member,
    `disaggregation-mode` or `kv-transfer-config`, which the operator derives."""

    workload: Mapped[Workload] = relationship("Workload", back_populates="roles", lazy="noload")


class WorkloadRevision(BaseMixin):
    """One serving configuration a workload has held, kept as the request that would restate it.

    Append-only, written by every write that moves `Workload.serving_digest`. Nothing reconciles
    against it. `snapshot` is a `WorkloadServingUpdate` that can be sent straight back to
    `PATCH /v1/workloads/{ref}`, so a rollback is an ordinary update.

    `number` counts from 1 per workload and never rewinds: going back to revision 2 appends a new
    revision with its snapshot. `serving_digest` is not unique here, since A to B to A gives two
    revisions with one digest.
    """

    __tablename__ = "demand_workload_revision"
    __table_args__ = (UniqueConstraint("workload_id", "number", name="uq_demand_workload_revision_number"),)

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    workload_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("demand_workload.id", ondelete="CASCADE"), nullable=False, index=True
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    serving_digest: Mapped[str] = mapped_column(String(71), nullable=False, index=True)
    artifact_uid: Mapped[str] = mapped_column(String(40), nullable=False, server_default="")
    """The weights this configuration served, as a uid instead of a foreign key so the history
    survives the artifact's deletion. Outside `snapshot` because weights are not patchable."""
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Free-text reason for the change, never parsed. The only mutable field on the row."""
    reason: Mapped[str] = mapped_column(String(32), nullable=False, server_default="updated")
    """Which write appended this: `created`, `updated`, `reshaped`, `promoted` or `imported`.
    `imported` records the configuration a workload had before its history began."""

    workload: Mapped[Workload] = relationship("Workload", lazy="noload")


class Deployment(BaseMixin):
    """One workload's share on one cluster. The unit the agent obeys.

    `min_replicas` / `max_replicas` / `desired_replicas` are THIS CLUSTER'S SHARE of the workload
    total, never the total; nothing may sum or re-derive one from the other. Admission is
    per-cluster and lives here. Changes update the row and bump `generation`; a drain or a
    re-expansion that leaves no share deletes it, which is why the stable name is on `Endpoint`.
    Placement creates deployments.

    The `adopted_*` columns are set when the deployment was promoted from a `DiscoveredWorkload`.
    The operator then takes ownership of the existing object, only while it still digests to
    `adopted_fingerprint`; a mismatch is refused and reported. Namespace, kind and name are copied at
    promotion so the adoption outlives the pruned sighting.

    `authority` is `observed` (record and verify, write nothing) or `managed` (render and converge).
    It is not derived from `adopted_workload_uid` (ADR 0009).
    """

    __tablename__ = "demand_deployment"
    __table_args__ = (
        UniqueConstraint("cluster_id", "name", name="uq_demand_deployment_cluster_name"),
        UniqueConstraint("workload_id", "cluster_id", name="uq_demand_deployment_workload_cluster"),
    )

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    workload_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("demand_workload.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cluster_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    min_replicas: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    max_replicas: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    desired_replicas: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    admission_phase: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending", index=True)
    admission_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    admitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    placed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    adopted_workload_uid: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    adopted_fingerprint: Mapped[str | None] = mapped_column(String(80), nullable=True)
    adopted_namespace: Mapped[str | None] = mapped_column(String(253), nullable=True)
    adopted_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    adopted_name: Mapped[str | None] = mapped_column(String(253), nullable=True)
    authority: Mapped[str] = mapped_column(String(16), nullable=False, server_default="observed", index=True)

    workload: Mapped[Workload] = relationship("Workload", back_populates="deployments", lazy="noload")
    replicas: Mapped[list[Replica]] = relationship("Replica", back_populates="deployment", lazy="noload")
    status: Mapped[DeploymentStatus | None] = relationship(
        "DeploymentStatus", back_populates="deployment", lazy="noload", uselist=False
    )


class DeploymentStatus(BaseMixin):
    """Observed: what a deployment is actually doing in its cluster.

    Split from `Deployment` because only ingestion writes it. `reason` and `message` are the
    cluster's sub-state within `phase`, not an error; `reason` is unconstrained so a new operator
    sub-state shows up without a plane deploy. The replica counts are the cluster's own snapshot
    from one frame, and complement the rollup over `demand_replica`.
    """

    __tablename__ = "demand_deployment_status"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    deployment_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("demand_deployment.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    cluster_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), nullable=False, index=True
    )
    observed_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    phase: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending", index=True)
    desired_replicas: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    ready_replicas: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_replicas: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    unavailable_replicas: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    roles: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    """Per-role counts (including desired) as the cluster reported them in the same frame as the
    scalars. Read whole, never queried. Empty on an aggregated deployment."""
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    deployment: Mapped[Deployment] = relationship("Deployment", back_populates="status", lazy="noload")


class Replica(BaseMixin):
    """One running instance of a deployment, observed. Written only by ingestion.

    Carries readiness, the engine version actually running (which may lag during a rollout) and the
    node. Its devices are in `Allocation`.
    """

    __tablename__ = "demand_replica"
    __table_args__ = (UniqueConstraint("cluster_id", "name", name="uq_demand_replica_cluster_name"),)

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    deployment_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("demand_deployment.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cluster_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("supply_node.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending", index=True)
    ready: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    ready_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    """`prefill` or `decode` as read off the pod label; null for an aggregated deployment."""
    engine_runtime_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """The runtime tag the operator copied from the deployment when this replica started."""
    serving_digest: Mapped[str | None] = mapped_column(String(71), nullable=True, index=True)
    """The serving digest this replica was rendered with, read off its annotation.
    Compare with `Workload.serving_digest` to see a rollout in progress. Null means unknown."""
    observed_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    restart_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    deployment: Mapped[Deployment] = relationship("Deployment", back_populates="replicas", lazy="noload")


class DiscoveredWorkload(BaseMixin):
    """A workload found running in a cluster that the plane never declared.

    Observed, written only by ingestion, and never a `Deployment`: declaring it would make a
    reconciler restart a process that is already serving. `uid` is minted from a natural key
    (ADR 0002), so a pruned and rediscovered sighting keeps its identity.
    """

    __tablename__ = "demand_discovered_workload"
    __table_args__ = (
        UniqueConstraint("cluster_id", "namespace", "name", "kind", name="uq_demand_discovered_workload_identity"),
    )

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    cluster_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), nullable=False, index=True
    )
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    images: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    replicas_desired: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    replicas_ready: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    devices_per_replica: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    devices_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    node_uids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    labels: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    spec_fingerprint: Mapped[str | None] = mapped_column(String(80), nullable=True)
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    first_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    cluster: Mapped[Cluster] = relationship("Cluster", lazy="noload")


class WorkloadAdoption(BaseMixin):
    """What we decided about a sighting: track it, or stop showing it.

    Written only by the API and keyed by `workload_uid`, so the decision survives the sighting being
    pruned. Nothing here is streamed to a cluster: adopting a running model must not restart it.
    """

    __tablename__ = "demand_workload_adoption"
    __table_args__ = (UniqueConstraint("cluster_id", "workload_uid", name="uq_demand_workload_adoption_identity"),)

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    cluster_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workload_uid: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    project_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("bind_project.id", ondelete="SET NULL"), nullable=True, index=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Endpoint(BaseMixin):
    """The stable public name clients call. Plane-only: it never syncs to a cluster.

    Separate from deployments so weighted splits, canaries and renames do not break clients.
    `routing_strategy` and `failover_order` are stored and returned; cross-cluster failover is on
    the roadmap.

    `name` is globally unique while there is one tenant. It becomes `(project_id, name)` when a
    second tenant appears, and the gateway's routing key has to include the project then.

    The name the engine answers to is `Artifact.slug`; the gateway rewrites between the two (see
    `app.core.serving.served_model`).
    """

    __tablename__ = "demand_endpoint"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    project_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("bind_project.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    """The internal handle. In URLs, in metrics labels, in the console; unique, and not patchable,
    because an endpoint exists so that deployments can be replaced without anybody noticing and
    renaming it spends exactly that."""
    slug: Mapped[str] = mapped_column(String(255), nullable=False, index=True, server_default="")
    """The model id clients send, such as `openai/gpt-oss-20b`. Patchable, unlike `name`.
    Defaults to the artifact's slug."""
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, server_default="private")
    routing_strategy: Mapped[str] = mapped_column(String(32), nullable=False, server_default="weighted")
    failover_order: Mapped[list[UUID]] = mapped_column(ARRAY(DBUUID), nullable=False, server_default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    surface: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    """How the served API is exposed: which protocols answer, how callers authenticate, and the
    rate-limit policy the gateway reads. Never synced to a cluster."""
    labels: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    targets: Mapped[list[EndpointTarget]] = relationship(
        "EndpointTarget", back_populates="endpoint", lazy="noload", cascade="all, delete-orphan"
    )
    aliases: Mapped[list[EndpointAlias]] = relationship(
        "EndpointAlias", back_populates="endpoint", lazy="noload", cascade="all, delete-orphan"
    )


class EndpointAlias(BaseMixin):
    """An additional public model id that resolves to one endpoint.

    The gateway turns an alias into the canonical `slug` before any other stage, so every record
    stays keyed on the slug and a rename leaves the old id as an alias. `alias` is globally unique;
    clashes with another endpoint's slug are refused by the service, since no constraint spans the
    two tables. Stored lowercased and resolved case-insensitively.
    """

    __tablename__ = "demand_endpoint_alias"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    endpoint_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("demand_endpoint.id", ondelete="CASCADE"), nullable=False, index=True
    )
    alias: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)

    endpoint: Mapped[Endpoint] = relationship("Endpoint", back_populates="aliases", lazy="noload")


class EndpointTarget(BaseMixin):
    """One deployment behind one endpoint, with a traffic weight.

    `deployment_id` cascades, and nothing retargets the endpoint at a replacement deployment:
    re-point the endpoint after moving a workload.
    """

    __tablename__ = "demand_endpoint_target"
    __table_args__ = (UniqueConstraint("endpoint_id", "deployment_id", name="uq_demand_endpoint_target"),)

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    endpoint_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("demand_endpoint.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deployment_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("demand_deployment.id", ondelete="CASCADE"), nullable=False, index=True
    )
    weight: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default="primary")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    served_model_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """Overrides the model name the engine behind this target answers to; null asks the deployment.
    The gateway rewrites the inbound `model` to it."""

    endpoint: Mapped[Endpoint] = relationship("Endpoint", back_populates="targets", lazy="noload")
    deployment: Mapped[Deployment] = relationship("Deployment", lazy="noload")


class WarmSet(BaseMixin):
    """Declared weight-cache intent: which artifacts should be resident on a cluster.

    Observed counterpart: `CacheEntry`. KV and prefix caches are unrelated and configured on
    `Workload`.
    """

    __tablename__ = "demand_warm_set"
    __table_args__ = (UniqueConstraint("cluster_id", "artifact_id", name="uq_demand_warm_set_cluster_artifact"),)

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    cluster_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("demand_artifact.id", ondelete="CASCADE"), nullable=False, index=True
    )
    eviction_policy: Mapped[str] = mapped_column(String(32), nullable=False, server_default="lru")
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    min_replicas_resident: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


# =========================
# Binding
# =========================


class Tenant(BaseMixin):
    """The billing and isolation root. Projects hang off it; quota attaches below it.

    `ingress` is the listener the tenant's traffic arrives on, `auth` whether the gateway checks a
    key there, and `host` the name checked against the request's `Host` header (ADR 0016).
    """

    __tablename__ = "bind_tenant"
    __table_args__ = (
        CheckConstraint("ingress IN ('internal', 'public')", name="ck_bind_tenant_ingress"),
        CheckConstraint("auth IN ('none', 'api_key')", name="ck_bind_tenant_auth"),
    )

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    ingress: Mapped[str] = mapped_column(String(16), nullable=False, server_default="internal")
    auth: Mapped[str] = mapped_column(String(16), nullable=False, server_default="api_key")
    host: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)

    projects: Mapped[list[Project]] = relationship("Project", back_populates="tenant", lazy="noload")
    api_keys: Mapped[list[TenantAPIKey]] = relationship("TenantAPIKey", back_populates="tenant", lazy="noload")


class TenantToken(BaseMixin):
    """A console credential that signs in as one tenant, and only as that tenant.

    Not an inference key; only the plane resolves it. Only the sha256 is stored (high-entropy, one
    indexed lookup). Many per tenant for rotation, revoked instead of deleted.
    """

    __tablename__ = "bind_tenant_token"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("bind_tenant.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    """sha256 of the plaintext, which is shown once at mint and is not recoverable."""
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Stamped on a successful sign-in, so a token nobody uses can be told from one
    somebody depends on before it is revoked. Written at most once a minute (see
    `AuthService.resolve_tenant_token`); a login is not a metric."""
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped[Tenant] = relationship("Tenant", lazy="noload")


class TenantAPIKey(BaseMixin):
    """An inference credential, checked by the gateway on a tenant's public listener.

    Separate from `TenantToken` so console and inference credentials revoke independently. Only the
    sha256 is stored; one key per caller.
    """

    __tablename__ = "bind_tenant_api_key"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("bind_tenant.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    """sha256 of the plaintext, which is shown once at mint and is not recoverable."""
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="api_keys", lazy="noload")


class Project(BaseMixin):
    """The unit quota, priority, and workload ownership attach to, inside a tenant."""

    __tablename__ = "bind_project"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_bind_project_tenant_slug"),)

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("bind_tenant.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    default_priority_class_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("bind_priority_class.id", ondelete="SET NULL"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="projects", lazy="noload")
    quotas: Mapped[list[Quota]] = relationship("Quota", back_populates="project", lazy="noload")


class Quota(BaseMixin):
    """A guaranteed device floor plus a fair share of otherwise-idle capacity.

    Per project and device class, optionally scoped to one cluster (`cluster_id` null is
    fleet-wide). Both numbers are admission inputs; reclaiming by preemption is on the roadmap.
    """

    __tablename__ = "bind_quota"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "cluster_id",
            "device_class",
            name="uq_bind_quota_scope",
            postgresql_nulls_not_distinct=True,
        ),
    )
    """`cluster_id` is null for a fleet-wide quota, and `NULLS NOT DISTINCT` is what makes a
    second fleet-wide quota for the same project and class a conflict, not a second row."""

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    project_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("bind_project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cluster_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), nullable=True, index=True
    )
    device_class: Mapped[str] = mapped_column(String(64), nullable=False)
    guaranteed_devices: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    limit_devices: Mapped[int | None] = mapped_column(Integer, nullable=True)
    over_quota_weight: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False, server_default="1")
    over_quota_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    project: Mapped[Project] = relationship("Project", back_populates="quotas", lazy="noload")


class ServingQuota(BaseMixin):
    """Committed request rate, token rate and concurrency for one endpoint.

    Unrelated to `Quota`: this is traffic, enforced per request by the gateway. Every limit is
    nullable and null means uncommitted, never zero; no row means no rate limit.

    `success_rate_objective` is a fraction in (0, 1], declared and enforced by nothing, and only
    meaningful within the committed capacity above it. `availability_objective` names an entry of
    the frozen objectives table. `enforced` false is shadow mode: the gateway counts but never
    refuses.
    """

    __tablename__ = "bind_serving_quota"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    endpoint_id: Mapped[UUID] = mapped_column(
        DBUUID,
        ForeignKey("demand_endpoint.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    committed_rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    committed_tpm: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_concurrent_requests: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    burst_rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ramp_up_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    on_exceed: Mapped[str] = mapped_column(String(16), nullable=False, server_default="reject")
    queue_timeout_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    limit_scope: Mapped[str] = mapped_column(String(16), nullable=False, server_default="per_replica")
    """`per_replica`: each gateway replica enforces the full limit (R replicas admit up to R times it).
    `divided`: each enforces `limit / replicas`. There is no shared counter."""
    success_rate_objective: Mapped[float | None] = mapped_column(Numeric(6, 5), nullable=True)
    availability_objective: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="availability-2026.1"
    )
    objective_window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="600")
    enforced: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    endpoint: Mapped[Endpoint] = relationship("Endpoint", lazy="noload")


class PriorityClass(BaseMixin):
    """What would preempt what. Referenced by every schedulable workload.

    Preemption is on the roadmap: placement can name victims (`PreemptionPlan`) but nothing evicts
    them. `value` is unique in the database so eviction order is deterministic under concurrent
    writes.
    """

    __tablename__ = "bind_priority_class"
    __table_args__ = (UniqueConstraint("value", name="uq_bind_priority_class_value"),)

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    preemptible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    preempts: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class Policy(BaseMixin):
    """A placement constraint: region, residency, device class, allowed clusters.

    Subjects are attached through `PolicyAttachment`.
    """

    __tablename__ = "bind_policy"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    allowed_regions: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, server_default="{}")
    allowed_residencies: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, server_default="{}")
    allowed_device_classes: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, server_default="{}")
    allowed_cluster_ids: Mapped[list[UUID]] = mapped_column(ARRAY(DBUUID), nullable=False, server_default="{}")
    denied_cluster_ids: Mapped[list[UUID]] = mapped_column(ARRAY(DBUUID), nullable=False, server_default="{}")
    rules: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    attachments: Mapped[list[PolicyAttachment]] = relationship(
        "PolicyAttachment", back_populates="policy", lazy="noload", cascade="all, delete-orphan"
    )


class PolicyAttachment(BaseMixin):
    """Binds a policy to a tenant, project, cluster, endpoint or workload.

    `subject_id` has no foreign key. An `AFTER DELETE` trigger on each subject table (revision
    `9d05d968be9c`) deletes the attachments naming the deleted row. A new subject type needs a
    trigger in a new revision; `tests/test_models.py` checks every type in `app.modules.policies`
    has one.
    """

    __tablename__ = "bind_policy_attachment"
    __table_args__ = (UniqueConstraint("policy_id", "subject_type", "subject_id", name="uq_bind_policy_attachment"),)

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    policy_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("bind_policy.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(DBUUID, nullable=False, index=True)

    policy: Mapped[Policy] = relationship("Policy", back_populates="attachments", lazy="noload")


class Allocation(BaseMixin):
    """Observed: a set of devices committed together to one workload.

    The allocation is the unit of commitment, so a gang takes all N devices or none. The unique
    constraint on `AllocationDevice.device_id` stops two live allocations claiming one accelerator.
    `deployment_id` is the workload's share in this cluster, which is what preemption reasons about.
    """

    __tablename__ = "bind_allocation"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    cluster_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workload_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("demand_workload.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deployment_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("demand_deployment.id", ondelete="CASCADE"), nullable=True, index=True
    )
    replica_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("demand_replica.id", ondelete="CASCADE"), nullable=True, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("bind_project.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    device_class: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    device_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    over_quota: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    phase: Mapped[str] = mapped_column(String(16), nullable=False, server_default="held", index=True)
    acquired_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    devices: Mapped[list[AllocationDevice]] = relationship(
        "AllocationDevice", back_populates="allocation", lazy="noload", cascade="all, delete-orphan"
    )


class AllocationDevice(BaseMixin):
    """One device inside one allocation.

    A partial unique index on `device_id` for live allocations enforces that an
    accelerator is committed to at most one workload at a time while still
    retaining released rows for history.
    """

    __tablename__ = "bind_allocation_device"
    __table_args__ = (
        UniqueConstraint("allocation_id", "device_id", name="uq_bind_allocation_device"),
        Index(
            "uq_bind_allocation_device_live",
            "device_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    allocation_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("bind_allocation.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_device.id", ondelete="CASCADE"), nullable=False, index=True
    )
    released_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    allocation: Mapped[Allocation] = relationship("Allocation", back_populates="devices", lazy="noload")


# =========================
# Machinery
# =========================


class Component(BaseMixin):
    """Declared: something we install into a cluster at a version.

    The agent, the serving runtime, exporters, collectors, the cache tier, the gateway and
    observability all use this one path.
    """

    __tablename__ = "ops_component"
    __table_args__ = (UniqueConstraint("cluster_id", "name", name="uq_ops_component_cluster_name"),)

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    cluster_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    desired_version: Mapped[str] = mapped_column(String(64), nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    status: Mapped[ComponentStatus | None] = relationship(
        "ComponentStatus", back_populates="component", lazy="noload", uselist=False, cascade="all, delete-orphan"
    )


class ComponentStatus(BaseMixin):
    """Observed: what version of a component is actually running, and how the rollout is going.

    Split from `Component` because only ingestion writes it.
    """

    __tablename__ = "ops_component_status"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    component_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("ops_component.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    cluster_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), nullable=False, index=True
    )
    observed_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rollout_phase: Mapped[str] = mapped_column(String(32), nullable=False, server_default="unknown", index=True)
    healthy: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    component: Mapped[Component] = relationship("Component", back_populates="status", lazy="noload")


class Operation(BaseMixin):
    """An imperative, long-running, failable action against some target.

    One table with a typed payload for drain, cordon, reboot and the rest. `target_id` has no
    foreign key: an operation outlives its target. `phase` is ingestion's and `cancel_requested_at`
    the plane's; cancelling is a request the cluster observes. `idempotency_key` is a column so a
    unique index can stop concurrent retries.
    """

    __tablename__ = "ops_operation"
    __table_args__ = (
        Index("ix_ops_operation_target", "target_type", "target_id"),
        Index(
            "uq_ops_operation_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    cluster_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="SET NULL"), nullable=True, index=True
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[UUID] = mapped_column(DBUUID, nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending", index=True)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cancel_requested_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    deadline_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BenchmarkRequest(BaseMixin):
    """The ask: what was to be measured, at which level, and against which criteria version.

    Declared, and the parent of the `BenchmarkRun`s it expands into (one per input-length band in a
    sweeping `mode`). `level` names which target column is set; the check constraint allows at most
    one, and the service checks they agree. There is no phase, summary or verdict: run phases are
    observed and verdicts are derived at read.
    """

    __tablename__ = "ops_benchmark_request"
    __table_args__ = (
        CheckConstraint(
            "(deployment_id IS NOT NULL)::int + (workload_id IS NOT NULL)::int + (endpoint_id IS NOT NULL)::int <= 1",
            name="ck_ops_benchmark_request_one_target",
        ),
    )

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    deployment_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("demand_deployment.id", ondelete="SET NULL"), nullable=True, index=True
    )
    workload_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("demand_workload.id", ondelete="SET NULL"), nullable=True, index=True
    )
    endpoint_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("demand_endpoint.id", ondelete="SET NULL"), nullable=True, index=True
    )
    harness: Mapped[str] = mapped_column(String(32), nullable=False)
    image: Mapped[str] = mapped_column(String(512), nullable=False, server_default="")
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="single")
    criteria_version: Mapped[str] = mapped_column(String(32), nullable=False)
    tier: Mapped[str | None] = mapped_column(String(8), nullable=True)
    coverage: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    homogeneous: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    runs: Mapped[list[BenchmarkRun]] = relationship("BenchmarkRun", back_populates="request", lazy="noload")


class BenchmarkRun(BaseMixin):
    """One measurement of one deployment by one harness, and what it measured.

    The plane writes the top half once at creation; ingestion writes `phase`, `summary`,
    `artifacts_uri`, `harness_version` and the timestamps from `benchmark.observed`.

    `image` is what the run pulled, recorded because the default moves between releases. `summary`
    is the harness's own output, stored verbatim; rows compare by `harness` and `harness_version`.
    `serving_digest`, `deployment_generation` and the `engine_args` snapshot attribute the number to
    a configuration after the deployment is gone. `operation_id` is `ON DELETE SET NULL`.
    """

    __tablename__ = "ops_benchmark_run"
    __table_args__ = (Index("ix_ops_benchmark_run_deployment_seq", "deployment_id", "seq"),)

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    request_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("ops_benchmark_request.id", ondelete="CASCADE"), nullable=True, index=True
    )
    deployment_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("demand_deployment.id", ondelete="SET NULL"), nullable=True, index=True
    )
    endpoint_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("demand_endpoint.id", ondelete="SET NULL"), nullable=True, index=True
    )
    cluster_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="SET NULL"), nullable=True, index=True
    )
    operation_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("ops_operation.id", ondelete="SET NULL"), nullable=True, index=True
    )
    harness: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    harness_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image: Mapped[str] = mapped_column(String(512), nullable=False, server_default="")
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    model: Mapped[str] = mapped_column(String(255), nullable=False)
    deployment_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    serving_digest: Mapped[str | None] = mapped_column(String(71), nullable=True, index=True)
    engine_args: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    serving: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    step_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    vantage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discard: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    phase: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending", index=True)
    summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    artifacts_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The harness's stdout, tail-capped by the operator at ~64 KiB before it ever reaches
    # here. Sized so a failed run's traceback is there in full, and small enough that the
    # sync stream never carries more than one screenful of it.
    log_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    request: Mapped[BenchmarkRequest | None] = relationship("BenchmarkRequest", back_populates="runs", lazy="noload")


class Event(BaseMixin):
    """Observed, append-only: node joined, device degraded, load failed, OOM, preemption.

    State, not telemetry. `subject_id` has no foreign key so an event outlives its subject.
    `app.db.retention` (the `prune-events` mode) deletes events past the retention window once the
    reactor's `last_event_seq` has passed them.
    """

    __tablename__ = "ops_event"
    __table_args__ = (
        Index("ix_ops_event_subject", "subject_type", "subject_id"),
        Index("ix_ops_event_cluster_occurred", "cluster_id", "occurred_at"),
        Index(
            "uq_ops_event_occurrence",
            "cluster_id",
            "type",
            "subject_type",
            "occurred_at",
            text("md5(message)"),
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )
    """One occurrence per cluster, type, subject kind, instant and message; a replayed frame is a no-op.
    NULLS NOT DISTINCT covers plane-raised events (null cluster); `message` is hashed to bound the
    index entry."""

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    cluster_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="SET NULL"), nullable=True
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, server_default="info", index=True)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[UUID | None] = mapped_column(DBUUID, nullable=True)
    operation_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("ops_operation.id", ondelete="SET NULL"), nullable=True, index=True
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CacheEntry(BaseMixin):
    """Observed residency of an artifact on a cluster or a specific node.

    Compared with `WarmSet` and read by the router. `node_id` is null for cluster-level tiers.
    """

    __tablename__ = "ops_cache_entry"
    __table_args__ = (
        UniqueConstraint(
            "cluster_id",
            "node_id",
            "artifact_id",
            name="uq_ops_cache_entry_scope",
            postgresql_nulls_not_distinct=True,
        ),
    )
    """NULLS NOT DISTINCT so cluster-scoped residency (null `node_id`) upserts into one row."""

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    cluster_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("supply_node.id", ondelete="CASCADE"), nullable=True, index=True
    )
    artifact_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("demand_artifact.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tier: Mapped[str] = mapped_column(String(32), nullable=False, server_default="node")
    state: Mapped[str] = mapped_column(String(32), nullable=False, server_default="absent", index=True)
    bytes_resident: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Credential(BaseMixin):
    """Declared, scoped secrets: registry pulls, HF tokens, artifact-store access.

    Never cluster-admin. The secret is a reference to an external store. `scope_id` has no foreign
    key and is null for a fleet-scoped credential; deleting the scoped object leaves the row.
    """

    __tablename__ = "ops_credential"
    __table_args__ = (
        UniqueConstraint(
            "scope_type",
            "scope_id",
            "name",
            name="uq_ops_credential_scope_name",
            postgresql_nulls_not_distinct=True,
        ),
    )
    """`scope_id` is null for a fleet-scoped credential; `NULLS NOT DISTINCT` keeps its name unique."""

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_id: Mapped[UUID | None] = mapped_column(DBUUID, nullable=True)
    secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# =========================
# Auth
# =========================


class AuthRefreshToken(BaseMixin):
    """A stored refresh token that survives server restarts and can be revoked.

    Only the sha256 is stored. `name` is the principal (currently always `admin`). `replaced_by`
    chains rotations: only `revoked_at` means logged out, both means rotated and then revoked.
    """

    __tablename__ = "auth_refresh_token"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    """sha256 of the plaintext refresh token."""
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_token_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("bind_tenant_token.id", ondelete="CASCADE"), nullable=True, index=True
    )
    """The tenant credential this session was opened with, or null for an admin. Revoking the token
    ends its sessions at the next refresh; deleting the tenant ends them outright."""
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


# =========================
# Hub
# =========================


class HubClusterClaim(BaseMixin):
    """Which hub session currently owns a cluster's stream (ADR 0005).

    Exactly one session writes for a `cluster_id` at a time. A `hello` claims the row with an upsert
    that always wins; the displaced session finds out by polling. Keyed by the wire `cluster_id`, not
    a foreign key, because clusters are resolved lazily per frame.
    """

    __tablename__ = "hub_cluster_claim"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    cluster_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    session_id: Mapped[str] = mapped_column(String(32), nullable=False)
    instance: Mapped[str] = mapped_column(String(128), nullable=False)
    claimed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    heartbeat_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# =========================
# Reactor
# =========================


class Alert(BaseMixin):
    """A condition worth waking someone for, and the record of who has seen it.

    Derived: the reactor writes everything except the acknowledgement. `dedupe_key` is the identity;
    a re-fire after a resolve reopens the same row.

    `state` is `pending` (true, not yet for long enough; nothing sent), `firing` (notified once),
    `acknowledged` (seen, silent) or `resolved`. Condition alerts resolve automatically when the
    evaluator stops seeing them; event alerts are resolved by hand, since an event never un-happens.
    """

    __tablename__ = "ops_alert"
    __table_args__ = (
        Index("ix_ops_alert_state_seq", "state", "seq"),
        Index("ix_ops_alert_cluster", "cluster_id"),
    )

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    rule: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="condition")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, server_default="warning", index=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending", index=True)

    cluster_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="SET NULL"), nullable=True
    )
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """The subject's public id, not a foreign key, so the alert outlives the row it is about."""

    title: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fired_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    acknowledged_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    acknowledged_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    notified_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    notified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notify_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The last delivery failure. An alert is due for notification when its notifiable `state`
    differs from `notified_state`; with more than one channel, one failing marks it notified for all."""


class AlertThreshold(BaseMixin):
    """An operator-declared threshold on a named metric. Declared state, written through the API.

    `subject_uid` null is the fleet default for the rule; a uid overrides it. The scope index is
    `NULLS NOT DISTINCT` so a rule has one fleet default. No foreign key on `subject_uid`. `lookback`
    because `WINDOW` is reserved in Postgres.
    """

    __tablename__ = "ops_alert_threshold"
    __table_args__ = (
        Index(
            "uq_ops_alert_threshold_scope",
            "rule",
            "subject_type",
            "subject_uid",
            "dimensions",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    rule: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dimensions: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    value: Mapped[float] = mapped_column(Numeric, nullable=False)
    objective: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lookback: Mapped[str | None] = mapped_column(String(16), nullable=True)
    for_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class NotificationChannel(BaseMixin):
    """Where a firing alert is sent. Declared configuration, written through the API.

    `config` holds the credential (for `slack`, the webhook URL) and is never returned by the API.
    `min_severity` filters per channel.
    """

    __tablename__ = "ops_notification_channel"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    min_severity: Mapped[str] = mapped_column(String(16), nullable=False, server_default="warning")
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class ReactorState(BaseMixin):
    """The reactor's lease and its cursor, in one row because they are one fact.

    One row, `default`; holding the lease entitles a process to evaluate and notify. The lease is a
    compare-and-swap on `holder` and `lease_expires_at` so it is visible to a select.
    `last_event_seq` is the cursor into `ops_event.seq`, so a reactor that was down catches up.
    """

    __tablename__ = "ops_reactor_state"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    holder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    last_tick_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
