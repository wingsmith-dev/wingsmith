"""Metric-backed alerting: the judgement, the pass that feeds it, and the CRUD around it.

No database anywhere in here, which is the property the design is for. `rules.py`
takes floats and returns intents, `conditions.metric_thresholds` takes a reader it
does not construct, and the module's validation is functions over a request struct.
The one thing that needs Postgres, the threshold rows themselves, is the one thing
these tests stub.
"""

import ast
import datetime as dt

from decimal import Decimal
from pathlib import Path

import pytest
import msgspec

from litestar.di import Provide
from litestar.testing import create_test_client

from app.auth import AuthManager
from app.reactor import rules
from app.reactor import conditions
from app.core.ids import PREFIXES
from app.core.ids import ALERT_THRESHOLD
from app.core.ids import is_uid
from app.core.ids import new_uid
from app.db.models import AlertThreshold
from app.core.context import RequestContext
from app.reactor.rules import ABOVE
from app.reactor.rules import BELOW
from app.reactor.rules import Threshold
from app.reactor.rules import MetricSubject
from app.core.exceptions import Invalid
from app.core.exceptions import NotFound
from app.reactor.measures import Reading
from app.reactor.measures import MetricQuery
from app.reactor.measures import ServiceMetricReader
from app.modules.alerts.routes import AlertRuleRoutes
from app.modules.alerts.routes import AlertThresholdRoutes
from app.modules.alerts.schemas import AlertThresholdSchema
from app.modules.alerts.schemas import AlertThresholdCreateRequest
from app.modules.alerts.schemas import AlertThresholdUpdateRequest
from app.modules.metrics.client import MetricsUnavailable
from app.modules.alerts.services import AlertThresholdService
from app.modules.alerts.services import catalogue
from app.modules.metrics.schemas import SampleSchema
from app.modules.metrics.schemas import MetricsSchema
from app.modules.metrics.registry import BY_NAME
from app.modules.metrics.registry import family
from app.modules.alerts.repository import AlertThresholdRepository


TTFT = "deployment.ttft_high"
SPEED = "deployment.output_speed_below_floor"
TEMPERATURE = "node.gpu_temperature_high"

DEPLOYMENT = MetricSubject(uid="dep_01j9x2aaaaaaaaaaaaaaaaaaaa", name="gemma-us", cluster_name="us")
OTHER = MetricSubject(uid="dep_01j9x2bbbbbbbbbbbbbbbbbbbb", name="gemma-ap", cluster_name="ap")


def threshold(rule: str = TTFT, value: float = 2.0, **kwargs) -> Threshold:
    definition = rules.METRIC_RULES.get(rule)
    kwargs.setdefault("subject_type", definition.subject_type if definition else "deployment")
    return Threshold(rule=rule, value=value, **kwargs)


class FakeReader:
    """A metrics backend the size of a dict. The seam the design exists to allow."""

    def __init__(self, values: dict[str, float | None] | None = None) -> None:
        self.values = values or {}
        self.queries: list[MetricQuery] = []

    async def read(self, query: MetricQuery) -> Reading:
        self.queries.append(query)
        value = self.values.get(query.metric)
        return Reading(query.metric, value=value, withheld=None if value is not None else "no series")


def test_the_threshold_prefix_is_registered():
    assert is_uid(new_uid(ALERT_THRESHOLD))
    assert ALERT_THRESHOLD in PREFIXES


def test_every_rule_names_a_metric_the_registry_actually_has():
    """A rule addresses a metric by its Hangar name, never a series and never PromQL."""
    for definition in rules.METRIC_RULES.values():
        assert definition.metric in BY_NAME, definition.rule
        assert definition.comparison in rules.COMPARISONS
        assert definition.severity in rules.SEVERITY_ORDER
        assert definition.rule.startswith(f"{definition.subject_type}.")


def test_no_rule_claims_to_measure_availability_from_cluster_side_series():
    """The absence that has to stay absent.

    A request refused at the front door, routed nowhere or timed out upstream emits
    nothing an engine exporter can count, so any ratio built out of these series
    reports a fully-broken cluster as "no traffic", not as an outage. A rule
    here that claimed to watch availability would be a green light with nothing
    behind it, and it would be believed.

    `serving.availability` exists and is exempt, because it is measured at the
    gateway: the one hop that saw the requests no cluster did. The ban is on
    claiming the number out of `gpu` or `inference` series, not on the number.
    """
    banned = ("availability", "failure", "error_rate", "success_rate", "uptime")
    for definition in rules.METRIC_RULES.values():
        assert not any(word in definition.rule for word in banned), definition.rule
        assert not any(word in definition.metric for word in banned), definition.metric
    cluster_side = {metric.name for metric in family("gpu") + family("inference")}
    assert not any(any(word in name for word in banned) for name in cluster_side)


