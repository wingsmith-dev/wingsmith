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
    # adopted by watching a cluster serve them. The row is a real artifact and workloads may point
    # at it, but nothing should mistake it for content-addressed until the bytes are resolved.
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    """What a person calls these weights. `GPT-OSS 20B`, `Llama 3.1 70B Instruct`. Display only."""
    slug: Mapped[str] = mapped_column(String(255), nullable=False, index=True, server_default="")
    """What a machine calls them: `openai/gpt-oss-20b`, lowercase, organisation and all.

    Stored rather than derived at read time, because the organisation only exists in the
    location URI. Derived once, on the way in, from the artifact's preferred location:
    `hf://openai/gpt-oss-20b` becomes `openai/gpt-oss-20b`. Lowercase because that is what a
    caller types; the organisation is kept because a bare model name is not an id anybody can
    look up.

    Not unique. Two artifacts may be the same repository at different revisions or
    quantisations; identity is the digest. This is what the model is *called*.
    """
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
    opposite in exactly the way `Workload.imported_args` is `Workload.engine_args`: one is a claim
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


class Workload(BaseMixin):
    """What you asked to run, globally. The object a user creates, scales, and deletes.

    A workload is not tied to a cluster. It names an artifact, declares how to serve it, and
    declares a kind, a priority and TOTAL scale across every cluster it is allowed to use;
    placement expands it into one `Deployment` per cluster, and each deployment carries that
    cluster's share of the total. `kind` discriminates `service | task | gang`; only `service` is
    rendered and run today, and `task` and `gang` are on the roadmap. Nothing below this object
    branches on `kind`; the operator is the only component entitled to read it, when it decides
    what Kubernetes object to render.

    The serving configuration lives here rather than on a shared recipe object. What sharing a
    recipe bought, not deploying the same thing twice under two identities, is `serving_digest`.

    `engine_args` and `imported_args` are the same shape and different in kind. `engine_args` is
    declared: somebody asserted it, and it renders onto the command line. `imported_args` is
    observed: it was read off a running process by the discovery path, it is recorded in full,
    and it is never rendered. The split is provenance, and provenance is what makes taking
    management of an adopted workload reviewable — folded into one map there is no way to tell
    intent from incidental capture, and the first render is a guess about which values somebody
    meant (ADR 0009).

    `overlay` is the one opaque field on an otherwise fully-modelled object. Engines and the
    Kubernetes operators that run them carry far more configuration than a control plane wants to
    mirror field by field, and mirroring it is a losing race against every engine release — so
    the plane models what it reasons about and passes the rest through untouched.

    `device_class` is required, not optional — you cannot tensor-parallel across an A100 and an
    H100, so a shape without a class is not a shape. Context length is here rather than on the
    artifact because it determines the footprint.

    Splittability is a property of `kind`, not a field: `service` may fan out across clusters,
    while placement keeps a `gang` inside one cluster and one topology domain.

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
    artifact_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("demand_artifact.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    serving_digest: Mapped[str] = mapped_column(String(71), nullable=False, index=True)
    """`svg_` + sha256 over the fields that decide what process runs. Derived, never accepted.

    It answers one question — would this produce the same running engine — so it covers only
    fields that reach a cluster. `imported_args` is observed, `capabilities` is asserted,
    `served_model_name` is a name rather than a footprint, and scale and placement are not
    properties of the configuration at all; none of them are in it. `app.core.serving` owns the
    computation and is the only thing entitled to write this.

    It is what a benchmark run points at. A digest cannot drift, where the `recipe_id` and
    `recipe_version` it replaces named a mutable row.
    """
    engine: Mapped[str] = mapped_column(String(64), nullable=False)
    """Which engine inside the runtime serves the weights — `vllm`, `sglang`, `trtllm`.

    It names a module and a flag dialect, and nothing else. The operator launches
    `python3 -m dynamo.<engine>`, and `SERVING_FLAGS` reads it to know that a
    structured-output backend is `--guided-decoding-backend` on vLLM and
    `--grammar-backend` on SGLang. It carries no version: the engine's own build is
    whatever the runtime image was compiled with, which is `engine_runtime_version`'s business.
    """
    engine_runtime_version: Mapped[str] = mapped_column(String(64), nullable=False)
    """A tag of the Dynamo runtime image, and never a version of the engine.

    `1.4.1` is a Dynamo release. The vLLM inside it is on `0.28.0`, and the two count
    in different sequences, which is the whole reason this field is not called
    `engine_version` any more: under that name the obvious value to type was the
    engine's number, and `vllm-runtime:0.28.0` is an address that has never existed.
    A recipe's `min_vllm_version` is therefore evidence and never a value — it
    constrains which runtime is new enough, not what goes here.

    `resolve_engine_runtime_image` pastes it onto `ENGINE_RUNTIME_REPOS[engine]`. That is its
    only use; nothing reads it to decide behaviour.
    """
    engine_runtime_image: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """The whole address, when this workload pins one instead of having it derived.

    Null is the ordinary case and means `engine` plus `engine_runtime_version` resolve it.
    """
    frontend_image: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """The image this model's own Dynamo frontend runs. Null means the cluster decides.

    Separate from `engine_runtime_image` because a frontend and a worker are separately versioned
    binaries that only happen to be published from one repository. The worker loads the weights;
    the frontend tokenizes the request and registers the model, and the tables it needs to do that
    are compiled into it. A model whose tokenizer only a newer frontend build understands needs a
    different frontend and the same worker, and one field could not say that.

    Null is this workload declining to have an opinion, not an instruction to reuse
    `engine_runtime_image`. The operator resolves it against what that cluster runs its frontends on — a
    local fact the plane does not hold and must not guess at — and only falls back to the runtime
    image once that has declined too. So there is no `resolve_` for this field the way there is
    for `engine_runtime_image`: the plane can derive a runtime image because `ENGINE_RUNTIME_REPOS` tells it
    where workers come from, and nothing tells it the same about frontends, so an address invented
    here is an `ImagePullBackOff` chosen by the wrong side of the wire.
    """
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
    """How the served API behaves: thinking mode, the reasoning and tool-call parsers, streaming
    defaults, structured-output support, EOS suppression.

    Every field here renders to a flag or a rendered value, travels inlined on
    `deployment.desired`, and is obeyed by the cluster — which is the whole test for what belongs
    here rather than in `capabilities`. Until this column existed these settings vanished into the
    `engine_args` passthrough, where the plane could neither validate them nor say what it had
    asked for.
    """
    router: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    """How this workload's own Dynamo frontend routes between its workers.

    `mode`, `queue_threshold`, `queue_policy` and `kv_overlap_score_credit`, each rendered as one
    `DYN_ROUTER_*` variable on the frontend. Only declared keys are stored, so `{}` is the one way to
    say nothing was declared, and nothing is what every workload from before the column says. Its own
    column rather than a key inside `serving`, because `serving` is replaced whole by a patch that
    only meant to change a parser, and a router setting that vanished with it would be a benchmark
    configuration silently undone.
    """
    capabilities: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    """What this serving configuration can do, asserted and never rendered.

    The same split as `engine_args` against `imported_args`, one level up: nothing here reaches a
    cluster, because a cluster has no use for a claim about itself. A wrong value changes only
    what we told somebody, and the only thing that can falsify it is a conformance run.
    """
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
    """`aggregated | disaggregated` — whether prefill and decode share an engine loop or are
    separate processes exchanging a KV cache.

    Named `serving_topology` and not `topology`, which is what it wants to be called, because
    `topology_domain_kind` and `requires_single_topology_domain` are already on this object and
    mean something entirely different: an NVLink or rack domain a placement has to fit inside.
    Two unrelated senses of "topology" one line apart is a comment nobody reads and a bug
    somebody writes.

    Defaulted rather than nullable. There is no third state: a workload either is disaggregated
    or is not, and a null would invent a "not yet decided" that nothing can render.

    It is the discriminator every consumer branches on, and it is what makes "does `min_replicas`
    count replicas or groups" a declared question rather than an inferred one. Under
    `disaggregated`, `min_replicas` counts **groups** — one group being the per-role weights in
    `roles` taken together — and the operator multiplies to get each role's process count.

    In `serving_digest`, but only when disaggregated (`app.core.serving.serving_digest`). A
    topology change changes *which* processes exist, which is a configuration change of the
    largest available kind; a weight change only changes how many of a role there are, which is
    what `min_replicas` already does and has never been hashed. Contributed conditionally so
    that every aggregated workload's digest is byte-identical across the migration — the same
    trick `engine_defaults` plays there, for the same reason.
    """
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    deployments: Mapped[list[Deployment]] = relationship("Deployment", back_populates="workload", lazy="noload")
    artifact: Mapped[Artifact] = relationship("Artifact", lazy="noload")
    roles: Mapped[list[WorkloadRole]] = relationship("WorkloadRole", back_populates="workload", lazy="noload")


class WorkloadRole(BaseMixin):
    """One role in a disaggregated workload, and the little that differs about it.

    Two rows for a `disaggregated` workload — `prefill` and `decode` — and none at all for an
    `aggregated` one, which is the ordinary case and stays exactly as it was.

    **The table is narrow on purpose.** Across NVIDIA's `disagg-gb300-agentic` recipe the two
    roles differ on six arguments and agree on everything else: same artifact, same TP8, same
    device class, same context length, same quantization, same `kv-cache-dtype`, and the same
    `kv-transfer-config` string on both sides — the transfer mechanism is a property of the
    topology, not of the role. So a role is one weight, one optional `max_batch_tokens`, and an
    argument map. Giving each role a full copy of the serving block would be paying for a
    difference that does not exist.

    Why a table rather than a JSON block on `Workload`, which is how `serving` and
    `prefix_cache_config` live: placement computes a fit against per-role device counts, and a
    JSON blob that placement reads is a typed column that has not been declared yet. `weight` is
    reasoned about; `engine_args` is not, which is why it stays a map inside the row.
    """

    __tablename__ = "demand_workload_role"
    __table_args__ = (UniqueConstraint("workload_id", "role", name="uq_demand_workload_role_workload_role"),)

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    workload_id: Mapped[UUID] = mapped_column(
        DBUUID, ForeignKey("demand_workload.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    """`prefill | decode`. A closed set, validated on the way in.

    The same argument `check_shape` makes about device classes: a shape without a class is not a
    shape. A role outside the set renders a `disaggregation-mode` no engine accepts, and the
    failure is a worker that will not start rather than a validation error somebody can read.
    """
    weight: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    """How many workers of this role are in one group. Positive, and free.

    Deliberately NOT constrained coprime. That constraint was proposed so each ratio would have
    exactly one spelling and `serving_digest` could not differ for two spellings of one
    footprint — but the weights are not in the digest at all (they are scale, and `DIGEST_FIELDS`
    has always excluded scale), so the argument it rested on is gone. What
    coprimality costs is real: it makes "two prefill and two decode" inexpressible as such,
    forcing it to be written `1:1` with two groups, which reinterprets a live number to say the
    same thing.

    A role's process count is `deployment.min_replicas * weight`. Any pair of per-role counts is
    therefore reachable at one group, and the group only earns its keep when scaling load — bump
    the count and both roles grow together, so the ratio is preserved by construction rather than
    maintained by something.

    **Kept out of `serving_digest`, and the reason is identity rather than restarts.**
    `ops_benchmark_run.serving_digest` names the configuration a measurement was taken against,
    and `DIGEST_FIELDS` is explicit that a run whose digest matches no workload "has lost the
    only link back to what it measured". A weight in the hash would orphan every benchmark on a
    model the moment somebody added one prefill worker — and those benchmarks are exactly what
    anyone choosing a ratio would be reading.

    Whether pods restart is not decided by the digest. Any declared change bumps the
    deployment's generation, so the operator re-applies; what decides a restart is whether the
    applied object changes a component's own spec.
    """
    max_batch_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """This role's `--max-num-batched-tokens`. Null inherits the workload's.

    The one typed column in the per-role delta, and it is typed because the workload already
    types it and `DERIVED_FLAGS` already spells it. Prefill wants 32768 and decode 2048 in the
    published recipe, which is a 16x difference in the thing that decides how much work one
    engine step does — not a knob to pass through untyped.
    """
    engine_args: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    """This role's engine arguments, laid over the workload's.

    Declared, like `Workload.engine_args`, and subject to the same rule: it may not name a
    member of `DERIVED_FLAGS`, nor `disaggregation-mode` or `kv-transfer-config`, both of which
    the operator now derives from the topology. A value for a derived flag arriving from here is
    not a default, it is a contradiction of a decision already acted on.
    """

    workload: Mapped[Workload] = relationship("Workload", back_populates="roles", lazy="noload")


class WorkloadRevision(BaseMixin):
    """One serving configuration a workload has held, kept as the request that would restate it.

    Declared state, append-only, and written by exactly the writes that move
    `Workload.serving_digest` — a create, a serving patch, a reshape, and a promotion. Nothing
    here reaches a cluster and nothing reconciles against it: the workload row is still the only
    declaration, and this is the record of what that row said before.

    `snapshot` is a `WorkloadServingUpdate` as msgspec renders it — the patch shape, not the
    column shape. `serving_request` already reads a stored workload back as "the request that
    would have created it", and `_apply_serving_patch` already applies a patch onto that and
    revalidates the whole thing, so what is stored here is that reading minus the one field a
    patch may not carry: a revision is a body that can be sent straight back to
    `PATCH /v1/workloads/{ref}`. That is what makes a rollback an ordinary update rather than an
    operation of its own — there is no restore path to write, no second description of a legal
    configuration, and a revision that would no longer validate is refused by the same checks
    that refuse a hand-written one.

    `number` counts from 1 within the workload and never rewinds. Going back to revision 2
    appends revision 5 carrying revision 2's snapshot, because the history is what happened
    rather than where the workload can still get to. `BaseMixin.seq` is a global identity
    column and is not this.

    `serving_digest` is copied off the workload after the write landed, so it names the same
    configuration that `ops_benchmark_run.serving_digest` and `demand_replica.serving_digest`
    name. It is deliberately not unique here: a workload edited A to B and back to A has two
    revisions carrying one digest, which is why a running replica resolves to a *configuration*
    and never to a revision number.
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
    """Which weights this configuration served, recorded rather than referenced.

    A plain uid and not a foreign key: a revision is a statement about what happened, and a
    history that could be made un-writable by an artifact somebody deleted would be a history
    that stops recording exactly when the record starts to matter.

    It is here rather than inside `snapshot` because `snapshot` is a patch body and the weights
    are not patchable — `WorkloadServingUpdate` has no `artifact` field, deliberately, since
    repointing a workload at other weights makes every deployment expanded from it mean
    something else. Folding the two together would give a snapshot that no longer decodes as
    the thing it exists to be resent as.
    """
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Why somebody made this change, in their own words.

    The one mutable field on an otherwise append-only row, and the split is the point: what the
    configuration *was* is a fact and cannot be edited, while why it was changed is frequently
    only understood later — a parameter moved on a hunch and explained by the benchmark that
    ran afterwards. Writable through the revision, and settable in the same breath as the change
    by a `note` on the create, the patch or the reshape that files it.

    Free text and never parsed. `reason` is the closed set naming which write this was, and the
    two are not the same question: `reason` says a patch did it, this says the context window
    was doubled because the eval suite was truncating.
    """
    reason: Mapped[str] = mapped_column(String(32), nullable=False, server_default="updated")
    """Which write appended this — `created`, `updated`, `reshaped`, `promoted` or `imported`.

    `imported` is the one that is not a write at all. It is the configuration a workload was
    already running when its history began, recorded from the pre-image the next edit read
    before it changed anything. Without it, a workload older than this table would have its
    first recorded revision be the state *after* the first edit — and the edit somebody wants
    to undo is usually that one.
    """

    workload: Mapped[Workload] = relationship("Workload", lazy="noload")


class Deployment(BaseMixin):
    """One workload's share on one cluster. The unit the agent obeys.

    A deployment is workload x cluster. What changed is what sits above
    it: a `Workload` is now its parent rather than its supertype, and one workload has many
    deployments — one per cluster it was expanded onto. `min_replicas` / `max_replicas` /
    `desired_replicas` are THIS CLUSTER'S SHARE of the workload total, never the total. Nothing
    anywhere may sum a share against a total or re-derive one from the other.

    Serving configuration, adapters, overrides, labels and the topology pair live on the workload: they are
    facts about what was asked for, and a cluster does not get its own answer to them. Admission
    is per-cluster and lives here, because a workload can be running in one region and queued on
    quota in another and there is no single phase that is true of both.

    A configuration change, an engine upgrade or a new share updates the row in place and bumps
    `generation`, which is what the operator re-reads. A drain, or a re-expansion that leaves this
    cluster with no share, deletes the row. Deployments are therefore not stable handles, which is
    why the public name lives on `Endpoint` instead. You do not create one: placement creates it
    when it expands a workload.

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
    roles: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    """The same counts split by role, as the cluster reported them in one frame.

    Stored rather than derived, and the derivation is what argues for the column.
    `ready` per role could be counted over `demand_replica.role`, but `desired`
    per role could not: it is the count this cluster rendered, which lives only in
    the frame that reported it — a rollup over replica rows can say how many
    prefill workers exist and never how many were asked for, which is precisely
    the half that makes a shortfall legible. Recomputing one number and storing
    the other would also split one internally consistent observation across two
    sources taken at different instants, which is the reason the scalars above are
    a column rather than a rollup in the first place.

    JSONB and not a table, because nothing joins it, filters on it or aggregates
    it: it is read whole, with its deployment, and displayed. Empty on an
    aggregated deployment, which has one role and for which the scalars are
    already the whole answer.
    """
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
    workload's during a rollout — and the node it landed on. The devices it holds
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
    role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    """`prefill` or `decode`, and null for a replica of an aggregated deployment.

    Nullable rather than defaulted to `decode`, because "this deployment has one
    worker" and "this worker is the decode half of a pair" are different facts and
    a default would erase the difference on every row that predates the column.

    The cluster's own answer, read off the pod label the operator stamped, and the
    workload's declared roles are not it: the declaration says what should exist,
    this says what a running pod turned out to be, and a rollout is exactly the
    interval in which the two disagree.
    """
    engine_runtime_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """The runtime tag this replica reports, which is the declared one echoed back.

    The operator copies it off the deployment rather than reading it out of the
    running process, so it corroborates nothing on its own. It is here because a
    replica that outlives an edit to its workload still says which runtime it started
    under, and that is the one question the workload's own column cannot answer.
    """
    serving_digest: Mapped[str | None] = mapped_column(String(71), nullable=True, index=True)
    """The configuration this replica actually came up on, read off the annotation it was
    rendered with.

    The observed half of versioning, and the only one there is: `Workload.serving_digest` says
    what the plane declared, and this says what a running process was started from. A rollout is
    exactly the interval in which the two disagree, and so is a cluster whose agent has not
    reconnected since the last edit.

    Deliberately not a field on the workload. "Which version is live" has no scalar answer —
    several are, during every roll and across every cluster — so it is counted over these rows
    rather than stored as declared state with a second writer.

    Nullable because a replica reported before this column existed has no answer, and because
    an operator too old to stamp the annotation sends an empty string. Null is "not known",
    never "the declared one".
    """
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
    disposable. Keeping them separate is what allows weighted splits, canaries and
    renaming without breaking clients; merging them would give one globally unique
    record N cluster-side writers. `routing_strategy` and `failover_order` are stored
    and returned, but automatic cross-cluster failover is on the roadmap, not built.

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

    Not to be confused with the name the engine answers to. That is `Artifact.slug`, and the
    gateway rewrites between the two — see `app.core.serving.served_model`. They are equal
    whenever nobody has asked for an alias, which makes the rewrite a no-op in the ordinary case
    and a rename in the deliberate one.
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
    """The model id clients send. `openai/gpt-oss-20b`.

    Patchable where `name` is not, because `model` in an OpenAI request is a public contract: it
    wants to read as the model, not as whatever the endpoint was called when it was made, and it
    has to change without breaking the handle everything else holds. It is what the resolved table
    reports, so changing it costs one gateway poll — no engine flag, no pod respec, nothing on any
    cluster.

    Defaulted from the artifact's slug when a caller declares none, which is the case that should
    be normal: an endpoint in front of one model answers to that model's name, and saying so twice
    is how the two drift. Declaring one is then an alias and reads as a deliberate act.
    """
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, server_default="private")
    routing_strategy: Mapped[str] = mapped_column(String(32), nullable=False, server_default="weighted")
    failover_order: Mapped[list[UUID]] = mapped_column(ARRAY(DBUUID), nullable=False, server_default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    surface: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    """The terms on which the served API is exposed: which protocols answer at this name
    (`/v1/chat/completions`, `/v1/responses`, `/v1/messages`), how a caller authenticates, and the
    rate-limit policy the gateway reads.

    It sits here rather than on the workload because honouring any of it needs no engine flag and an
    endpoint never syncs to a cluster. Some of it the gateway acts on and some of it is a
    disclosure the gateway only reports; the schema layer says which, per field.
    """
    labels: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    targets: Mapped[list[EndpointTarget]] = relationship(
        "EndpointTarget", back_populates="endpoint", lazy="noload", cascade="all, delete-orphan"
    )
    aliases: Mapped[list[EndpointAlias]] = relationship(
        "EndpointAlias", back_populates="endpoint", lazy="noload", cascade="all, delete-orphan"
    )


class EndpointAlias(BaseMixin):
    """An additional public model id that resolves to one endpoint.

    The third of three names, and the only one a caller chooses between. `slug` is the canonical
    one and every internal record — usage, policy, metrics, logs — is keyed on it;
    `EndpointTarget.served_model_name` is what one engine answers to. An alias is neither: it is a
    string a client may type, and the gateway turns it into the canonical before any other stage
    runs. That is what makes a rename cheap — the old id becomes an alias and nothing downstream
    is renamed.

    It hangs off the endpoint rather than off the workload or the gateway because the public name
    a caller types is what an endpoint *is*. The gateway resolves and never defines: it holds no
    catalog of its own, and an alias reaches it as one more key into the same route.

    `alias` is globally unique, which makes aliases and canonical slugs one namespace. The index
    only stops two endpoints claiming one alias; the rest of that namespace — an alias equal to
    some endpoint's slug, or a slug moved onto somebody's alias — is the service's to refuse,
    because the two live in different tables and no constraint spans them.

    Stored lowercased, and resolved case-insensitively, so `GPT-OSS-20B` and `gpt-oss-20b` are not two
    routes somebody has to keep in step.
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

    The many-to-many between global names and per-cluster deployments. It exists
    as a table rather than as gateway configuration so that a canary or a
    weighted split is versioned and auditable like anything else.

    `deployment_id` cascades: when a drain or a re-expansion deletes a deployment,
    its targets go with it, and nothing retargets the endpoint at the deployment
    that replaced it. Re-point the endpoint after moving a workload.
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
    workload carrying an `engine_args` override the plane derived around, an adopted workload
    already serving under a name nobody here chose — the fix is a write here.

    Per target rather than per endpoint because one endpoint fronts many deployments and they
    need not agree; per target rather than on the deployment because the deployment is disposable
    and this correction should outlive a configuration bump. The gateway rewrites the inbound `model` to
    it, so changing it costs a routing-table poll and nothing restarts.
    """

    endpoint: Mapped[Endpoint] = relationship("Endpoint", back_populates="targets", lazy="noload")
    deployment: Mapped[Deployment] = relationship("Deployment", lazy="noload")


class WarmSet(BaseMixin):
    """Declared weight-cache intent: which artifacts should be resident on a cluster.

    The declared half of the weight cache. Its observed counterpart is
    `CacheEntry`, in the machinery group. The KV and prefix caches are unrelated
    to this and are configuration on `Workload` — collapsing the two would produce
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
    """The billing and isolation root. Projects hang off it; quota attaches below it.

    `ingress` and `auth` are how the gateway decides who a caller is and whether
    to believe them (docs/decisions/0016). `ingress` says which listener the tenant's
    traffic arrives on; `auth` says whether the gateway checks a key on it.
    `host` is the name that resolves to this tenant, checked against the
    request's `Host` header — null until a tenant is given one.
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
    auth: Mapped[str] = mapped_column(String(16), nullable=False, server_default="none")
    host: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)

    projects: Mapped[list[Project]] = relationship("Project", back_populates="tenant", lazy="noload")
    api_keys: Mapped[list[TenantAPIKey]] = relationship("TenantAPIKey", back_populates="tenant", lazy="noload")


