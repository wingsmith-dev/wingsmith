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

    The declared half is the registry entry: name, region, phase, and the labels
    placement matches against. `phase` is declared intent — an operator marks a
    cluster `draining` — and is deliberately not the same axis as reachability,
    which is observed and lives on `ClusterSyncState`. There is no address column:
    the plane never dials a cluster, so a cluster's identity is the credential its
    agent presents, not a URL the plane could call. The one address a cluster
    can carry is its data-plane serving address, and it lives on `ClusterServing`.
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
    """Bcrypt hash of the *same* enrolment token, for the metrics write ingress.

    Two verifiers over one credential: the hub compares `token_hash` in constant
    time after an indexed lookup, and Traefik's `basicAuth` middleware in front of
    the central Prometheus checks this one, because a crypt-style hash is the only
    thing it can check. The reactor renders every non-null hash here into the
    htpasswd Secret that middleware reads, so a cluster's telemetry credential and
    its hub credential are the same string and rotate in one act (ADR 0012).

    Null means no bcrypt verifier exists, and it cannot be backfilled — the
    plaintext is unrecoverable. A cluster registered before this column existed
    must re-issue its token to gain one."""

    metrics_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """The `cluster` label this cluster's series are stamped with in the shared store.

    Not an address, so it breaks no invariant above: the plane still never dials a
    cluster, and this names a value to match on inside a store the plane already
    holds one endpoint for. It is declared state and it is the cluster's own fact —
    `libs/chart/cluster-setup` writes `external_labels.cluster` from the same
    `cluster.id` the plane renders into the install values, so on an enrolled
    cluster this is that identity rather than a guess at it.

    Null means "not observable", and it is honoured as such: metrics report no
    source for the cluster rather than querying the shared store under a label
    nobody has confirmed. A clean empty there would read as a healthy idle fleet,
    which is the one wrong answer that looks right (ADR 0008, ADR 0011)."""

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

    Split from `Cluster` because the two have different writers. `Cluster` is
    written only by the API; this row is written only by ingestion, on every
    connect, heartbeat, and disconnect. Folding `reachable` into `Cluster.phase`
    would give one record two writers and make "declared draining" and "observed
    unreachable" indistinguishable.

    `read_only` is the operator's own posture rather than the cluster's: true means
    it runs without its reconcile loop and applies nothing the plane declares. It
    is observed for the same reason the rest of this row is — only the operator
    knows it — and it is what lets a caller say that a workload placed here will
    never start, instead of leaving somebody to infer it from a replica count that
    never moves.

    `multinode` is what the operator found in the cluster's API groups: a
    leader-worker backend, so Dynamo can render a replica across machines. Observed
    rather than configured because the CRDs are either served or they are not, and
    a flag asserting it is a second copy of the same truth that can be wrong in the
    direction that hangs.

    `distribution`, `cloud` and `observed_region` are what the cluster turned out
    to be. They live here rather than on `Cluster` for the same reason
    `reachable` does — they are observed, and writing them onto the registry row
    would give it a second writer. `observed_region` is deliberately not merged
    into `Cluster.region`: the declared region is what placement matches policies
    against, and the two disagreeing is a misconfiguration worth being able to
    see rather than one silently overwriting the other.
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

    Deliberately not a column on `Cluster`. The plane still never dials a cluster
    — this row is configuration the plane stores and hands out, and the gateway is
    the only thing that ever resolves it into a connection. Keeping it separate
    keeps the registry row free of an address that would read as one the control
    plane could use.

    `url` is opaque: a public ingress, an in-cluster service name, anything the
    gateway can dial. `credential_id` points at whatever the address is fronted
    by — an access token, a signed header, mTLS material — and is nullable
    because a private in-cluster address is fronted by nothing.
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
    """A machine observed inside a cluster. Never declared — nodes appear, they are not written.

    Attributes here are deliberately generic (os image, kernel, driver, agent
    version): the plane does not know a cluster is Kubernetes, so no runtime- or
    orchestrator-specific version field belongs on this table. `cordoned` is an
    observed boolean rather than a value in a condition enum, because cordoning is
    something an `Operation` does and draining has a lifecycle of its own.
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
    """A set of devices that share an interconnect — NVLink, NVSwitch, a PCIe root, or a NUMA node.

    First-class rather than a label on `Device` so that "eight devices in one
    fabric" is a join, not a string comparison. `kind` names what is shared and
    `parent_id` nests domains, so a NUMA domain can contain PCIe roots which
    contain NVLink islands.
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
    """Which nodes a topology domain spans. One row per node, written by ingestion.

    `topology.observed` has always carried the full `node_ids`, but the domain
    held a single nullable `node_id` — so a fabric across four machines stored a
    null and the membership survived only on the wire. A domain with one member
    and a domain with none were the same row, which made "the domains on this
    node" a question the database could not answer. This table is that answer,
    and it replaces the scalar rather than sitting beside it: two spellings of
    one fact is how they drift apart.
    """

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
    """One accelerator. The schedulable unit of the whole system.

    There is no `gpu_count` anywhere above this table, because a count cannot say
    which of eight GPUs is throttling, which has ECC errors, and which is already
    committed. Health, ECC and throttle state, memory, and MIG profile live here;
    utilization, temperature, and power do not — those are telemetry and never
    enter Postgres.
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

    Declared rather than a selector over observed nodes because we own the
    substrate: min/max are a scaling loop's inputs, not a description. This is the
    one object in the supply group that breaks if the plane ever manages a cluster
    it did not provision.
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

    Identity is the digest, so the same weights registered twice are one row. A
    fine-tune output, a private customer checkpoint, a quantized derivative, and a
    LoRA adapter are all artifacts — none of them are catalog entries, and the
    catalog is deliberately absent from this schema.
    """

    __tablename__ = "demand_artifact"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    digest: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # False when the digest is a reference rather than content — `hf:org/model` for weights we
    # adopted by watching a cluster serve them. The row is a real artifact and recipes may point
    # at it, but nothing should mistake it for content-addressed until the bytes are resolved.
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
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
    """Reference facts about the model as published upstream, asserted and never observed.

    Context window, multimodality, parameter count as the publisher states them, plus the
    provenance triple that says who asserted it, from where, and when. It is `metadata`'s
    opposite in exactly the way `Recipe.imported_args` is `Recipe.engine_args`: one is a claim
    about the world that somebody signed, the other is a scratchpad. Nothing here reaches a
    cluster, nothing here is checked by ingestion, and a wrong value changes only what we told
    somebody.
    """
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")

    baselines: Mapped[list[ArtifactBaseline]] = relationship(
        "ArtifactBaseline", back_populates="artifact", lazy="noload", cascade="all, delete-orphan"
    )
    locations: Mapped[list[ArtifactLocation]] = relationship(
        "ArtifactLocation", back_populates="artifact", lazy="noload", cascade="all, delete-orphan"
    )