def test_a_breach_is_strict_on_both_sides_of_the_boundary():
    assert rules.breached(ABOVE, 2.01, 2.0)
    assert not rules.breached(ABOVE, 2.0, 2.0)
    assert not rules.breached(ABOVE, 1.99, 2.0)
    assert rules.breached(BELOW, 29.9, 30.0)
    assert not rules.breached(BELOW, 30.0, 30.0)
    assert not rules.breached(BELOW, 30.1, 30.0)


def test_an_unrecognised_comparison_is_never_a_breach():
    assert not rules.breached("sideways", 1.0, 0.0)


def test_a_breach_becomes_an_intent_carrying_the_number_it_was_judged_against():
    intent = rules.metric_breach(DEPLOYMENT, threshold(value=2.0, reason="clause 4.2"), observed=3.5)
    assert intent is not None
    assert intent.rule == TTFT
    assert intent.source == rules.CONDITION
    assert intent.severity == rules.WARNING
    assert intent.subject_type == "deployment"
    assert intent.subject_uid == DEPLOYMENT.uid
    assert intent.dedupe_key == f"{TTFT}:{DEPLOYMENT.uid}"
    assert intent.details["metric"] == "inference.ttft"
    assert intent.details["comparison"] == ABOVE
    assert intent.details["observed"] == 3.5
    assert intent.details["threshold"] == 2.0
    assert intent.details["declared_reason"] == "clause 4.2"
    assert "3.5" in intent.title and "2" in intent.title


def test_a_measurement_that_clears_the_threshold_is_not_an_alert():
    assert rules.metric_breach(DEPLOYMENT, threshold(value=2.0), observed=1.0) is None


def test_a_floor_fires_below_and_not_above():
    below = rules.metric_breach(DEPLOYMENT, threshold(rule=SPEED, value=30.0), observed=12.0)
    assert below is not None and below.rule == SPEED
    assert rules.metric_breach(DEPLOYMENT, threshold(rule=SPEED, value=30.0), observed=44.0) is None


def test_no_measurement_never_alerts():
    """`None` is "the store had no series", which is not zero and is not a breach.

    The same asymmetry that keeps availability out of the catalogue: an idle
    deployment reports nothing, and reading that silence as a floor being missed
    would page someone every night for every deployment nobody was using.
    """
    assert rules.metric_breach(DEPLOYMENT, threshold(rule=SPEED, value=30.0), observed=None) is None
    assert rules.metric_breach(DEPLOYMENT, threshold(value=2.0), observed=None) is None


def test_a_row_naming_a_rule_that_does_not_exist_produces_nothing():
    """A plane rolled back past the rule a row names evaluates what it has and not what it does not."""
    assert rules.metric_breach(DEPLOYMENT, threshold(rule="deployment.invented"), observed=99.0) is None


def test_the_row_overrides_the_rules_own_severity_window_and_debounce():
    declared = threshold(value=2.0, severity=rules.CRITICAL, lookback="30m", for_seconds=60)
    intent = rules.metric_breach(DEPLOYMENT, declared, observed=9.0)
    assert intent is not None
    assert intent.severity == rules.CRITICAL
    assert intent.for_seconds == 60
    assert intent.details["lookback"] == "30m"


def test_a_row_that_pins_nothing_takes_the_rules_defaults():
    definition = rules.METRIC_RULES[TTFT]
    intent = rules.metric_breach(DEPLOYMENT, threshold(value=2.0), observed=9.0)
    assert intent is not None
    assert intent.severity == definition.severity
    assert intent.for_seconds == definition.for_seconds
    assert intent.details["lookback"] == definition.lookback


def test_for_seconds_zero_on_the_row_means_zero_and_not_the_default():
    """A falsy override is still an override. `or` here would silently restore the default."""
    intent = rules.metric_breach(DEPLOYMENT, threshold(value=2.0, for_seconds=0), observed=9.0)
    assert intent is not None
    assert intent.for_seconds == 0


