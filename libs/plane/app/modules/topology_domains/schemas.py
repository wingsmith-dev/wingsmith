import datetime as dt

from app.core.schemas import Struct


class TopologyDomainSchema(Struct):
    """A set of devices that share an interconnect.

    The standalone form, and the authority on topology. A device carries an
    embedded summary of its domain for display; that summary has no parent and
    no counts, so nesting and membership are answered here and only here.

    `parent` is the id of the enclosing domain, which is how a NUMA domain
    contains PCIe roots which contain NVLink islands. All observed — a domain is
    discovered by the operator, never declared.
    """

    id: str
    cluster: str
    cluster_name: str
    node: str | None
    node_name: str | None
    parent: str | None
    kind: str
    identifier: str
    link_generation: str | None
    link_bandwidth_gbps: int | None
    device_count: int
    spans_nodes: bool
    attributes: dict
    observed_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime | None