class ArtifactLocation(BaseMixin):
    """Where one artifact's bytes can be fetched from. An artifact may have several.

    Separate from `Artifact` because locations are mutable and plural while the
    artifact is neither. Credentials are referenced, never inlined.
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

    Declared, not observed and not derived. Somebody asserts that this checkpoint scores this
    number on this eval, records where the number came from, and signs it; an acceptance review
    is then an argument about that assertion rather than about whichever number a harness
    happened to produce last. A quantized derivative that regresses is only visible against a
    baseline that was written down before the quantization was run.

    `tolerance_pct` is the per-row slack, separate from the acceptance floor a verdict applies:
    the floor is a property of the criteria version and is resolved at read, and burying it in a
    column here would let two artifacts be graded against two silently different standards.

    `source_uri`, `recorded_by` and `recorded_at` are spelled the same as the provenance triple
    on `Artifact.upstream` on purpose. Two spellings of "who said this and when" is one argument
    nobody can follow.
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


class Recipe(BaseMixin):
    """A fully-specified, reproducible way to serve an artifact.

    The load-bearing object of the demand group: engine and version, quantization,
    tensor/pipeline/expert parallelism, context length, KV and prefix cache
    configuration, engine arguments, and the device shape that all of that
    implies. `device_class` is required, not optional — you cannot tensor-parallel
    across an A100 and an H100, so a shape without a class is not a shape.
    Context length lives here rather than on any catalog entry because it
    determines the footprint.

    `engine_args` and `imported_args` are the same shape and different in kind.
    `engine_args` is declared: somebody asserted it, and it renders onto the
    command line. `imported_args` is observed: it was read off a running process
    by the discovery path, it is recorded in full, and it is never rendered. The
    split is provenance, and provenance is what makes taking management of an
    adopted workload reviewable — folded into one map there is no way to tell
    intent from incidental capture, and the first render is a guess about which
    values somebody meant (ADR 0009).

    `overlay` is the one opaque field on an otherwise fully-modelled object.
    Engines and the Kubernetes operators that run them carry far more
    configuration than a control plane wants to mirror field by field, and
    mirroring it is a losing race against every engine release — so the recipe
    models what the plane reasons about and passes the rest through untouched.
    """

    __tablename__ = "demand_recipe"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_demand_recipe_name_version"),)

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    artifact_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("demand_artifact.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False, server_default="published", index=True)
    """Declared publication state: `draft`, `published` or `deprecated`.

    A plain writable field, not an operation: publishing a recipe changes nothing on any cluster,
    it only governs whether the recipe is offered for new workloads. `deprecated` keeps existing
    workloads running while steering new ones elsewhere.
    """
    engine: Mapped[str] = mapped_column(String(64), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(64), nullable=False)
    engine_image: Mapped[str | None] = mapped_column(String(512), nullable=True)
    frontend_image: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """The image this model's own Dynamo frontend runs. Null means the cluster decides.

    Separate from `engine_image` because a frontend and a worker are separately versioned
    binaries that only happen to be published from one repository. The worker loads the weights;
    the frontend tokenizes the request and registers the model, and the tables it needs to do that
    are compiled into it. Kimi K3 is the case: a worker on the stock
    `nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.4.0-patched.1` served it, while every released
    frontend refused the `kimi_k3` tiktoken type and answered `/v1/models` with an empty list. The
    fix was a frontend build and nothing else. Folded into `engine_image`, that one model would
    have moved the image every model on the cluster is served through.

    Null is this recipe declining to have an opinion, not an instruction to reuse `engine_image`.
    The operator resolves it against what that cluster runs its frontends on — a local fact the
    plane does not hold and must not guess at — and only falls back to the engine image once that
    has declined too. So there is no `resolve_` for this field the way there is for `engine_image`:
    the plane can derive an engine image because `ENGINE_IMAGE_REPOS` tells it where workers come
    from, and nothing tells it the same about frontends, so an address invented here is an
    `ImagePullBackOff` chosen by the wrong side of the wire.
    """
    engine_args: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    imported_args: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    served_model_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """The model id an API client sends, when this recipe declares one. Null means derive it.

    Spelled the way vLLM and the OpenAI API spell it, and the way the escape hatch in
    `engine_args["served-model-name"]` already spells it: one vocabulary for one string, rather
    than a third name for the thing the flag is called. Null is the ordinary case and resolves to
    the artifact's lowercase Hugging Face slug — see `app.core.serving.served_model`, which owns
    the order and is the only thing entitled to answer this question.
    """
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
    requires_single_topology_domain: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    topology_domain_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cpu_cores: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_mib: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    disk_gib: Mapped[int | None] = mapped_column(Integer, nullable=True)
    labels: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    overlay: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    serving: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    """How the served API behaves: thinking mode, the reasoning and tool-call parsers, streaming
    defaults, structured-output support, EOS suppression.

    Every field here renders to a flag or a rendered value, travels on `recipe.desired`, and is
    obeyed by the cluster — which is the whole test for what belongs here rather than in
    `capabilities`. Until this column existed these settings vanished into the `engine_args`
    passthrough, where the plane could neither validate them nor say what it had asked for.
    """
    capabilities: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    """What this recipe's serving configuration can do, asserted and never rendered.

    The same split as `engine_args` against `imported_args`, one level up: nothing here reaches a
    cluster, because a cluster has no use for a claim about itself. A wrong value changes only
    what we told somebody, and the only thing that can falsify it is a conformance run.
    """

    artifact: Mapped[Artifact] = relationship("Artifact", lazy="noload")


class Workload(BaseMixin):
    """What you asked to run, globally. The object a user creates, scales, and deletes.

    A workload is not tied to a cluster. It declares a recipe, a kind, a priority, and TOTAL
    scale across every cluster it is allowed to use; placement expands it into one `Deployment`
    per cluster, and each deployment carries that cluster's share of the total. `kind`
    discriminates `service | task | gang`, and nothing below this object is allowed to branch on
    it — preemption and quota stay one code path across serving and batch. The only component
    entitled to read `kind` is the operator, when it decides what Kubernetes object to render.

    Splittability is a property of `kind`, not a field: `service` and `task` may fan out,
    `gang` is atomic and always has exactly one deployment. The schema permits N; placement
    constrains a gang to 1.

    Admission is per-cluster and therefore lives on `Deployment`, not here. `started_at` and
    `finished_at` are rollups the plane derives from its deployments, never written by a cluster.

    `cluster_floors` is `{cluster_uid: int}`, a per-cluster minimum share keyed by `clu_...` uid
    rather than by database id; an unknown uid is ignored, never an error. `pinned_cluster_id` is
    the deliberate scheduler escape: checked for existence, never for capacity, and it forces a
    single deployment. `paused` is declared state that placement reads, not an operation status —
    `workload.pause` is the operation that sets it.
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
    recipe_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("demand_recipe.id", ondelete="RESTRICT"), nullable=False, index=True
    )
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
    served_model_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """This workload's override of the model id clients send, beating the recipe's when set.

    Both levels exist because a recipe is shared and a workload is not: the recipe says what the
    model is normally called, and a workload serving the same recipe as a canary or under a
    tenant's own name says so here without forking the recipe. Null is the ordinary case.
    """
    overrides: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    labels: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    requires_single_topology_domain: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    topology_domain_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    deployments: Mapped[list[Deployment]] = relationship("Deployment", back_populates="workload", lazy="noload")
    recipe: Mapped[Recipe] = relationship("Recipe", lazy="noload")