def test_a_scoped_row_beats_the_fleet_default_and_only_for_its_own_subject():
    fleet = threshold(value=2.0)
    scoped = threshold(value=0.5, subject_uid=DEPLOYMENT.uid)
    declared = [fleet, scoped]
    assert rules.resolve_threshold(declared, TTFT, DEPLOYMENT.uid) is scoped
    assert rules.resolve_threshold(declared, TTFT, OTHER.uid) is fleet


def test_the_fleet_default_is_what_a_null_subject_means():
    declared = [threshold(value=2.0)]
    assert rules.resolve_threshold(declared, TTFT, OTHER.uid) is declared[0]


def test_a_rule_nobody_declared_a_number_for_resolves_to_nothing():
    assert rules.resolve_threshold([threshold(value=2.0)], SPEED, DEPLOYMENT.uid) is None
    assert rules.resolve_threshold([], TTFT, DEPLOYMENT.uid) is None


def test_a_row_scoped_to_another_subject_does_not_leak_onto_this_one():
    declared = [threshold(value=0.5, subject_uid=OTHER.uid)]
    assert rules.resolve_threshold(declared, TTFT, DEPLOYMENT.uid) is None


def test_undimensioned_row_wins_over_dimensioned_sibling():
    plain = threshold(value=2.0)
    narrowed = threshold(value=4.0, dimensions={"input_bucket": "32k_64k"})
    assert rules.resolve_threshold([narrowed, plain], TTFT, DEPLOYMENT.uid) is plain
    assert rules.resolve_threshold([plain, narrowed], TTFT, DEPLOYMENT.uid) is plain


def test_two_narrowings_of_one_rule_are_two_alerts():
    first = rules.metric_breach(DEPLOYMENT, threshold(value=2.0, dimensions={"input_bucket": "6k_16k"}), 9.0)
    second = rules.metric_breach(DEPLOYMENT, threshold(value=2.0, dimensions={"input_bucket": "32k_64k"}), 9.0)
    assert first is not None and second is not None
    assert first.dedupe_key != second.dedupe_key


def test_the_dedupe_key_does_not_move_with_the_measurement():
    """It is the identity of an ongoing problem, so nothing that varies per tick may enter it."""
    first = rules.metric_breach(DEPLOYMENT, threshold(value=2.0), observed=3.0)
    second = rules.metric_breach(DEPLOYMENT, threshold(value=2.0), observed=7.0)
    assert first is not None and second is not None
    assert first.dedupe_key == second.dedupe_key


PURE_IMPORTS = {"typing", "dataclasses", "datetime", "enum", "math", "collections", "collections.abc"}


def test_rules_stay_free_of_io():
    """`rules.py` is pure by design, and the design is only worth as much as this test.

    The moment a rule can fetch its own number, the interesting half of an alerting
    system needs a database and a metrics store to exercise, and judgement that
    needs a fixture to run is judgement that stops being run.
    """
    tree = ast.parse(Path(rules.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported <= PURE_IMPORTS, imported - PURE_IMPORTS
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.AsyncFunctionDef, ast.Await))]


async def test_the_pass_asks_for_nothing_when_nobody_declared_anything(monkeypatch):
    """The threshold table is the work list; an empty one costs one query and no reads."""
    monkeypatch.setattr(conditions, "declared_thresholds", fake_declared([]))
    reader = FakeReader({"inference.ttft": 9.0})
    assert await conditions.metric_thresholds(db=None, reader=reader) == []
    assert reader.queries == []


async def test_a_fleet_default_is_evaluated_for_every_subject(monkeypatch):
    monkeypatch.setattr(conditions, "declared_thresholds", fake_declared([threshold(value=2.0)]))
    monkeypatch.setattr(conditions, "SUBJECTS", {"deployment": fake_subjects([(DEPLOYMENT, {}), (OTHER, {})])})
    reader = FakeReader({"inference.ttft": 9.0})
    intents = await conditions.metric_thresholds(db=None, reader=reader)
    assert {intent.subject_uid for intent in intents} == {DEPLOYMENT.uid, OTHER.uid}
    assert {query.lookback for query in reader.queries} == {rules.METRIC_RULES[TTFT].lookback}


async def test_a_scoped_row_changes_the_number_for_one_subject_only(monkeypatch):
    declared = [threshold(value=20.0), threshold(value=2.0, subject_uid=DEPLOYMENT.uid)]
    monkeypatch.setattr(conditions, "declared_thresholds", fake_declared(declared))
    monkeypatch.setattr(conditions, "SUBJECTS", {"deployment": fake_subjects([(DEPLOYMENT, {}), (OTHER, {})])})
    intents = await conditions.metric_thresholds(db=None, reader=FakeReader({"inference.ttft": 9.0}))
    assert [intent.subject_uid for intent in intents] == [DEPLOYMENT.uid]
    assert intents[0].details["threshold"] == 2.0


