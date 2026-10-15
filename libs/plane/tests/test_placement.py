import sys
import subprocess

from types import SimpleNamespace

import pytest

from msgspec import structs

from app.modules.placement import DeviceHold
from app.modules.placement import ReservedUnit
from app.modules.placement import QuotaSnapshot
from app.modules.placement import DeviceSnapshot
from app.modules.placement import PolicySnapshot
from app.modules.placement import WorkloadDemand
from app.modules.placement import ClusterSnapshot
from app.modules.placement import CapacitySnapshot
from app.modules.placement import PlacementRequest
from app.modules.placement import TopologyDomainSnapshot
from app.modules.placement import place
from app.modules.placement import expand
from app.modules.placement import decision as codes
from app.modules.placement.services import PlacementService


def devices(cluster, device_class="h100", count=8, node="n0", domain=None, prefix="d", **kw):
    return [
        DeviceSnapshot(uid=f"{cluster}-{prefix}{i}", node=node, device_class=device_class, domain=domain, **kw)
        for i in range(count)
    ]


def nvlink(uid, identifier="nvl0", kind="nvlink", parent=None, spans_nodes=False):
    return TopologyDomainSnapshot(uid=uid, kind=kind, identifier=identifier, parent=parent, spans_nodes=spans_nodes)


def cluster(uid, name, devs=(), doms=(), **kw):
    return ClusterSnapshot(
        uid=uid, name=name, region=kw.pop("region", "us-east"), devices=tuple(devs), domains=tuple(doms), **kw
    )


def hold(workload, priority_value=0, preemptible=False):
    return DeviceHold(workload=workload, project="proj_a", priority_value=priority_value, preemptible=preemptible)


def request(count=8, **kw):
    return PlacementRequest(
        workload=kw.pop("workload", "wl_1"),
        project=kw.pop("project", "proj_a"),
        device_class=kw.pop("device_class", "h100"),
        device_count=count,
        **kw,
    )


def verdict_for(decision, name):
    return next(v for v in decision.verdicts if v.cluster_name == name)


def codes_for(decision, name):
    return [r.code for r in verdict_for(decision, name).reasons]