class Deployment(BaseMixin):
    """One workload's share on one cluster. The unit the agent obeys.

    A deployment is recipe x cluster, and that has not changed. What changed is what sits above
    it: a `Workload` is now its parent rather than its supertype, and one workload has many
    deployments — one per cluster it was expanded onto. `min_replicas` / `max_replicas` /
    `desired_replicas` are THIS CLUSTER'S SHARE of the workload total, never the total. Nothing
    anywhere may sum a share against a total or re-derive one from the other.

    Recipe, adapters, overrides, labels and the topology pair live on the workload: they are
    facts about what was asked for, and a cluster does not get its own answer to them. Admission
    is per-cluster and lives here, because a workload can be running in one region and queued on
    quota in another and there is no single phase that is true of both.

    Deployments are disposable — recreated on every recipe bump, engine upgrade, and drain —
    which is why the public name lives on `Endpoint` instead. You do not create one: placement
    creates it when it expands a workload.

    `adopted_workload_uid` is set when this deployment was promoted from a workload that was
    already running in the cluster (a `DiscoveredWorkload`, unrelated to the parent `Workload`
    above). It changes what the cluster is being asked to do: the object exists, so the operator
    takes ownership rather than applying, and only while the live object still digests to
    `adopted_fingerprint`. A mismatch is refused in the cluster and reported, never converged.

    `adopted_namespace` and `adopted_kind` name that object, and they are copied here at
    promotion rather than read back off the `DiscoveredWorkload` they came from. The sighting is
    observed state and the snapshot prunes it the moment a cluster stops mentioning it; the
    adoption is declared state and outlives that. Deriving the wire's `adopt` block from the
    sighting made a deployment's declared identity depend on a row that comes and goes, and a
    prune window was enough to send `adopt: null` to a cluster that then dropped an adopted
    deployment to one it had nothing to do with — permanently, since nothing re-dispatches when
    the sighting returns.

    `adopted_name` is stored beside them rather than taken from `name`, because promotion accepts
    a name and only defaults it to the workload's. A renamed adoption still has to point the
    operator at the object that actually exists in the cluster.

    `authority` is the one word that answers who owns this: `observed` means the plane records
    and verifies it and writes nothing to the cluster, `managed` means the plane renders and
    converges it. It is deliberately not derived from `adopted_workload_uid` (ADR 0009).
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

    A separate table from `Deployment` for the same reason `ComponentStatus` is
    separate from `Component` — declared and observed have different writers, and
    a nullable `phase` column on the declared row would let an API write land on
    top of a cluster's. The pair read together is what "the deployment is behind"
    means.

    `reason` and `message` are the cluster's sub-state within `phase`, and are
    deliberately not `last_error`: waiting for weights to download is neither an
    error nor a thing that should survive into the next observation the way an
    error does. `reason` is a closed set the operator owns and this column does
    not constrain — an unrecognised value is stored and displayed rather than
    rejected, so a new sub-state does not need a plane deploy to become visible.

    The replica counts here are the cluster's own accounting and are deliberately
    redundant with the rollup over `demand_replica`. They arrive in one frame and
    are internally consistent at the instant the cluster took them, which a rollup
    over rows written by many frames is not; the rollup answers which replicas,
    this answers how many the cluster thinks there are.
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
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    deployment: Mapped[Deployment] = relationship("Deployment", back_populates="status", lazy="noload")


class Replica(BaseMixin):
    """One running instance of a deployment, observed. Written only by ingestion.

    Carries readiness and the engine version actually running — which may lag the
    recipe's during a rollout — and the node it landed on. The devices it holds
    are in `Allocation`, not here, because a gang commits many devices at once.
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
    engine_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    restart_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    deployment: Mapped[Deployment] = relationship("Deployment", back_populates="replicas", lazy="noload")


