"""The frozen objectives table is the one definition of an availability number."""

import pytest

from app.core.exceptions import Invalid
from app.modules.metrics.promql import DIALECT
from app.modules.quotas.services import validate_serving
from app.modules.metrics.registry import BY_NAME
from app.modules.metrics.objectives import OUTCOMES
from app.modules.metrics.objectives import AVAILABILITY_2026_1
from app.modules.metrics.objectives import AVAILABILITY_OBJECTIVES
from app.modules.metrics.objectives import DEFAULT_AVAILABILITY_OBJECTIVE
from app.modules.metrics.objectives import Objective
from app.modules.metrics.objectives import _check
from app.modules.metrics.objectives import objective


def test_every_entry_names_metrics_the_registry_and_the_dialect_both_answer():
    for entry in AVAILABILITY_OBJECTIVES.values():
        for name in (entry.failure_metric, entry.availability_metric):
            assert name in BY_NAME
            assert name in DIALECT


def test_the_dialect_is_rendered_from_the_entry_and_not_beside_it():
    """A hand-copied regex is how a denominator drifts from the definition a quota names."""
    entry = AVAILABILITY_2026_1
    rendered = DIALECT[entry.failure_metric].template
    assert entry.failed_matcher in rendered
    assert entry.counted_matcher in rendered
    assert DIALECT[entry.availability_metric].template == f"100 - ({rendered})"


def test_the_2026_1_definition_is_the_one_quotas_were_sold_under():
    entry = AVAILABILITY_2026_1
    assert entry.failed_matcher == 'outcome=~"shed|no_upstream|upstream_error|upstream_timeout|internal_error"'
    assert entry.counted_matcher == 'outcome!="canceled"'
    assert "rate_limited" not in entry.failed, "a 429 over the caller's own limit is the standard's carve-out"
    assert set(entry.failed) <= entry.counted
    assert "canceled" not in entry.counted


def test_the_default_is_an_entry_of_the_table():
    assert objective(DEFAULT_AVAILABILITY_OBJECTIVE) is AVAILABILITY_2026_1
    assert objective("availability-1999.1") is None


@pytest.mark.parametrize(
    "entry",
    [
        Objective("v", ("shed", "teapot"), ("canceled",), "m", "a", ""),
        Objective("v", ("shed",), (), "m", "a", ""),
        Objective("v", ("shed", "canceled"), ("canceled",), "m", "a", ""),
    ],
)
def test_an_entry_outside_the_gateways_vocabulary_cannot_be_filed(entry):
    with pytest.raises(ValueError):
        _check({entry.version: entry})


def test_an_entry_filed_under_another_version_cannot_be_filed():
    with pytest.raises(ValueError):
        _check({"availability-2027.1": AVAILABILITY_2026_1})


def test_the_vocabulary_is_the_gateways_closed_set_of_ten():
    assert len(OUTCOMES) == 10


def test_a_quota_naming_a_version_the_table_does_not_hold_is_refused():
    with pytest.raises(Invalid) as refused:
        validate_serving({"availability_objective": "availability-1999.1"}, {})
    assert refused.value.detail["allowed"] == sorted(AVAILABILITY_OBJECTIVES)