async def test_a_subject_whose_measurement_is_withheld_produces_no_alert(monkeypatch):
    monkeypatch.setattr(conditions, "declared_thresholds", fake_declared([threshold(value=2.0)]))
    monkeypatch.setattr(conditions, "SUBJECTS", {"deployment": fake_subjects([(DEPLOYMENT, {})])})
    assert await conditions.metric_thresholds(db=None, reader=FakeReader({})) == []


async def test_a_label_still_mutes_a_threshold_backed_condition(monkeypatch):
    """The declared row supplies the number; the label is the operational override and wins."""
    monkeypatch.setattr(conditions, "declared_thresholds", fake_declared([threshold(value=2.0)]))
    monkeypatch.setattr(
        conditions,
        "SUBJECTS",
        {"deployment": fake_subjects([(DEPLOYMENT, {"hangar.dev/alert": "off"}), (OTHER, {})])},
    )
    intents = await conditions.metric_thresholds(db=None, reader=FakeReader({"inference.ttft": 9.0}))
    assert [intent.subject_uid for intent in intents] == [OTHER.uid]


async def test_a_label_severity_beats_the_declared_one(monkeypatch):
    declared = [threshold(value=2.0, severity=rules.WARNING)]
    monkeypatch.setattr(conditions, "declared_thresholds", fake_declared(declared))
    monkeypatch.setattr(
        conditions,
        "SUBJECTS",
        {"deployment": fake_subjects([(DEPLOYMENT, {"hangar.dev/alert.severity": "critical"})])},
    )
    intents = await conditions.metric_thresholds(db=None, reader=FakeReader({"inference.ttft": 9.0}))
    assert [intent.severity for intent in intents] == [rules.CRITICAL]


async def test_a_node_rule_is_never_asked_about_a_deployment(monkeypatch):
    declared = [threshold(rule=TEMPERATURE, value=87.0)]
    monkeypatch.setattr(conditions, "declared_thresholds", fake_declared(declared))
    monkeypatch.setattr(conditions, "SUBJECTS", {"deployment": fake_subjects([(DEPLOYMENT, {})])})
    reader = FakeReader({"gpu.temperature": 95.0})
    assert await conditions.metric_thresholds(db=None, reader=reader) == []
    assert reader.queries == []


def fake_declared(thresholds: list[Threshold]):
    async def declared(db):
        return thresholds

    return declared


def fake_subjects(subjects: list[tuple[MetricSubject, dict]]):
    async def enumerate_subjects(db):
        return subjects

    return enumerate_subjects


class StubMetrics:
    """Enough of `MetricsService` for the reader. Counts calls, because caching is the point."""

    def __init__(self, answer=None, raises=None) -> None:
        self.answer = answer
        self.raises = raises
        self.calls = 0

    async def deployment(self, ref, window=None, step=None, span=3600, series=False):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.answer

    async def node(self, ref, window=None, step=None, span=3600, series=False):
        return await self.deployment(ref, window=window)


def answer(**samples) -> MetricsSchema:
    return MetricsSchema(
        scope="deployment",
        ref="dep_1",
        cluster="clu_1",
        source=None,
        window="5m",
        samples=[SampleSchema(metric=name, title=name, unit="seconds", value=value) for name, value in samples.items()],
    )


def reader_over(stub: StubMetrics) -> ServiceMetricReader:
    reader = ServiceMetricReader(db=None)
    reader.metrics = stub
    return reader


async def test_the_reader_returns_the_metric_it_was_asked_for():
    reader = reader_over(StubMetrics(answer(**{"inference.ttft": 3.0, "inference.latency": 9.0})))
    reading = await reader.read(MetricQuery("inference.ttft", "deployment", "dep_1", "5m"))
    assert reading.value == 3.0
    assert reading.withheld is None


async def test_one_subject_and_window_is_fetched_once_however_many_rules_ask():
    stub = StubMetrics(answer(**{"inference.ttft": 3.0, "inference.latency": 9.0}))
    reader = reader_over(stub)
    await reader.read(MetricQuery("inference.ttft", "deployment", "dep_1", "5m"))
    await reader.read(MetricQuery("inference.latency", "deployment", "dep_1", "5m"))
    assert stub.calls == 1