class DiscoveredWorkload(BaseMixin):
    """A workload found running in a cluster that the plane never declared.

    Observed, and written only by ingestion. Deliberately not a `Deployment`: a
    deployment is something the plane declared and the cluster obeys, so writing
    a sighting into that table would manufacture a difference between declared
    and running state on the spot — and reconciling that difference means
    restarting a process whose weights are already in VRAM. This table says only
    "this exists", which is a fact no reconciler can act on.

    `uid` is minted by the cluster from a natural key (ADR 0002), so a sighting
    pruned on one resync and rediscovered on the next comes back with the same
    identity — which is what lets an adoption decision outlive it.
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

    A separate row from the sighting because the two have different writers —
    ingestion owns `DiscoveredWorkload` and only the API writes here. Folding the
    decision into the observed row would give that table two writers and make an
    operator's choice something a resync could overwrite.

    Keyed by `workload_uid` rather than a foreign key, for the same reason: the
    sighting is pruned whenever a snapshot says the workload is gone, and a
    decision that vanishes with it would have to be made again every time a
    cluster reconnects mid-snapshot.

    Nothing here is ever streamed to a cluster. Tracking is a decision to watch,
    not a decision to declare, and there is no `deployment.desired` at the end of
    it — which is the whole point: adopting five running models must not restart
    any of them.
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
    """The stable public name clients call. Plane-only — it never syncs to a cluster.

    Global where a deployment is per-cluster, and a contract where a deployment is
    disposable. Keeping them separate is what allows weighted splits, canaries,
    cross-cluster failover, and renaming without breaking clients; merging them
    would give one globally unique record N cluster-side writers.

    `name` is globally unique, which makes this the only child object in the schema
    that reaches past its parent for uniqueness — `Workload` is unique on
    `(project_id, name)` and `Project` on `(tenant_id, slug)`. That is deliberate
    while there is one tenant, because a single namespace makes global and
    project-scoped the same thing, and it is the cheaper of the two to change
    later: with no colliding rows the migration is a constraint swap. The scope
    becomes `(project_id, name)` when a second tenant appears, and the routing key
    the gateway resolves has to grow the project alongside it — `model` is the only
    key an OpenAI-compatible client sends, so a name must resolve to exactly one
    endpoint within whatever scope the caller belongs to.

    Not to be confused with the name the engine answers to. That is the recipe's,
    it is usually a derived configuration slug, and the gateway rewrites between
    the two — see `app.core.serving.served_model`.
    """

    __tablename__ = "demand_endpoint"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    project_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("bind_project.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    model_slug: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    """The model id clients send, when it should not be `name`. Null means it is.

    `name` is the internal handle — it is in URLs, in metrics labels and in the console, it is
    unique, and it is deliberately not patchable, because an endpoint exists so that deployments
    can be replaced without anybody noticing and renaming it spends exactly that. None of which a
    caller should have to care about: `model` in an OpenAI request is a public contract, it wants
    to read `moonshotai/kimi-k3` rather than whatever the endpoint was called when it was made,
    and it has to be changeable without breaking the handle everything else holds.

    So the two are split and this one is patchable. It is what the resolved table reports as the
    endpoint's name, which is the key the gateway routes on, so changing it costs one poll — no
    engine flag, no pod respec, nothing on any cluster. Unique where set, because `model` is the
    only key an OpenAI-compatible client sends and it must resolve to exactly one endpoint.
    """
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, server_default="private")
    routing_strategy: Mapped[str] = mapped_column(String(32), nullable=False, server_default="weighted")
    failover_order: Mapped[list[UUID]] = mapped_column(ARRAY(DBUUID), nullable=False, server_default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    surface: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    """The terms on which the served API is exposed: which protocols answer at this name
    (`/v1/chat/completions`, `/v1/responses`, `/v1/messages`), how a caller authenticates, and the
    rate-limit policy the gateway reads.

    It sits here rather than on the recipe because honouring any of it needs no engine flag and an
    endpoint never syncs to a cluster. Some of it the gateway acts on and some of it is a
    disclosure the gateway only reports; the schema layer says which, per field.
    """
    labels: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    targets: Mapped[list[EndpointTarget]] = relationship(
        "EndpointTarget", back_populates="endpoint", lazy="noload", cascade="all, delete-orphan"
    )


class EndpointTarget(BaseMixin):
    """One deployment behind one endpoint, with a traffic weight.

    The many-to-many between global names and per-cluster deployments. It exists
    as a table rather than as gateway configuration so that a canary or a
    weighted split is versioned and auditable like anything else.
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
    """What the engine behind this target answers to, when the plane's answer is wrong.

    Null is the ordinary case and means "ask the deployment". This is the correction, and it
    exists because the routing table is ours: the engine's name is invisible to clients, so the
    only thing that matters is that this table agrees with the process. When it does not — a
    recipe carrying an `engine_args` override the plane derived around, an adopted workload
    already serving under a name nobody here chose — the fix is a write here.

    Per target rather than per endpoint because one endpoint fronts many deployments and they
    need not agree; per target rather than on the deployment because the deployment is disposable
    and this correction should outlive a recipe bump. The gateway rewrites the inbound `model` to
    it, so changing it costs a routing-table poll and nothing restarts.
    """

    endpoint: Mapped[Endpoint] = relationship("Endpoint", back_populates="targets", lazy="noload")
    deployment: Mapped[Deployment] = relationship("Deployment", lazy="noload")