def test_placement_imports_no_database_or_api():
    """Importing the pure half loads neither the models nor the API, checked in a clean interpreter."""
    probe = (
        "import sys\n"
        "import app.modules.placement.engine, app.modules.placement.snapshot, app.modules.placement.decision\n"
        "print(sorted(m for m in sys.modules if m == 'app.db' or m.startswith(('app.db.', 'app.api'))))"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_capacity_is_counted_from_free_devices_not_node_counts():
    devs = devices("c1", count=8)
    devs[0] = DeviceSnapshot(uid="sick", node="n0", device_class="h100", healthy=False)
    devs[1] = DeviceSnapshot(
        uid="busy",
        node="n0",
        device_class="h100",
        held_by=DeviceHold(workload="other", project="p", priority_value=100, preemptible=False),
    )
    snap = CapacitySnapshot(clusters=(cluster("c1", "iad-1", devs),))

    assert place(request(6), snap).placed is True
    unplaced = place(request(7), snap)
    assert unplaced.placed is False
    assert codes_for(unplaced, "iad-1") == [codes.INSUFFICIENT_CAPACITY]
    assert verdict_for(unplaced, "iad-1").free_devices == 6


def test_a_cluster_of_small_machines_cannot_hold_a_tensor_group():
    """Free devices are not the answer when the machines are the wrong shape.

    Four one-device nodes have four free devices and can serve nothing that needs
    two of them in one node. Reported as capacity it reads as a queue to wait in,
    and preemption is offered as the remedy for hardware.
    """
    devs = [DeviceSnapshot(uid=f"c1-d{i}", node=f"n{i}", device_class="h100") for i in range(4)]
    snap = CapacitySnapshot(clusters=(cluster("c1", "iad-1", devs),))

    decision = place(request(2, tensor_parallel=2), snap)

    assert decision.placed is False
    assert codes_for(decision, "iad-1") == [codes.TENSOR_GROUP_TOO_LARGE]


def test_the_same_devices_serve_a_replica_that_does_not_need_them_in_one_node():
    """The other half of it: pipeline stages cross a network perfectly well."""
    devs = [DeviceSnapshot(uid=f"c1-d{i}", node=f"n{i}", device_class="h100") for i in range(4)]
    snap = CapacitySnapshot(clusters=(cluster("c1", "iad-1", devs),))

    assert place(request(2, tensor_parallel=1), snap).placed is True


def test_gang_fits_only_in_the_one_domain_large_enough():
    doms = (nvlink("dom-a", "nvl-a"), nvlink("dom-b", "nvl-b"))
    devs = devices("c1", count=4, domain="dom-a", prefix="a") + devices("c1", count=8, domain="dom-b", prefix="b")
    snap = CapacitySnapshot(clusters=(cluster("c1", "iad-1", devs, doms),))

    decision = place(request(8, kind="gang", requires_single_domain=True, domain_kind="nvlink"), snap)

    assert decision.placed is True
    assert verdict_for(decision, "iad-1").domain == "dom-b"


def test_gang_that_fits_in_aggregate_but_not_contiguously_is_rejected_with_the_numbers():
    doms = (nvlink("dom-a", "nvl-a"), nvlink("dom-b", "nvl-b"))
    devs = devices("c1", count=6, domain="dom-a", prefix="a") + devices("c1", count=6, domain="dom-b", prefix="b")
    snap = CapacitySnapshot(clusters=(cluster("c1", "iad-1", devs, doms),))

    decision = place(request(8, kind="gang", requires_single_domain=True, domain_kind="nvlink"), snap)

    assert decision.placed is False
    verdict = verdict_for(decision, "iad-1")
    assert verdict.free_devices == 12
    assert verdict.largest_domain_free_devices == 6
    reason = verdict.reasons[0]
    assert reason.code == codes.INSUFFICIENT_TOPOLOGY
    assert reason.detail == {
        "largest_domain_free_devices": 6,
        "free_devices": 12,
        "reserved_devices": 0,
        "available_devices": 12,
        "requested_devices": 8,
        "domain_kind": "nvlink",
    }
    assert "6 available h100 in its largest nvlink domain, 8 requested contiguously" in reason.message


def test_a_domain_spanning_nodes_still_satisfies_a_gang():
    doms = (nvlink("dom-a", "nvl-a", spans_nodes=True),)
    devs = devices("c1", count=4, node="n0", domain="dom-a", prefix="a") + devices(
        "c1", count=4, node="n1", domain="dom-a", prefix="b"
    )
    snap = CapacitySnapshot(clusters=(cluster("c1", "iad-1", devs, doms),))

    decision = place(request(8, kind="gang", requires_single_domain=True, domain_kind="nvlink"), snap)

    assert decision.placed is True
    assert verdict_for(decision, "iad-1").domain == "dom-a"


def test_nested_domains_resolve_to_the_requested_kind():
    doms = (nvlink("numa-0", "numa0", kind="numa"), nvlink("nvl-0", "nvl0", parent="numa-0"))
    devs = devices("c1", count=8, domain="nvl-0")
    snap = CapacitySnapshot(clusters=(cluster("c1", "iad-1", devs, doms),))

    numa = place(request(8, requires_single_domain=True, domain_kind="numa"), snap)
    pcie = place(request(8, requires_single_domain=True, domain_kind="pcie"), snap)

    assert numa.placed is True and verdict_for(numa, "iad-1").domain == "numa-0"
    assert pcie.placed is False


def test_quota_exhaustion_blocks_a_project_with_free_devices_in_front_of_it():
    snap = CapacitySnapshot(
        clusters=(cluster("c1", "iad-1", devices("c1", count=16)),),
        quotas=(QuotaSnapshot(project="proj_a", device_class="h100", limit_devices=10, allocated_devices=8),),
    )

    decision = place(request(4), snap)

    assert decision.placed is False
    verdict = verdict_for(decision, "iad-1")
    assert verdict.quota_headroom == 2
    assert verdict.reasons[0].code == codes.QUOTA_EXHAUSTED
    assert place(request(2), snap).placed is True


def test_over_quota_disabled_caps_at_the_guaranteed_floor():
    snap = CapacitySnapshot(
        clusters=(cluster("c1", "iad-1", devices("c1", count=16)),),
        quotas=(
            QuotaSnapshot(
                project="proj_a",
                device_class="h100",
                guaranteed_devices=4,
                limit_devices=16,
                allocated_devices=0,
                over_quota_enabled=False,
            ),
        ),
    )

    assert place(request(4), snap).placed is True
    assert place(request(5), snap).placed is False


def test_cluster_scoped_quota_wins_over_the_global_one():
    snap = CapacitySnapshot(
        clusters=(cluster("c1", "iad-1", devices("c1", count=16)),),
        quotas=(
            QuotaSnapshot(project="proj_a", device_class="h100", limit_devices=64),
            QuotaSnapshot(project="proj_a", cluster="c1", device_class="h100", limit_devices=2),
        ),
    )

    assert verdict_for(place(request(1), snap), "iad-1").quota_headroom == 2


def test_a_preemptible_lower_priority_hold_is_not_counted_as_capacity():
    """Nothing evicts, so a device another workload holds is never on offer, whatever its priority."""
    low = DeviceHold(workload="train_1", project="proj_b", priority_value=10, preemptible=True)
    devs = devices("c1", count=4) + [
        DeviceSnapshot(uid=f"held{i}", node="n1", device_class="h100", held_by=low) for i in range(4)
    ]
    snap = CapacitySnapshot(clusters=(cluster("c1", "iad-1", devs),))

    decision = place(request(8, priority_value=100, preempts=True), snap)

    assert decision.placed is False
    assert decision.preemption is None
    verdict = verdict_for(decision, "iad-1")
    assert verdict.preemption is None
    assert codes_for(decision, "iad-1") == [codes.INSUFFICIENT_CAPACITY]
    assert verdict.reasons[0].detail["available_devices"] == 4


def test_a_single_domain_unit_is_not_placed_on_reclaimable_devices_in_its_domain():
    low = DeviceHold(workload="train_1", project="proj_b", priority_value=10, preemptible=True)
    doms = (nvlink("dom-a", "nvl-a"),)
    devs = devices("c1", count=2, domain="dom-a") + [
        DeviceSnapshot(uid=f"held{i}", node="n1", device_class="h100", domain="dom-a", held_by=low) for i in range(6)
    ]
    snap = CapacitySnapshot(clusters=(cluster("c1", "iad-1", devs, doms),))

    decision = place(
        request(8, requires_single_domain=True, domain_kind="nvlink", priority_value=100, preempts=True),
        snap,
    )

    assert decision.placed is False
    assert codes_for(decision, "iad-1") == [codes.INSUFFICIENT_CAPACITY]


def test_expansion_does_not_count_reclaimable_devices():
    low = DeviceHold(workload="train_1", project="proj_b", priority_value=10, preemptible=True)
    devs = devices("a", count=2) + [
        DeviceSnapshot(uid=f"held{i}", node="n0", device_class="h100", held_by=low) for i in range(6)
    ]
    decision = expand(
        demand(desired_scale=4, priority_value=100, preempts=True), supply(cluster("clu_a", "iad-1", devs))
    )
    assert shares_of(decision) == {"iad-1": 1}
    assert decision.shortfall == 3


def test_a_gang_never_places_partially():
    doms = (nvlink("dom-a", "nvl-a"),)
    snap = CapacitySnapshot(clusters=(cluster("c1", "iad-1", devices("c1", count=7, domain="dom-a"), doms),))

    decision = place(request(8, kind="gang", requires_single_domain=True, domain_kind="nvlink"), snap)

    assert decision.placed is False
    assert decision.cluster is None
    assert verdict_for(decision, "iad-1").domain is None


def test_draining_but_reachable_cluster_is_not_a_target():
    snap = CapacitySnapshot(clusters=(cluster("c1", "iad-1", devices("c1", count=8), phase="draining"),))

    decision = place(request(8), snap)

    assert decision.placed is False
    assert codes_for(decision, "iad-1") == [codes.CLUSTER_PHASE]
    assert verdict_for(decision, "iad-1").free_devices == 8


def test_active_but_unreachable_cluster_is_not_a_target():
    snap = CapacitySnapshot(clusters=(cluster("c1", "iad-1", devices("c1", count=8), reachable=False),))

    decision = place(request(8), snap)

    assert decision.placed is False
    assert codes_for(decision, "iad-1") == [codes.CLUSTER_UNREACHABLE]


@pytest.mark.parametrize(
    "policy,expected",
    [
        (PolicySnapshot(name="eu-only", allowed_regions=("eu-west",)), codes.POLICY_REGION),
        (PolicySnapshot(name="gdpr", allowed_residencies=("eu",)), codes.POLICY_RESIDENCY),
        (PolicySnapshot(name="a100-only", allowed_device_classes=("a100",)), codes.POLICY_DEVICE_CLASS),
        (PolicySnapshot(name="pinned", allowed_clusters=("c2",)), codes.POLICY_CLUSTER_NOT_ALLOWED),
        (PolicySnapshot(name="blocked", denied_clusters=("c1",)), codes.POLICY_CLUSTER_DENIED),
    ],
)
def test_policy_excludes_a_cluster_and_says_which_policy_did_it(policy, expected):
    snap = CapacitySnapshot(
        clusters=(cluster("c1", "iad-1", devices("c1", count=8), residency="us"),), policies=(policy,)
    )

    decision = place(request(8), snap)

    assert decision.placed is False
    assert codes_for(decision, "iad-1") == [expected]
    assert decision.verdicts[0].reasons[0].detail["policy"] == policy.name


def test_a_disabled_policy_constrains_nothing():
    snap = CapacitySnapshot(
        clusters=(cluster("c1", "iad-1", devices("c1", count=8)),),
        policies=(PolicySnapshot(name="eu-only", enabled=False, allowed_regions=("eu-west",)),),
    )

    assert place(request(8), snap).placed is True


def test_ties_between_identical_clusters_break_deterministically_on_name():
    snap = CapacitySnapshot(
        clusters=(
            cluster("c2", "zzz-1", devices("c2", count=8)),
            cluster("c1", "aaa-1", devices("c1", count=8)),
        )
    )

    first = place(request(4), snap)
    reversed_snap = CapacitySnapshot(clusters=tuple(reversed(snap.clusters)))

    assert first.cluster_name == "aaa-1"
    assert place(request(4), reversed_snap).cluster_name == "aaa-1"
    assert [v.cluster_name for v in first.verdicts] == ["aaa-1", "zzz-1"]


def test_workloads_alternate_between_identical_clusters_instead_of_packing_one():
    """Four one-device workloads over two identical four-device clusters.

    Ranking on cluster-wide slack sorted by free devices ascending, which sent
    every one of them to the same cluster until it was full.
    """
    placements = []
    held: dict[str, str] = {}
    for n in range(4):
        clusters = []
        for uid, name in (("c1", "aaa-1"), ("c2", "zzz-1")):
            devs = [
                structs.replace(d, held_by=hold(held[d.uid])) if d.uid in held else d for d in devices(uid, count=4)
            ]
            clusters.append(cluster(uid, name, devs))
        snap = CapacitySnapshot(clusters=tuple(clusters))

        decision = place(request(1, workload=f"wl_{n}"), snap)
        placements.append(decision.cluster_name)

        chosen = next(c for c in clusters if c.uid == decision.cluster)
        held[next(d for d in chosen.devices if d.free).uid] = f"wl_{n}"

    assert placements == ["aaa-1", "zzz-1", "aaa-1", "zzz-1"]


def test_a_gang_still_takes_the_tightest_domain_when_a_roomier_cluster_exists():
    """The domain half of the tiebreak, which the alternation must not undo."""
    doms = (nvlink("dom-a", "nvl-a"), nvlink("dom-b", "nvl-b"))
    tight = devices("c1", count=4, domain="dom-a", prefix="a") + devices("c1", count=4, domain="dom-b", prefix="b")
    roomy = devices("c2", count=16, domain="dom-c", prefix="c")
    snap = CapacitySnapshot(
        clusters=(
            cluster("c1", "aaa-1", tight, doms),
            cluster("c2", "zzz-1", roomy, (nvlink("dom-c", "nvl-c"),)),
        )
    )

    decision = place(request(4, requires_single_domain=True, domain_kind="nvlink"), snap)

    assert decision.cluster_name == "aaa-1"
    assert verdict_for(decision, "aaa-1").fit_slack == 0


def test_the_tightest_domain_that_fits_wins_so_big_fabrics_stay_whole():
    doms = (nvlink("dom-a", "nvl-a"), nvlink("dom-b", "nvl-b"))
    devs = devices("c1", count=4, domain="dom-a", prefix="a") + devices("c1", count=16, domain="dom-b", prefix="b")
    snap = CapacitySnapshot(clusters=(cluster("c1", "iad-1", devs, doms),))

    decision = place(request(4, requires_single_domain=True, domain_kind="nvlink"), snap)

    assert verdict_for(decision, "iad-1").domain == "dom-a"
    assert verdict_for(decision, "iad-1").fit_slack == 0


def test_a_missing_device_class_is_its_own_answer():
    snap = CapacitySnapshot(clusters=(cluster("c1", "iad-1", devices("c1", device_class="a100", count=8)),))

    decision = place(request(8), snap)

    assert codes.NO_SUCH_DEVICE_CLASS in codes_for(decision, "iad-1")


def test_device_memory_floor_filters_candidates():
    devs = [DeviceSnapshot(uid=f"d{i}", node="n0", device_class="h100", memory_mib=40_000) for i in range(8)]
    snap = CapacitySnapshot(clusters=(cluster("c1", "iad-1", devs),))

    assert place(request(8, device_memory_mib_min=80_000), snap).placed is False
    assert place(request(8, device_memory_mib_min=40_000), snap).placed is True


def test_an_empty_snapshot_is_an_answer_not_an_exception():
    decision = place(request(8), CapacitySnapshot())

    assert decision.placed is False
    assert decision.verdicts == ()
    assert "no clusters" in decision.summary


def test_every_rejected_cluster_reports_its_own_reasoning():
    snap = CapacitySnapshot(
        clusters=(
            cluster("c1", "aaa-1", devices("c1", count=2)),
            cluster("c2", "zzz-1", devices("c2", count=8), phase="disabled"),
        )
    )

    decision = place(request(8), snap)

    assert decision.placed is False
    assert codes_for(decision, "aaa-1") == [codes.INSUFFICIENT_CAPACITY]
    assert codes_for(decision, "zzz-1") == [codes.CLUSTER_PHASE]
    assert "aaa-1:" in decision.summary and "zzz-1:" in decision.summary


def a_workload(**overrides):
    fields = {
        "device_class": "h100",
        "device_count": 4,
        "tensor_parallel": 4,
        "device_memory_mib_min": 81920,
        "requires_single_topology_domain": True,
        "topology_domain_kind": "nvlink",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def a_priority(value: int = 100, preempts: bool = False):
    return SimpleNamespace(value=value, preempts=preempts)


def test_replicas_multiply_the_device_count_but_not_the_domain_requirement():
    """A deployment is placed whole; a replica's collective is not the deployment.

    Three replicas of a four-device workload need twelve devices in the cluster, but
    only each four have to share an NVLink domain — the replicas do not talk to
    each other over it.
    """
    request = PlacementService(db=None).request(
        workload_row=a_workload(),
        project_uid="prj_1",
        priority=a_priority(),
        workload="serve",
        replicas=3,
    )
    assert request.device_count == 12
    assert request.requires_single_domain is True
    assert request.domain_kind == "nvlink"


def test_a_request_carries_the_priority_it_will_preempt_at():
    request = PlacementService(db=None).request(
        workload_row=a_workload(),
        project_uid="prj_1",
        priority=a_priority(value=900, preempts=True),
        workload="serve",
    )
    assert (request.priority_value, request.preempts) == (900, True)


def test_a_null_deployment_requirement_falls_through_to_the_workload():
    request = PlacementService(db=None).request(
        workload_row=a_workload(),
        project_uid="prj_1",
        priority=a_priority(),
        workload="serve",
        requires_single_domain=None,
        domain_kind=None,
    )
    assert request.requires_single_domain is True
    assert request.domain_kind == "nvlink"


def test_a_deployment_can_declare_a_shape_the_workload_does_not_have():
    request = PlacementService(db=None).request(
        workload_row=a_workload(requires_single_topology_domain=False, topology_domain_kind=None),
        project_uid="prj_1",
        priority=a_priority(),
        workload="gang",
        kind="gang",
        requires_single_domain=True,
        domain_kind="nvswitch",
    )
    assert request.requires_single_domain is True
    assert request.domain_kind == "nvswitch"


def test_a_deployment_can_relax_a_requirement_the_workload_asserts():
    request = PlacementService(db=None).request(
        workload_row=a_workload(),
        project_uid="prj_1",
        priority=a_priority(),
        workload="serve",
        requires_single_domain=False,
    )
    assert request.requires_single_domain is False
    assert request.domain_kind == "nvlink"


def demand(**kw):
    return WorkloadDemand(
        workload=kw.pop("workload", "wkl_1"),
        project=kw.pop("project", "proj_a"),
        name=kw.pop("name", "serve"),
        device_class=kw.pop("device_class", "h100"),
        unit_device_count=kw.pop("unit_device_count", 2),
        **kw,
    )


def supply(*clusters):
    return CapacitySnapshot(clusters=tuple(clusters))


def shares_of(decision):
    return {s.cluster_name: s.replicas for s in decision.shares}


def test_pack_keeps_a_workload_in_one_cluster_when_one_cluster_holds_it():
    snapshot = supply(
        cluster("clu_a", "iad-1", devices("a", count=8)),
        cluster("clu_b", "sfo-1", devices("b", count=8)),
    )
    decision = expand(demand(desired_scale=3), snapshot)
    assert len(decision.shares) == 1
    assert decision.placed == 3
    assert decision.shortfall == 0


def test_pack_spills_into_a_second_cluster_only_once_the_first_is_full():
    snapshot = supply(
        cluster("clu_a", "iad-1", devices("a", count=8)),
        cluster("clu_b", "sfo-1", devices("b", count=8)),
    )
    decision = expand(demand(desired_scale=6), snapshot)
    assert sorted(shares_of(decision).values()) == [2, 4]
    assert decision.placed == 6


def test_spread_divides_a_workload_as_evenly_as_the_clusters_allow():
    snapshot = supply(
        cluster("clu_a", "iad-1", devices("a", count=8)),
        cluster("clu_b", "sfo-1", devices("b", count=8)),
    )
    decision = expand(demand(desired_scale=6, strategy="spread"), snapshot)
    assert shares_of(decision) == {"iad-1": 3, "sfo-1": 3}


def test_a_gang_that_fits_nowhere_whole_is_rejected_rather_than_split():
    snapshot = supply(
        cluster("clu_a", "iad-1", devices("a", count=4)),
        cluster("clu_b", "sfo-1", devices("b", count=4)),
    )
    decision = expand(demand(kind="gang", gang_size=4, desired_scale=4), snapshot)
    assert decision.shares == ()
    assert decision.shortfall == 4
    assert decision.splittable is False
    assert "cannot be split" in decision.summary


def test_a_gang_that_fits_in_one_cluster_gets_exactly_one_share():
    snapshot = supply(
        cluster("clu_a", "iad-1", devices("a", count=4)),
        cluster("clu_b", "sfo-1", devices("b", count=8)),
    )
    decision = expand(demand(kind="gang", gang_size=4, desired_scale=4), snapshot)
    assert shares_of(decision) == {"sfo-1": 4}


def test_a_floor_is_honoured_before_anything_is_ranked():
    snapshot = supply(
        cluster("clu_a", "iad-1", devices("a", count=8)),
        cluster("clu_b", "sfo-1", devices("b", count=8)),
    )
    decision = expand(demand(desired_scale=4, cluster_floors={"clu_b": 3}), snapshot)
    assert shares_of(decision) == {"iad-1": 1, "sfo-1": 3}


def test_a_floor_that_the_cluster_cannot_meet_leaves_the_rest_to_the_fill():
    snapshot = supply(
        cluster("clu_a", "iad-1", devices("a", count=8)),
        cluster("clu_b", "sfo-1", devices("b", count=2)),
    )
    decision = expand(demand(desired_scale=4, cluster_floors={"clu_b": 3}), snapshot)
    assert shares_of(decision) == {"iad-1": 3, "sfo-1": 1}


def test_the_fan_out_cap_leaves_the_overflow_as_a_shortfall():
    snapshot = supply(
        cluster("clu_a", "iad-1", devices("a", count=4)),
        cluster("clu_b", "sfo-1", devices("b", count=4)),
    )
    decision = expand(demand(desired_scale=4, max_clusters=1), snapshot)
    assert len(decision.shares) == 1
    assert decision.placed == 2
    assert decision.shortfall == 2


def test_a_partial_placement_reports_what_it_could_not_place():
    snapshot = supply(cluster("clu_a", "iad-1", devices("a", count=4)))
    decision = expand(demand(desired_scale=5), snapshot)
    assert decision.placed == 2
    assert decision.shortfall == 3


def test_a_scale_up_fills_the_cluster_already_running_before_reaching_for_another():
    snapshot = supply(
        cluster("clu_a", "iad-1", devices("a", count=8)),
        cluster("clu_b", "sfo-1", devices("b", count=8)),
    )
    decision = expand(demand(desired_scale=3, existing={"clu_b": 1}), snapshot)
    assert shares_of(decision) == {"sfo-1": 3}


def test_a_pinned_cluster_is_the_only_candidate():
    snapshot = supply(
        cluster("clu_a", "iad-1", devices("a", count=4)),
        cluster("clu_b", "sfo-1", devices("b", count=8)),
    )
    decision = expand(demand(desired_scale=4, pinned_cluster="clu_a"), snapshot)
    assert shares_of(decision) == {"iad-1": 2}
    assert decision.shortfall == 2


def test_a_pin_at_a_cluster_with_no_capacity_places_nothing():
    snapshot = supply(
        cluster("clu_a", "iad-1", devices("a", count=0)),
        cluster("clu_b", "sfo-1", devices("b", count=8)),
    )
    decision = expand(demand(desired_scale=4, pinned_cluster="clu_a"), snapshot)
    assert decision.shares == ()
    assert decision.shortfall == 4


@pytest.mark.parametrize(
    ("pinned", "expected"),
    [
        (cluster("clu_a", "iad-1", devices("a", count=8), phase="draining"), codes.CLUSTER_PHASE),
        (cluster("clu_a", "iad-1", devices("a", count=8), reachable=False), codes.CLUSTER_UNREACHABLE),
    ],
)
def test_a_pinned_cluster_still_has_to_be_healthy(pinned, expected):
    decision = expand(demand(desired_scale=1, pinned_cluster="clu_a"), supply(pinned))
    assert decision.shares == ()
    assert expected in codes_for(decision, "iad-1")


def test_a_pinned_cluster_still_answers_to_a_deny_policy():
    snapshot = CapacitySnapshot(
        clusters=(cluster("clu_a", "iad-1", devices("a", count=8)),),
        policies=(PolicySnapshot(name="no-iad", denied_clusters=("clu_a",)),),
    )
    decision = expand(demand(desired_scale=1, pinned_cluster="clu_a"), snapshot)
    assert decision.shares == ()
    assert codes.POLICY_CLUSTER_DENIED in codes_for(decision, "iad-1")


def test_a_pinned_cluster_still_answers_to_quota():
    snapshot = CapacitySnapshot(
        clusters=(cluster("clu_a", "iad-1", devices("a", count=8)),),
        quotas=(QuotaSnapshot(project="proj_a", device_class="h100", limit_devices=4),),
    )
    decision = expand(demand(desired_scale=4, pinned_cluster="clu_a"), snapshot)
    assert shares_of(decision) == {"iad-1": 2}
    assert decision.shortfall == 2


def test_a_global_quota_is_spent_once_across_clusters():
    snapshot = CapacitySnapshot(
        clusters=(
            cluster("clu_a", "iad-1", devices("a", count=8)),
            cluster("clu_b", "sfo-1", devices("b", count=8)),
        ),
        quotas=(QuotaSnapshot(project="proj_a", device_class="h100", limit_devices=8),),
    )
    for strategy in ("pack", "spread"):
        decision = expand(demand(desired_scale=8, strategy=strategy), snapshot)
        assert decision.placed == 4, strategy
        assert decision.shortfall == 4, strategy


def test_a_global_quota_budget_is_shared_by_floors_and_fill():
    snapshot = CapacitySnapshot(
        clusters=(
            cluster("clu_a", "iad-1", devices("a", count=8)),
            cluster("clu_b", "sfo-1", devices("b", count=8)),
        ),
        quotas=(QuotaSnapshot(project="proj_a", device_class="h100", limit_devices=6),),
    )
    decision = expand(demand(desired_scale=4, cluster_floors={"clu_b": 2}), snapshot)
    assert shares_of(decision) == {"iad-1": 1, "sfo-1": 2}
    assert decision.shortfall == 1


def test_a_cluster_scoped_quota_is_not_drawn_from_the_global_budget():
    snapshot = CapacitySnapshot(
        clusters=(
            cluster("clu_a", "iad-1", devices("a", count=8)),
            cluster("clu_b", "sfo-1", devices("b", count=8)),
        ),
        quotas=(
            QuotaSnapshot(project="proj_a", device_class="h100", limit_devices=4),
            QuotaSnapshot(project="proj_a", device_class="h100", cluster="clu_b", limit_devices=8),
        ),
    )
    decision = expand(demand(desired_scale=8, strategy="spread"), snapshot)
    assert shares_of(decision) == {"iad-1": 2, "sfo-1": 4}


def test_single_domain_units_are_counted_per_domain_not_per_cluster():
    """Two domains of six hold two four-device units, not three."""
    doms = (nvlink("dom-a", "nvl-a"), nvlink("dom-b", "nvl-b"))
    devs = devices("a", count=6, domain="dom-a", prefix="x") + devices("a", count=6, domain="dom-b", prefix="y")
    snapshot = supply(cluster("clu_a", "iad-1", devs, doms))
    decision = expand(
        demand(unit_device_count=4, desired_scale=3, requires_single_domain=True, domain_kind="nvlink"), snapshot
    )
    assert shares_of(decision) == {"iad-1": 2}
    assert decision.shortfall == 1


def test_tensor_groups_are_counted_on_free_devices_per_node():
    """Two nodes with three free devices each hold two tensor-two groups' worth of room, not three."""
    devs = devices("a", count=3, node="n0", prefix="x") + devices("a", count=3, node="n1", prefix="y")
    snapshot = supply(cluster("clu_a", "iad-1", devs))
    decision = expand(demand(unit_device_count=2, tensor_parallel=2, desired_scale=3), snapshot)
    assert shares_of(decision) == {"iad-1": 2}
    assert decision.shortfall == 1


def test_free_devices_split_across_nodes_cannot_hold_a_tensor_group():
    busy = DeviceHold(workload="other", project="proj_b", priority_value=0, preemptible=False)
    n0 = devices("c1", count=1, node="n0", prefix="x") + [
        DeviceSnapshot(uid=f"x-held{i}", node="n0", device_class="h100", held_by=busy) for i in range(3)
    ]
    n1 = devices("c1", count=1, node="n1", prefix="y") + [
        DeviceSnapshot(uid=f"y-held{i}", node="n1", device_class="h100", held_by=busy) for i in range(3)
    ]
    snap = CapacitySnapshot(clusters=(cluster("c1", "iad-1", n0 + n1),))

    decision = place(request(2, tensor_parallel=2), snap)

    assert decision.placed is False
    reason = verdict_for(decision, "iad-1").reasons[0]
    assert reason.code == codes.INSUFFICIENT_CAPACITY
    assert reason.detail["tensor_groups_available"] == 0


def test_a_paused_workload_keeps_its_deployments_at_a_share_of_zero():
    snapshot = supply(cluster("clu_a", "iad-1", devices("a", count=8)))
    decision = expand(demand(desired_scale=4, paused=True, existing={"clu_a": 4}), snapshot)
    assert shares_of(decision) == {"iad-1": 0}


def test_a_cluster_whose_name_is_taken_by_another_workload_is_not_a_candidate():
    snapshot = supply(
        cluster("clu_a", "iad-1", devices("a", count=8), taken_names={"serve": "wkl_other"}),
        cluster("clu_b", "sfo-1", devices("b", count=8)),
    )
    decision = expand(demand(desired_scale=2), snapshot)
    assert shares_of(decision) == {"sfo-1": 2}
    assert codes.NAME_TAKEN in codes_for(decision, "iad-1")


def test_quota_caps_a_cluster_share_below_what_is_free():
    snapshot = CapacitySnapshot(
        clusters=(cluster("clu_a", "iad-1", devices("a", count=8)), cluster("clu_b", "sfo-1", devices("b", count=8))),
        quotas=(
            QuotaSnapshot(project="proj_a", device_class="h100", cluster="clu_a", limit_devices=4),
            QuotaSnapshot(project="proj_a", device_class="h100", cluster="clu_b", limit_devices=8),
        ),
    )
    decision = expand(demand(desired_scale=6), snapshot)
    assert shares_of(decision) == {"iad-1": 2, "sfo-1": 4}


def test_expansion_is_deterministic():
    snapshot = supply(
        cluster("clu_a", "iad-1", devices("a", count=8)),
        cluster("clu_b", "sfo-1", devices("b", count=8)),
    )
    first = expand(demand(desired_scale=5, strategy="spread"), snapshot)
    second = expand(demand(desired_scale=5, strategy="spread"), snapshot)
    assert first.shares == second.shares


# ---------------------------------------------------------------------------
# Reserved capacity
# ---------------------------------------------------------------------------


def owed(devices_count, workload="wkl_other", **kw):
    return ReservedUnit(
        workload=workload,
        project=kw.pop("project", "proj_a"),
        device_class=kw.pop("device_class", "h100"),
        devices=devices_count,
        deployment=kw.pop("deployment", "dep_other"),
        deployment_name=kw.pop("deployment_name", "other"),
        **kw,
    )


def test_a_reservation_comes_off_the_capacity_a_cluster_offers():
    snap = supply(cluster("clu_a", "iad-1", devices("a", count=8), reserved=(owed(6),)))

    decision = place(request(4), snap)

    assert decision.placed is False
    verdict = verdict_for(decision, "iad-1")
    assert verdict.free_devices == 8
    assert verdict.reserved_devices == 6
    assert verdict.available_devices == 2
    assert verdict.reasons[0].code == codes.INSUFFICIENT_CAPACITY


def test_a_rejection_says_the_devices_are_promised_and_not_missing():
    snap = supply(cluster("clu_a", "iad-1", devices("a", count=8), reserved=(owed(6),)))

    reason = place(request(4), snap).verdicts[0].reasons[0]

    assert "6 reserved by deployments not yet bound" in reason.message
    assert reason.detail["free_devices"] == 8
    assert reason.detail["reserved_devices"] == 6
    assert reason.detail["available_devices"] == 2


def test_the_second_of_two_workloads_does_not_land_on_top_of_the_first():
    """The bug this exists for, written as the sequence that produced it.

    Two workloads created back to back. The first is placed on the emptier cluster
    and its deployment is written; nothing has bound a device yet. Without the
    reservation both reads are identical and the second workload picks the same
    cluster, and the two of them are now over its eight accelerators.
    """
    first = place(
        request(6),
        supply(cluster("clu_a", "iad-1", devices("a", count=8)), cluster("clu_b", "sfo-1", devices("b", count=8))),
    )
    assert first.cluster_name == "iad-1"

    after = supply(
        cluster("clu_a", "iad-1", devices("a", count=8), reserved=(owed(6, workload="wkl_1", deployment="dep_1"),)),
        cluster("clu_b", "sfo-1", devices("b", count=8)),
    )
    second = place(request(6, workload="wkl_2"), after)

    assert second.cluster_name == "sfo-1"


def test_a_workload_does_not_compete_with_its_own_reservation():
    snap = supply(
        cluster("clu_a", "iad-1", devices("a", count=8), reserved=(owed(6, workload="wkl_1", deployment="dep_1"),))
    )

    decision = place(request(4, workload="wkl_1"), snap)

    assert decision.placed is True
    assert verdict_for(decision, "iad-1").reserved_devices == 0


def test_a_scale_up_stays_where_it_is_rather_than_moving_off_its_own_promise():
    snapshot = supply(
        cluster("clu_a", "iad-1", devices("a", count=8), reserved=(owed(4, workload="wkl_1", deployment="dep_1"),)),
        cluster("clu_b", "sfo-1", devices("b", count=8)),
    )
    decision = expand(demand(workload="wkl_1", desired_scale=3, existing={"clu_a": 2}), snapshot)
    assert shares_of(decision) == {"iad-1": 3}


def test_a_pending_gang_takes_the_fabric_it_will_land_in_out_of_reach():
    """Twenty free devices, and only one fabric that could ever hold eight of them.

    That fabric is already promised to a gang the cluster has not bound yet, so the
    cluster has the aggregate room and nowhere contiguous to put this one. Burning
    the largest fabric first is what makes the rejection the honest `topology` and
    not a `capacity` the totals would have waved through.
    """
    doms = (nvlink("dom-a", "nvl-a"), nvlink("dom-b", "nvl-b"), nvlink("dom-c", "nvl-c"))
    devs = (
        devices("c1", count=8, domain="dom-a", prefix="a")
        + devices("c1", count=6, domain="dom-b", prefix="b")
        + devices("c1", count=6, domain="dom-c", prefix="c")
    )
    reserved = (owed(8, requires_single_domain=True, domain_kind="nvlink"),)
    snap = supply(cluster("clu_a", "iad-1", devs, doms, reserved=reserved))

    decision = place(request(8, kind="gang", requires_single_domain=True, domain_kind="nvlink"), snap)

    assert decision.placed is False
    verdict = verdict_for(decision, "iad-1")
    assert verdict.available_devices == 12
    assert verdict.largest_domain_free_devices == 6
    assert verdict.reasons[0].code == codes.INSUFFICIENT_TOPOLOGY


def test_the_same_fabric_is_reachable_once_nothing_is_promised_it():
    doms = (nvlink("dom-a", "nvl-a"), nvlink("dom-b", "nvl-b"), nvlink("dom-c", "nvl-c"))
    devs = (
        devices("c1", count=8, domain="dom-a", prefix="a")
        + devices("c1", count=6, domain="dom-b", prefix="b")
        + devices("c1", count=6, domain="dom-c", prefix="c")
    )
    snap = supply(cluster("clu_a", "iad-1", devs, doms))

    decision = place(request(8, kind="gang", requires_single_domain=True, domain_kind="nvlink"), snap)

    assert decision.placed is True
    assert verdict_for(decision, "iad-1").domain == "dom-a"


def test_a_spreading_reservation_is_charged_to_the_cluster_and_not_to_a_fabric():
    """Eight free in one fabric, and four pending replicas that will spread.

    Charging the four to the fabric as well as to the cluster would count them
    twice and reject a gang the cluster can still hold.
    """
    doms = (nvlink("dom-a", "nvl-a"),)
    devs = devices("c1", count=8, domain="dom-a", prefix="a")
    snap = supply(cluster("clu_a", "iad-1", devs, doms))
    assert place(request(8, kind="gang", requires_single_domain=True, domain_kind="nvlink"), snap).placed is True

    snap = supply(cluster("clu_a", "iad-1", devs, doms, reserved=(owed(4),)))
    decision = place(request(8, kind="gang", requires_single_domain=True, domain_kind="nvlink"), snap)

    assert decision.placed is False
    verdict = verdict_for(decision, "iad-1")
    assert verdict.largest_domain_free_devices == 8
    assert verdict.available_devices == 4
    assert verdict.reasons[0].code == codes.INSUFFICIENT_CAPACITY


def test_a_cluster_scoped_quota_counts_the_projects_own_pending_draw():
    snapshot = CapacitySnapshot(
        clusters=(cluster("clu_a", "iad-1", devices("a", count=16), reserved=(owed(6),)),),
        quotas=(QuotaSnapshot(project="proj_a", device_class="h100", cluster="clu_a", limit_devices=8),),
    )

    decision = place(request(4), snapshot)

    assert decision.placed is False
    assert verdict_for(decision, "iad-1").quota_headroom == 2
    assert codes.QUOTA_EXHAUSTED in codes_for(decision, "iad-1")


def test_a_global_quota_counts_a_pending_draw_in_every_cluster():
    snapshot = CapacitySnapshot(
        clusters=(
            cluster("clu_a", "iad-1", devices("a", count=16)),
            cluster("clu_b", "sfo-1", devices("b", count=16), reserved=(owed(6),)),
        ),
        quotas=(QuotaSnapshot(project="proj_a", device_class="h100", limit_devices=8),),
    )

    decision = place(request(4), snapshot)

    assert decision.placed is False
    assert verdict_for(decision, "iad-1").quota_headroom == 2


def test_another_projects_reservation_does_not_touch_this_ones_quota():
    snapshot = CapacitySnapshot(
        clusters=(cluster("clu_a", "iad-1", devices("a", count=16), reserved=(owed(6, project="proj_b"),)),),
        quotas=(QuotaSnapshot(project="proj_a", device_class="h100", cluster="clu_a", limit_devices=8),),
    )

    decision = place(request(4), snapshot)

    assert decision.placed is True
    assert verdict_for(decision, "iad-1").quota_headroom == 8


def test_a_reservation_is_not_reclaimable():
    """A promise is not a hold, so there is nothing to evict and nothing to reclaim."""
    snap = supply(cluster("clu_a", "iad-1", devices("a", count=8), reserved=(owed(6),)))

    decision = place(request(4, preempts=True, priority_value=100), snap)

    assert decision.placed is False
    assert verdict_for(decision, "iad-1").preemption is None


# --- a workload's own devices -----------------------------------------------


def own(prefix, count, workload="wkl_1", node="n0", **kw):
    return [
        DeviceSnapshot(uid=f"{prefix}{i}", node=node, device_class="h100", held_by=hold(workload, **kw))
        for i in range(count)
    ]


def test_scaling_up_keeps_the_replicas_where_they_already_run():
    snapshot = supply(
        cluster("clu_a", "iad-1", own("a-own", 4) + devices("a", count=1)),
        cluster("clu_b", "sfo-1", devices("b", count=8)),
    )
    decision = expand(demand(unit_device_count=1, desired_scale=5, existing={"clu_a": 4}), snapshot)
    assert shares_of(decision) == {"iad-1": 5}


def test_re_expanding_unchanged_on_a_full_cluster_keeps_the_deployment():
    snapshot = supply(
        cluster("clu_a", "iad-1", own("a-own", 8)),
        cluster("clu_b", "sfo-1", devices("b", count=8)),
    )
    decision = expand(demand(unit_device_count=2, desired_scale=4, existing={"clu_a": 4}), snapshot)
    assert shares_of(decision) == {"iad-1": 4}


def test_another_workloads_devices_are_still_taken():
    snapshot = supply(cluster("clu_a", "iad-1", own("a-other", 8, workload="wkl_2")))
    decision = expand(demand(unit_device_count=2, desired_scale=1), snapshot)
    assert decision.shares == ()


def test_a_workloads_own_draw_does_not_count_against_its_quota_headroom():
    snap = CapacitySnapshot(
        clusters=(cluster("c1", "iad-1", own("own", 4, workload="wl_1") + devices("c1", count=4)),),
        quotas=(QuotaSnapshot(project="proj_a", device_class="h100", limit_devices=6, allocated_devices=4),),
    )
    verdict = verdict_for(place(request(6), snap), "iad-1")
    assert verdict.quota_headroom == 6
    assert verdict.feasible is True


def test_a_workload_never_preempts_itself():
    snap = CapacitySnapshot(
        clusters=(cluster("c1", "iad-1", own("own", 4, workload="wl_1", priority_value=10, preemptible=True)),),
    )
    decision = place(request(4, priority_value=100, preempts=True), snap)
    assert decision.placed is True
    assert decision.preemption is None