async def test_a_different_window_is_a_different_question():
    stub = StubMetrics(answer(**{"inference.ttft": 3.0}))
    reader = reader_over(stub)
    await reader.read(MetricQuery("inference.ttft", "deployment", "dep_1", "5m"))
    await reader.read(MetricQuery("inference.ttft", "deployment", "dep_1", "30m"))
    assert stub.calls == 2


async def test_an_empty_series_is_withheld_rather_than_read_as_zero():
    reader = reader_over(StubMetrics(answer(**{"inference.ttft": None})))
    reading = await reader.read(MetricQuery("inference.ttft", "deployment", "dep_1", "5m"))
    assert reading.value is None
    assert reading.withheld == "no series"


async def test_a_store_that_cannot_answer_withholds_and_does_not_raise():
    reader = reader_over(StubMetrics(raises=MetricsUnavailable("prometheus refused")))
    reading = await reader.read(MetricQuery("inference.ttft", "deployment", "dep_1", "5m"))
    assert reading.value is None
    assert "refused" in (reading.withheld or "")


async def test_a_subject_that_no_longer_exists_withholds_too():
    reader = reader_over(StubMetrics(raises=NotFound("no deployment 'dep_1'")))
    reading = await reader.read(MetricQuery("inference.ttft", "deployment", "dep_1", "5m"))
    assert reading.value is None
    assert reading.withheld


async def test_an_unavailable_answer_is_withheld_with_its_reason():
    unavailable = answer()
    unavailable.unavailable = "no metrics source configured for cluster 'us'"
    reader = reader_over(StubMetrics(unavailable))
    reading = await reader.read(MetricQuery("inference.ttft", "deployment", "dep_1", "5m"))
    assert reading.withheld == "no metrics source configured for cluster 'us'"


async def test_a_narrowed_read_is_withheld_rather_than_answered_with_the_wide_number():
    """The reader cannot narrow by an arbitrary dimension yet, and says so instead of lying."""
    stub = StubMetrics(answer(**{"inference.ttft": 3.0}))
    reader = reader_over(stub)
    reading = await reader.read(
        MetricQuery("inference.ttft", "deployment", "dep_1", "5m", dimensions={"input_bucket": "32k_64k"})
    )
    assert reading.value is None
    assert stub.calls == 0


async def test_a_subject_type_with_no_read_is_withheld():
    reader = reader_over(StubMetrics(answer()))
    reading = await reader.read(MetricQuery("inference.ttft", "gateway", "gw_1", "5m"))
    assert reading.value is None
    assert "gateway" in (reading.withheld or "")


def repo() -> AlertThresholdRepository:
    return AlertThresholdRepository(db=None)


def sql(query) -> str:
    return str(query)


def test_thresholds_are_listed_oldest_first():
    assert "ORDER BY ops_alert_threshold.seq ASC" in sql(repo().list_query(limit=10))


def test_the_filters_compile_into_one_statement():
    text = sql(repo().list_query(limit=10, rule=TTFT, subject_type="deployment", subject="dep_1", enabled=True))
    assert "ops_alert_threshold.rule = " in text
    assert "ops_alert_threshold.subject_type = " in text
    assert "ops_alert_threshold.subject_uid = " in text
    assert "ops_alert_threshold.enabled IS " in text


def test_the_fleet_default_filter_is_a_null_test_and_not_a_sentinel():
    assert "ops_alert_threshold.subject_uid IS NULL" in sql(repo().list_query(limit=10, fleet_default=True))
    assert "ops_alert_threshold.subject_uid IS NOT NULL" in sql(repo().list_query(limit=10, fleet_default=False))


def service() -> AlertThresholdService:
    """The session is never reached by the validation paths under test."""
    return AlertThresholdService(db=None)


async def test_a_row_may_not_name_a_rule_nothing_evaluates():
    with pytest.raises(Invalid) as error:
        await service().create(AlertThresholdCreateRequest(rule="deployment.vibes", value=1.0))
    assert TTFT in error.value.detail["allowed"]


async def test_a_rule_and_a_subject_type_that_disagree_are_refused():
    with pytest.raises(Invalid):
        await service().create(
            AlertThresholdCreateRequest(rule=TTFT, value=1.0, subject_type="node", subject_id="nod_1")
        )


async def test_a_subject_must_be_an_id():
    with pytest.raises(Invalid):
        await service().create(AlertThresholdCreateRequest(rule=TTFT, value=1.0, subject_id="gemma-us"))