class WarmSet(BaseMixin):
    """Declared weight-cache intent: which artifacts should be resident on a cluster.

    The declared half of the weight cache. Its observed counterpart is
    `CacheEntry`, in the machinery group. The KV and prefix caches are unrelated
    to this and are configuration on `Recipe` — collapsing the two would produce
    an object half desired-state and half runtime.
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
    """The billing and isolation root. Projects hang off it; quota attaches below it."""

    __tablename__ = "bind_tenant"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    projects: Mapped[list[Project]] = relationship("Project", back_populates="tenant", lazy="noload")


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

    Per project, per device class, optionally scoped to one cluster. Two numbers
    rather than one cap: a hard limit leaves expensive silicon idle while someone
    queues, and over-quota-with-preemption is the primitive that makes mixed
    inference and training economics work at all.
    """

    __tablename__ = "bind_quota"
    __table_args__ = (UniqueConstraint("project_id", "cluster_id", "device_class", name="uq_bind_quota_scope"),)

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

    The second kind of quota, and unrelated to `Quota` above: that one is devices, checked once by
    placement at admission, and its usage is a join this database owns. This one is traffic,
    checked on every request by the gateway, and its usage is a measurement the plane never sees.
    No field is shared and none should be — "over quota" has to keep meaning exactly one thing.

    Every limit is nullable and null means uncommitted, never zero. An endpoint with no row here
    is not rate limited; an endpoint with a row and `committed_rpm` null is committed on tokens and
    not on requests. Absent is not a limit of nothing.

    `success_rate_objective` is declared and enforced by nothing. It sits here rather than on its
    own object because a success rate is uninterpretable without the load ceiling it was promised
    under: 99.9% is a commitment *within committed capacity*, and the committed capacity is the
    three fields above it. It is a fraction in (0, 1].

    `availability_objective` names an entry of the frozen objectives table, not a metric. The
    entry decides which outcomes sit in the numerator and the denominator; this row declares the
    interpretation and can never redefine the capture.

    `enforced` is declared state and not a status: `false` is shadow mode, where the gateway counts
    and reports but never refuses. Turning it on is a field write that takes effect on the
    gateway's next resync, not an operation with phases.
    """

    __tablename__ = "bind_serving_quota"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    endpoint_id: Mapped[UUID] = mapped_column(
        DBUUID,
        ForeignKey("demand_endpoint.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
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
    """Whether each gateway replica enforces the whole declared number or its share of it.

    `per_replica` has every replica enforce the full limit, so a fleet of R admits up to R times
    what was sold; `divided` has each enforce `limit / replicas`, so an uneven connection balance
    refuses callers who are inside the limit they were sold. There is no shared counter and no
    coordination hop in the request path, so one of those two error directions has to be chosen
    rather than avoided. `per_replica` is the default because over-admission is a commercial leak
    that shows up in the usage panel, while a spurious 429 is a visible breach of what we sold.
    """
    success_rate_objective: Mapped[float | None] = mapped_column(Numeric(6, 5), nullable=True)
    availability_objective: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="availability-2026.1"
    )
    objective_window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="600")
    enforced: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    endpoint: Mapped[Endpoint] = relationship("Endpoint", lazy="noload")


class PriorityClass(BaseMixin):
    """What preempts what. Referenced by every schedulable workload from day one.

    Only two values are needed today; the point is that the reference exists now
    so that preemption is a policy change later rather than a migration across
    every workload. `value` is unique in the database and not merely checked in
    the service: two classes at one number make eviction order depend on whatever
    the planner returns that day, and a service-level check alone loses that race
    between two concurrent writers.
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

    Attachable to more than one kind of subject, so the subject reference lives in
    `PolicyAttachment` rather than as a nullable column per subject type here.
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
    """Binds a policy to a tenant, project, endpoint, or cluster.

    A polymorphic pair of `subject_type` and `subject_id` rather than four
    nullable foreign keys, because the set of attachable subjects is expected to
    grow and each addition would otherwise be a column.
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

    The allocation, not its members, is the unit of commitment — a gang takes all
    N devices or none, so the atomicity has to live on a row that can be created
    once. Members are rows in `AllocationDevice`, whose unique constraint on
    device is what stops two live allocations claiming the same accelerator.

    `workload_id` names the global workload and `deployment_id` names its share in
    this cluster. Preemption reasons about deployments — evicting "a workload" from
    a cluster means evicting the deployment it has there — so the per-cluster
    identity is carried alongside the global one.
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

    The agent, the serving runtime, exporters, collectors, the cache tier, the
    gateway — all the same kind of thing. Observability included, deliberately:
    the moment it gets a bespoke path, the cache tier wants one too.
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

    A separate table from `Component` rather than a nullable `observed_version`
    column on it, because declared and observed have different writers. The pair
    read together is what "component is behind" means.
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

    Drain, cordon, reboot, device reset, cache warm, rolling update, component
    upgrade — one table with a typed payload rather than a status column on each
    object acted upon. A status field cannot answer who did this, why, how long
    it has been stuck, or what happens at the deadline. `target_type` /
    `target_id` are polymorphic because the set of targets spans four groups.

    `phase` is ingestion's and `cancel_requested_at` is the plane's, disjoint the
    way `Cluster.phase` and `ClusterSyncState` are: asking a running operation to
    stop is a request the cluster observes, not a phase the plane may assert on
    its behalf. `idempotency_key` is a column rather than a key inside `payload`
    because only a unique index can stop two concurrent retries both inserting.
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
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), nullable=True, index=True
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

    Declared state, and the parent of every `BenchmarkRun` it expands into. It exists because a
    run is a single measurement at a single load point and an acceptance claim is not: the
    standard sets a separate time-to-first-token target for each input-length band, so one request
    in a sweeping `mode` fans out to one run per band and each run records its own `step_index`.
    Without a parent row the bands are unrelated numbers and the claim cannot be re-aggregated
    afterwards from anything.

    `level` says which of the three targets is populated — a run against a deployment measures one
    cluster's configuration, a run against an endpoint measures what a caller actually gets, and
    the two are different claims that must not be averaged together. Exactly one target column is
    non-null, which the check constraint enforces; that `level` agrees with which one is a service
    check, because the constraint cannot see the string.

    There is no `phase`, no `summary` and no `verdict` here, deliberately. A request is declared, a
    run's phase is observed, and a verdict is derived at read against a pinned criteria version.
    Any of the three on this table would be an operation modelled as a status field.
    """

    __tablename__ = "ops_benchmark_request"
    __table_args__ = (
        CheckConstraint(
            "(deployment_id IS NOT NULL)::int + (workload_id IS NOT NULL)::int + (endpoint_id IS NOT NULL)::int = 1",
            name="ck_ops_benchmark_request_one_target",
        ),
    )

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    deployment_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("demand_deployment.id", ondelete="CASCADE"), nullable=True, index=True
    )
    workload_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("demand_workload.id", ondelete="CASCADE"), nullable=True, index=True
    )
    endpoint_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("demand_endpoint.id", ondelete="CASCADE"), nullable=True, index=True
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

    Declared and observed in the same two halves as everything else. The plane
    writes the top half once at creation — which deployment, which harness, what
    load, and the provenance columns that say what was running at the time — and
    ingestion writes `phase`, `summary`, `artifacts_uri`, `harness_version` and
    the timestamps from `benchmark.observed` thereafter. Neither writer touches
    the other's columns after creation.

    `image` is what the run actually pulled, resolved at creation from the harness
    default or from a per-run override. It is a provenance column like the others:
    the default is a pin that moves between releases of this plane, so a run that
    did not record its own image could not say afterwards what produced its
    numbers.

    `summary` is the harness's own output shape, stored verbatim. There is no
    normalised metric schema here on purpose: comparing AIPerf to vLLM's
    benchmark script is a question nobody has asked yet, and inventing the union
    of two result formats before either has been read in anger would fix the
    wrong shape permanently. What makes the rows comparable is `harness` and
    `harness_version` — a percentile definition that changed between releases is
    exactly what a same-tool comparison gets wrong without them.

    The provenance columns are the reason this table is worth keeping. A run
    without `deployment_generation`, `recipe_id`, `recipe_version` and
    `engine_args` is a number with nothing to attribute it to; with them the
    table is the parameter-tuning record. They are snapshotted rather than
    joined because the recipe they name is mutable and the deployment is
    disposable, and a run must still describe what it measured after both have
    moved on.

    `operation_id` is nullable and `ON DELETE SET NULL`: the operation is how the
    run was carried out and the run is the record of it, so the record outlives
    the errand.
    """

    __tablename__ = "ops_benchmark_run"
    __table_args__ = (
        Index("ix_ops_benchmark_run_deployment_seq", "deployment_id", "seq"),
        CheckConstraint(
            "deployment_id IS NOT NULL OR endpoint_id IS NOT NULL",
            name="ck_ops_benchmark_run_owner",
        ),
    )

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    request_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("ops_benchmark_request.id", ondelete="CASCADE"), nullable=True, index=True
    )
    deployment_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("demand_deployment.id", ondelete="CASCADE"), nullable=True, index=True
    )
    endpoint_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("demand_endpoint.id", ondelete="CASCADE"), nullable=True, index=True
    )
    cluster_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), nullable=True, index=True
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
    recipe_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("demand_recipe.id", ondelete="SET NULL"), nullable=True, index=True
    )
    recipe_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    engine_args: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    serving: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    step_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    vantage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discard: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    phase: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending", index=True)
    summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    artifacts_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    request: Mapped[BenchmarkRequest | None] = relationship("BenchmarkRequest", back_populates="runs", lazy="noload")


class Event(BaseMixin):
    """Observed, append-only: node joined, device degraded, load failed, OOM, preemption.

    State, not telemetry — discrete, low-volume, and worth joining against. Metric
    streams do not come through here and never enter Postgres.
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
    """One occurrence is cluster, type, subject kind, instant and message, and the replay of a
    frame must land on the row it already wrote rather than beside it. `cluster_id` is null for
    events the plane itself raises, and under the default rule two nulls never conflict — so
    without `NULLS NOT DISTINCT` the constraint would exclude exactly the rows nothing else
    de-duplicates. `message` is hashed because it is unbounded text and a btree entry over about
    2.7kB is refused on insert: a long message must not turn a duplicate into a wedged stream."""

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    uid: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    cluster_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), nullable=True
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

    Read against the declared `WarmSet` to say whether intent has been met, and
    read by the router, which can only be cache-aware if it knows where weights
    actually are. `node_id` is nullable because residency may be a cluster-level
    tier rather than a local disk.
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
    """`node_id` is null for cluster-scoped residency, and by default Postgres considers two nulls
    distinct — which would make the cluster-scoped row un-upsertable and insert a fresh duplicate on
    every observation. `NULLS NOT DISTINCT` is what makes "the cluster tier holds this artifact" a
    single row rather than an append-only log of the same fact."""

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

    Never cluster-admin — the plane holds no credential that can drive a cluster,
    which is the whole point of the agent dialling out. The secret material is
    stored by reference to an external secret store rather than inline.
    """

    __tablename__ = "ops_credential"
    __table_args__ = (UniqueConstraint("scope_type", "scope_id", "name", name="uq_ops_credential_scope_name"),)

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

    Only the sha256 of the plaintext is stored; the plaintext is returned to the
    client once at login/rotate and never recoverable. `name` is the principal the
    session runs as (currently always `admin`).

    `replaced_by` makes rotation an auditable chain: a token that has been rotated
    points at the hash of its successor, which itself may point further. A token
    with only `revoked_at` was explicitly logged out; one with both was rotated and
    then the successor's chain was revoked.
    """

    __tablename__ = "auth_refresh_token"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    """sha256 of the plaintext refresh token."""
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


