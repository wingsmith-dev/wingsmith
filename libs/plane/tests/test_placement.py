import inspect

from types import SimpleNamespace

import pytest

from app.modules import placement
from app.modules.placement import DeviceHold
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
    for module in (placement.engine, placement.snapshot, placement.decision):
        source = inspect.getsource(module)
        assert "app.db" not in source
        assert "app.api" not in source


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
        "requested_devices": 8,
        "domain_kind": "nvlink",
    }
    assert "6 free h100 in its largest nvlink domain, 8 requested contiguously" in reason.message


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


def test_preemption_is_identified_and_never_performed():
    low = DeviceHold(workload="train_1", project="proj_b", priority_value=10, preemptible=True)
    devs = devices("c1", count=4) + [
        DeviceSnapshot(uid=f"held{i}", node="n1", device_class="h100", held_by=low) for i in range(4)
    ]
    snap = CapacitySnapshot(clusters=(cluster("c1", "iad-1", devs),))

    decision = place(request(8, priority_value=100, preempts=True), snap)

    assert decision.placed is True
    assert decision.requires_preemption is True
    assert [v.workload for v in decision.preemption.victims] == ["train_1"]
    assert decision.preemption.devices_reclaimed == 4
    assert "only if train_1" in decision.summary


def test_preemption_needs_both_a_preempting_priority_and_a_preemptible_victim():
    pinned = DeviceHold(workload="serve_1", project="proj_b", priority_value=10, preemptible=False)
    devs = devices("c1", count=4) + [
        DeviceSnapshot(uid=f"held{i}", node="n1", device_class="h100", held_by=pinned) for i in range(4)
    ]
    snap = CapacitySnapshot(clusters=(cluster("c1", "iad-1", devs),))

    assert place(request(8, priority_value=100, preempts=True), snap).placed is False
    assert place(request(8, priority_value=100, preempts=False), snap).placed is False


def test_a_preemption_free_cluster_beats_one_needing_eviction():
    low = DeviceHold(workload="train_1", project="proj_b", priority_value=10, preemptible=True)
    busy = devices("c1", count=4) + [
        DeviceSnapshot(uid=f"held{i}", node="n1", device_class="h100", held_by=low) for i in range(4)
    ]
    snap = CapacitySnapshot(
        clusters=(
            cluster("c1", "aaa-1", busy),
            cluster("c2", "zzz-1", devices("c2", count=8)),
        )
    )

    decision = place(request(8, priority_value=100, preempts=True), snap)

    assert decision.cluster_name == "zzz-1"
    assert decision.requires_preemption is False


def test_a_gang_preemption_names_the_whole_victim_allocation():
    low = DeviceHold(workload="train_1", project="proj_b", priority_value=10, preemptible=True)
    doms = (nvlink("dom-a", "nvl-a"),)
    devs = devices("c1", count=2, domain="dom-a") + [
        DeviceSnapshot(uid=f"held{i}", node="n1", device_class="h100", domain="dom-a", held_by=low) for i in range(6)
    ]
    snap = CapacitySnapshot(clusters=(cluster("c1", "iad-1", devs, doms),))

    decision = place(
        request(8, kind="gang", requires_single_domain=True, domain_kind="nvlink", priority_value=100, preempts=True),
        snap,
    )

    assert decision.placed is True
    victim = decision.preemption.victims[0]
    assert (victim.workload, victim.devices_in_scope, victim.devices_released) == ("train_1", 6, 6)


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


def a_recipe(**overrides):
    fields = {
        "device_class": "h100",
        "device_count": 4,
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

    Three replicas of a four-device recipe need twelve devices in the cluster, but
    only each four have to share an NVLink domain — the replicas do not talk to
    each other over it.
    """
    request = PlacementService(db=None).request(
        recipe=a_recipe(),
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
        recipe=a_recipe(),
        project_uid="prj_1",
        priority=a_priority(value=900, preempts=True),
        workload="serve",
    )
    assert (request.priority_value, request.preempts) == (900, True)


def test_a_null_deployment_requirement_falls_through_to_the_recipe():
    request = PlacementService(db=None).request(
        recipe=a_recipe(),
        project_uid="prj_1",
        priority=a_priority(),
        workload="serve",
        requires_single_domain=None,
        domain_kind=None,
    )
    assert request.requires_single_domain is True
    assert request.domain_kind == "nvlink"


def test_a_deployment_can_declare_a_shape_the_recipe_does_not_have():
    request = PlacementService(db=None).request(
        recipe=a_recipe(requires_single_topology_domain=False, topology_domain_kind=None),
        project_uid="prj_1",
        priority=a_priority(),
        workload="gang",
        kind="gang",
        requires_single_domain=True,
        domain_kind="nvswitch",
    )
    assert request.requires_single_domain is True
    assert request.domain_kind == "nvswitch"


def test_a_deployment_can_relax_a_requirement_the_recipe_asserts():
    request = PlacementService(db=None).request(
        recipe=a_recipe(),
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


def test_a_pinned_cluster_is_the_only_candidate_and_is_not_checked_for_capacity():
    snapshot = supply(
        cluster("clu_a", "iad-1", devices("a", count=0)),
        cluster("clu_b", "sfo-1", devices("b", count=8)),
    )
    decision = expand(demand(desired_scale=4, pinned_cluster="clu_a"), snapshot)
    assert shares_of(decision) == {"iad-1": 4}


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