async def test_a_lookback_has_to_be_a_window():
    for bad in ("ten minutes", "10", "0m", "1w", ""):
        with pytest.raises(Invalid):
            await service().create(AlertThresholdCreateRequest(rule=TTFT, value=1.0, lookback=bad))


async def test_an_unknown_severity_is_refused():
    with pytest.raises(Invalid):
        await service().create(AlertThresholdCreateRequest(rule=TTFT, value=1.0, severity="apocalyptic"))


async def test_dimensions_are_flat_and_stringly_typed():
    with pytest.raises(Invalid):
        await service().create(AlertThresholdCreateRequest(rule=TTFT, value=1.0, dimensions={"bucket": 4}))


async def test_a_negative_debounce_is_refused():
    with pytest.raises(Invalid):
        await service().create(AlertThresholdCreateRequest(rule=TTFT, value=1.0, for_seconds=-1))


def test_the_update_request_cannot_move_a_row_to_another_rule_or_subject():
    """Those three are the row's identity under `uq_ops_alert_threshold_scope`."""
    fields = set(AlertThresholdUpdateRequest.__struct_fields__)
    assert not fields & {"rule", "subject_type", "subject_id"}
    assert "value" in fields


def row(**kwargs) -> AlertThreshold:
    defaults = {
        "uid": new_uid(ALERT_THRESHOLD),
        "rule": TTFT,
        "subject_type": "deployment",
        "subject_uid": None,
        "dimensions": {},
        "value": Decimal("2.500000"),
        "objective": None,
        "lookback": None,
        "for_seconds": None,
        "severity": None,
        "enabled": True,
        "reason": "clause 4.2 of the acceptance standard",
        "created_at": dt.datetime(2026, 8, 25, tzinfo=dt.UTC),
        "updated_at": None,
    }
    return AlertThreshold(**{**defaults, **kwargs})


def test_schema_carries_rule_metric_and_comparison():
    answer = service().schema(row())
    assert answer.metric == "inference.ttft"
    assert answer.comparison == ABOVE
    assert answer.subject.type == "deployment"
    assert answer.subject.id is None


def test_a_numeric_column_reaches_the_wire_as_a_number():
    """`Numeric` is `Decimal` at runtime and msgspec will not encode one."""
    encoded = msgspec.json.encode(service().schema(row()))
    assert b'"value":2.5' in encoded


def test_what_was_left_unpinned_is_returned_unpinned():
    answer = service().schema(row())
    assert answer.lookback is None
    assert answer.severity is None
    assert answer.for_seconds is None


def test_the_catalogue_is_derived_from_the_rules_and_carries_the_unit():
    entries = {entry.name: entry for entry in catalogue()}
    assert entries.keys() == rules.METRIC_RULES.keys()
    assert entries[TTFT].unit == BY_NAME["inference.ttft"].unit
    assert entries[TTFT].comparison == ABOVE
    assert entries[SPEED].comparison == BELOW


def test_the_catalogue_answers_msgspec():
    assert msgspec.json.encode(catalogue())


def test_the_controllers_are_mounted_where_the_contract_says():
    assert AlertThresholdRoutes.path == "/alert-thresholds"
    assert AlertRuleRoutes.path == "/alert-rules"


def test_the_schema_survives_a_round_trip_through_the_wire():
    encoded = msgspec.json.encode(service().schema(row(subject_uid="dep_01j9x2aaaaaaaaaaaaaaaaaaaa")))
    decoded = msgspec.json.decode(encoded, type=AlertThresholdSchema)
    assert decoded.subject.id == "dep_01j9x2aaaaaaaaaaaaaaaaaaaa"
    assert decoded.rule == TTFT
    assert decoded.reason


def test_the_rule_catalogue_answers_over_http():
    """No session anywhere in the path: the vocabulary is code, so listing it reads nothing."""
    context = RequestContext(principal=AuthManager.admin_principal(), db=None)
    handlers = [AlertRuleRoutes]
    dependencies = {"context": Provide(lambda: context, sync_to_thread=False)}
    with create_test_client(route_handlers=handlers, dependencies=dependencies) as client:
        response = client.get("/alert-rules")
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == len(rules.METRIC_RULES)
        assert {entry["name"] for entry in body["items"]} == set(rules.METRIC_RULES)
        assert all(entry["metric"] in BY_NAME for entry in body["items"])