# =========================
# Hub
# =========================


class HubClusterClaim(BaseMixin):
    """Which hub session currently owns a cluster's stream (ADR 0005).

    `SessionState` — cursors and the replay buffer — is deliberately in-process and not
    shared across hub replicas. What must be shared is the single fact that makes sharing
    it unnecessary: exactly one session may write for a given `cluster_id` at a time. A
    `hello` claims the row with an atomic upsert that always wins, since a new `hello` is
    definitionally the newer claim; the session it displaces discovers the loss by polling
    this row rather than being told, since it may be mid-stream on another pod entirely.

    Keyed by the wire `cluster_id`, not a foreign key into `supply_cluster` — the hub
    resolves clusters lazily per frame (see `app.ingestion.sink`) and a claim must be
    takeable before that resolution has ever happened.
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

    Derived rather than observed: nothing on a cluster reports an alert, and
    nothing declares one. The reactor writes every column here except the
    acknowledgement, which is the one field a human writes, and it is the reason
    this is a table at all — an alert that cannot be acknowledged is a log line.

    `dedupe_key` is the identity. One key is one ongoing problem however many
    times it is evaluated or re-reported, so a deployment that has been behind
    for seventeen hours is one row with a rising `occurrences`, not a row per
    tick. It is unique, and a re-fire after a resolve reopens the same row rather
    than starting a second history of the same fact.

    `state` is a lifecycle, not a severity:

    - `pending` — the condition is true but has not been true for long enough to
      be worth a notification. This is where flapping goes to die, and where an
      alert with a `for` window waits. Nothing is sent for a pending alert.
    - `firing` — true for longer than the rule's window. Notified once on entry.
    - `acknowledged` — a human has seen it. Still true, still listed, silent.
    - `resolved` — no longer true. Auto-set for condition-sourced alerts when the
      evaluator stops seeing them; set by hand for event-sourced ones, which have
      no way to know that whatever happened has stopped being a problem.

    `source` is what makes that split work. A `condition` alert is level-triggered
    — the evaluator recomputes the whole firing set every tick, so absence is
    evidence and auto-resolution is sound. An `event` alert is edge-triggered off
    a row in `ops_event`, where absence means nothing at all: the event happened,
    and the log will never say it un-happened.
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
        DBUUID, ForeignKey("supply_cluster.id", ondelete="CASCADE"), nullable=True
    )
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """The subject's public id, not its primary key — unlike `Event.subject_id`.

    A deliberate departure. An alert outlives the row it is about: the node that
    triggered it may have been pruned by the next snapshot, which is frequently
    the whole point of the alert, and a foreign key would have deleted the alert
    along with the evidence. A string that no longer resolves is the correct
    outcome here and a dangling one everywhere else.
    """

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
    """The last delivery failure, kept so a silently broken webhook is visible.

    `notified_state` is what was last successfully announced, and the pair is the
    whole delivery ledger: an alert is due for a notification when its current
    `state` is notifiable and differs from `notified_state`. There is no delivery
    table because there is one writer — only the reactor lease holder notifies —
    and a second table would record, per channel, a fact this pair already
    settles. The cost, stated plainly: adding a second channel later means either
    a delivery table or accepting that one channel failing marks the alert
    notified for both.
    """


class AlertThreshold(BaseMixin):
    """An operator-declared threshold on a named metric. Declared state, written through the API.

    What makes an alert rule data rather than a constant compiled into `app/reactor/rules.py`. A
    number that only exists in Python cannot be different for one endpoint than for the fleet,
    cannot be raised at three in the morning without a release, and cannot be shown next to the
    alert it produced.

    `subject_uid` null is the fleet default for that rule, and a row with a uid overrides it for
    that subject. That is exactly why the scope index is `NULLS NOT DISTINCT`: under the default
    rule two NULLs are distinct, so two fleet defaults for one rule would both be admitted and
    which one the evaluator read would be whichever the planner returned that day.

    `subject_uid` carries no foreign key, the same departure `Alert.subject_uid` makes and for the
    same reasons: a declared threshold outlives the endpoint or deployment it was written about,
    and a `gateway` subject has no row in this database to point at at all.

    `lookback` is named `lookback` and not `window` because `WINDOW` is reserved in Postgres.
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

    `config` is per-kind and holds the credential: for `slack`, a webhook URL,
    which is a bearer secret in URL form. It is never returned by the API — the
    schema sends a redacted hint instead — because a console that displays it
    turns every screenshot into a credential leak.

    `min_severity` is the filter, and it is per-channel rather than global so that
    a channel can be added for `critical` only without changing what anything else
    receives.
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

    Named rather than keyed by instance: there is one row, `default`, and holding
    it is what entitles a process to evaluate conditions and notify. Two hubs
    evaluating at once would each open the same alerts — idempotent, by
    `dedupe_key` — but would also each send the notification, which is not.

    The lease is a compare-and-swap on `holder` and `lease_expires_at` rather than
    a Postgres advisory lock, for the same reason `hub_cluster_claim` is a table:
    a lock is invisible to everything that is not currently holding a connection,
    and "which process is reacting, and since when" is a question worth being able
    to answer with a select.

    `last_event_seq` is the cursor into `ops_event.seq`. Events are matched into
    alerts from there forward, so a reactor that has been down comes back and
    works through what it missed instead of skipping to now — the seventeen-hour
    outage this was built for is exactly the case where the backlog is the story.
    """

    __tablename__ = "ops_reactor_state"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    holder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    last_tick_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