class TenantToken(BaseMixin):
    """A console credential that signs in as one tenant, and only as that tenant.

    This is not an inference key. The gateway authenticates the system in front of
    it and says so in `serving.Guard`; a credential that named a tenant there would
    turn that hop into a credential store. This one buys a read-only view of what a
    tenant is being served, and the plane is the only thing that ever resolves it.

    Only the sha256 of the plaintext is stored. The token is high-entropy random, so
    a fast hash is safe against precomputation and lets a login resolve it with one
    indexed lookup — the same reasoning as the enrolment token, and the opposite of
    the admin password, which is human-chosen and therefore bcrypt.

    Many rows per tenant rather than one, for rotation: issue the next, move the
    reader, revoke the last. `revoked_at` rather than a delete, so a credential that
    was used has a record that it existed.
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
    somebody depends on before it is revoked. Written at most once a minute — see
    `AuthService.resolve_tenant_token`; a login is not a metric."""
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped[Tenant] = relationship("Tenant", lazy="noload")


class TenantAPIKey(BaseMixin):
    """An inference credential, checked by the gateway on a tenant's public listener.

    This is the credential `TenantToken` says it must not become: `TenantToken`
    signs in to the console, this one calls the gateway. Keeping them as separate
    tables means revoking a console session can never touch inference traffic and
    the other way round.

    Only the sha256 of the plaintext is stored, for the same reason as
    `TenantToken.token_hash`. One tenant, many keys — one per caller, so revoking
    one caller does not revoke another.
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

    Per project, per device class, optionally scoped to one cluster (`cluster_id`
    null means fleet-wide). Two numbers rather than one cap, because a hard limit
    leaves capacity idle while someone queues. Reclaiming over-quota capacity by
    preemption is on the roadmap; today the floor and the share are admission inputs.
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
    second fleet-wide quota for the same project and class a conflict rather than a second row."""

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
    rather than avoided. `per_replica` is the default because over-admission shows up in the usage
    panel, while a spurious 429 refuses a caller who was inside the committed limit.
    """
    success_rate_objective: Mapped[float | None] = mapped_column(Numeric(6, 5), nullable=True)
    availability_objective: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="availability-2026.1"
    )
    objective_window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="600")
    enforced: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    endpoint: Mapped[Endpoint] = relationship("Endpoint", lazy="noload")


class PriorityClass(BaseMixin):
    """What would preempt what. Referenced by every schedulable workload.

    Preemption itself is on the roadmap: placement can name the victims it would evict
    (`PreemptionPlan`), but nothing evicts them yet. The reference exists now so that
    turning it on is a policy change rather than a migration across every workload. `value` is unique in the database and not merely checked in
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

    The cost is that the database cannot enforce the reference: `subject_id` has no
    foreign key, and deleting the tenant, project, endpoint or cluster it names
    leaves the attachment behind. Readers join on `(subject_type, subject_id)` and
    an attachment whose subject is gone matches nothing, so it is inert rather than
    wrong; it stays until the policy is deleted or the attachment is removed.
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
    this cluster. A preemption plan reasons about deployments — evicting "a workload" from
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
    `target_id` are polymorphic because the set of targets spans four groups, so
    `target_id` has no foreign key: an operation is a record of what was done and
    outlives its target, and a `target_id` that no longer resolves is expected.

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
    without `deployment_generation`, `serving_digest` and `engine_args` is a
    number with nothing to attribute it to; with them the table is the
    parameter-tuning record. `serving_digest` is what a run is about, and it
    replaced a `recipe_id` and `recipe_version` pair that named a mutable row:
    a digest is the configuration rather than a handle on it, so a run cannot
    come to describe a configuration nobody measured. `engine_args` is still
    snapshotted beside it, because the deployment is disposable and a run must
    describe what it measured after that has gone.

    `operation_id` is nullable and `ON DELETE SET NULL`: the operation is how the
    run was carried out and the run is the record of it, so the record outlives
    the errand.
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

    State, not telemetry — discrete, low-volume, and worth joining against. Metric
    streams do not come through here and never enter Postgres.

    `subject_type` / `subject_id` is polymorphic and has no foreign key, on purpose:
    an event is history and must outlive the node, device or deployment it is about.
    Nothing deletes events today, so the table grows until an operator prunes it by
    `occurred_at`; the reactor only reads forward from its `last_event_seq` cursor.
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

    `scope_type` / `scope_id` is polymorphic and has no foreign key; `scope_id` is
    null for a fleet-scoped credential. Deleting the scoped object leaves the
    credential row, which then applies to nothing until it is revoked or deleted.
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
    tenant_token_id: Mapped[UUID | None] = mapped_column(
        DBUUID, ForeignKey("bind_tenant_token.id", ondelete="CASCADE"), nullable=True, index=True
    )
    """The tenant credential this session was opened with, or null for an admin.

    A foreign key rather than the tenant's uid, because the question a refresh has
    to answer is not "does this tenant still exist" but "may this session still be
    renewed" — and that is revoked one credential at a time. Revoking the token ends
    every session it opened at the next refresh, and deleting the tenant ends them
    outright."""
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
    for hours is one row with a rising `occurrences`, not a row per
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
    works through what it missed instead of skipping to now.
    """

    __tablename__ = "ops_reactor_state"

    id: Mapped[UUID] = mapped_column(DBUUID, primary_key=True, server_default=dUUID)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    holder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    last_tick_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
