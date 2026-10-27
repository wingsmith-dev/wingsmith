"""Render tests for the cluster-setup chart.

Run with:

    uv run --no-project --with pyyaml --with pytest pytest libs/chart/cluster-setup/tests

Each test renders the chart offline with `helm template` and asserts on the
objects it produces, or on the refusal it prints. The subcharts are not vendored,
so the chart is copied and its `dependencies:` stripped first, the same way
`just chart-lint` renders it; nothing the subcharts own is asserted here.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tarfile

import pytest
import yaml

CHART = pathlib.Path(__file__).resolve().parents[1]

# The blanks an operator must fill. Every render supplies them unless a test is
# about one of them.
REQUIRED = {
    "cluster.id": "lint",
    "cluster.hubUrl": "wss://plane.example.com/v1/hub/sync",
    "enrolmentToken.value": "test-enrolment-token",
    "pools.labelKey": "example.com/pool",
    "pools.gpuValue": "gpu",
    "pools.cacheValue": "system",
    "modelCache.storageClass": "rwx",
}


@pytest.fixture(scope="session")
def chart(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    work = tmp_path_factory.mktemp("chart") / "cluster-setup"
    shutil.copytree(CHART, work, ignore=shutil.ignore_patterns("tests", "charts", "__pycache__"))
    notes_txt = (work / "templates" / "NOTES.txt").read_text()
    (work / "templates" / "_test_notes.tpl").write_text(
        '{{- define "test.notes" -}}\n' + notes_txt + "\n{{- end -}}\n")
    (work / "templates" / "zz-test-notes.yaml").write_text(
        'kind: RenderedNotes\ntext: {{ include "test.notes" . | quote }}\n')
    meta = work / "Chart.yaml"
    meta.write_text(meta.read_text().split("dependencies:")[0])
    (work / "Chart.lock").unlink(missing_ok=True)
    return work


def _args(values: dict[str, object], required: bool) -> list[str]:
    merged: dict[str, object] = dict(REQUIRED) if required else {}
    merged.update(values)
    args: list[str] = []
    for key, value in merged.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        args += ["--set", f"{key}={value}"]
    return args


def helm(chart: pathlib.Path, values: dict[str, object], required: bool = True,
         namespace: str = "hangar-system") -> subprocess.CompletedProcess[str]:
    cmd = ["helm", "template", "rel", str(chart), "-n", namespace, *_args(values, required)]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def render(chart: pathlib.Path, **values: object) -> list[dict]:
    result = helm(chart, values)
    assert result.returncode == 0, result.stderr
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def refusal(chart: pathlib.Path, **values: object) -> str:
    result = helm(chart, values)
    assert result.returncode != 0, f"rendered, but should have refused: {values}"
    return result.stderr


def notes(chart: pathlib.Path, **values: object) -> str:
    """NOTES.txt as rendered. `helm template` never renders it, so the fixture
    wraps it in a define and a test-only object that carries the text."""
    for d in render(chart, **values):
        if d.get("kind") == "RenderedNotes":
            return d["text"]
    raise AssertionError("NOTES.txt was not rendered")


def find(docs: list[dict], kind: str, name: str, namespace: str | None = None) -> dict | None:
    for d in docs:
        meta = d.get("metadata", {})
        if d.get("kind") == kind and meta.get("name") == name:
            if namespace is None or meta.get("namespace") == namespace:
                return d
    return None


def get(docs: list[dict], kind: str, name: str, namespace: str | None = None) -> dict:
    found = find(docs, kind, name, namespace)
    assert found is not None, f"no {kind} {name} rendered"
    return found


def container(deployment: dict, name: str) -> dict:
    for c in deployment["spec"]["template"]["spec"]["containers"]:
        if c["name"] == name:
            return c
    raise AssertionError(f"no container {name}")


def env(c: dict) -> dict[str, object]:
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in c.get("env", [])}


# ── packaging ────────────────────────────────────────────────────────────────


def test_package_keeps_vendored_subcharts_and_drops_stray_archives(chart, tmp_path):
    """`helm dependency build` leaves subcharts as charts/*.tgz. Ignoring every
    .tgz dropped them too, so a package could not be built from a vendored chart."""
    sub = tmp_path / "sub"
    subprocess.run(["helm", "create", str(sub)], check=True, capture_output=True)
    subprocess.run(["helm", "package", str(sub), "-d", str(chart / "charts")], check=True,
                   capture_output=True)
    (chart / "stray.tgz").write_bytes(b"")
    try:
        out = tmp_path / "out"
        subprocess.run(["helm", "package", str(chart), "-d", str(out)], check=True,
                       capture_output=True)
        (package,) = out.glob("*.tgz")
        names = tarfile.open(package).getnames()
        assert "cluster-setup/charts/sub/Chart.yaml" in names
        assert not any(n.endswith("stray.tgz") for n in names)
    finally:
        (chart / "stray.tgz").unlink()
        shutil.rmtree(chart / "charts")


# ── enrolment token ──────────────────────────────────────────────────────────

AGENT_ON = {
    "cluster.readOnly": False,
    "agent.enabled": True,
    "agent.gateway.address": "gateway.example.com:443",
    "frontend.mode": "per-model",
}
MONITORING_ON = {
    "monitoring.enabled": True,
    "cluster.region": "region-1",
    "monitoring.remoteWrite.url": "https://metrics.example.com/api/v1/write",
}


def _roll_keys(docs: list[dict]) -> dict[str, str | None]:
    out = {}
    for name in ("hangar-operator", "hangar-agent"):
        d = find(docs, "Deployment", name)
        if d is not None:
            out[name] = d["spec"]["template"]["metadata"].get("annotations", {}).get(
                "rollout/enrolment-token")
    return out


def test_token_annotation_is_not_a_digest_of_the_token(chart):
    import hashlib

    token = REQUIRED["enrolmentToken.value"]
    docs = render(chart, **AGENT_ON)
    keys = _roll_keys(docs)
    assert set(keys) == {"hangar-operator", "hangar-agent"}
    digests = {
        hashlib.sha256(token.encode()).hexdigest(),
        hashlib.sha256(f"enrolment-token:{token}".encode()).hexdigest(),
    }
    for key in keys.values():
        assert key
        assert not any(d.startswith(key) for d in digests)
    for d in docs:
        assert "checksum/enrolment-token" not in yaml.safe_dump(d)


def test_token_rotation_rolls_the_operator_and_agent(chart):
    before = _roll_keys(render(chart, **AGENT_ON))
    after = _roll_keys(render(chart, **AGENT_ON, **{"enrolmentToken.value": "rotated-token"}))
    again = _roll_keys(render(chart, **AGENT_ON))
    assert before == again
    for name in before:
        assert before[name] != after[name]


def test_existing_secret_renders_no_token_annotation(chart):
    docs = render(chart, **{"enrolmentToken.value": "", "enrolmentToken.existing": True})
    assert _roll_keys(docs) == {"hangar-operator": None}


def test_notes_never_point_at_a_fleet_token(chart):
    text = notes(chart, **{"enrolmentToken.value": "", "enrolmentToken.existing": True})
    assert "HANGAR_OPERATOR_TOKEN" not in text
    assert "/v1/clusters/lint/enrolment-token" in text
    assert "HANGAR_OPERATOR_TOKEN" not in (chart / "values.yaml").read_text()


def test_metrics_agent_mounts_only_the_password_key(chart):
    docs = render(chart, **MONITORING_ON)
    agent = get(docs, "Deployment", "hangar-metrics-agent")
    (volume,) = [v for v in agent["spec"]["template"]["spec"]["volumes"] if v["name"] == "remote-write"]
    assert volume["secret"]["items"] == [{"key": "enrolment-token", "path": "enrolment-token"}]


# ── workload namespace ───────────────────────────────────────────────────────


def test_uninstall_keeps_the_workload_namespace_and_model_cache(chart):
    docs = render(chart)
    ns = get(docs, "Namespace", "dynamo")
    pvc = get(docs, "PersistentVolumeClaim", "model-cache", "dynamo")
    for obj in (ns, pvc):
        assert obj["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/warn"] == "baseline"
    assert "pod-security.kubernetes.io/enforce" not in ns["metadata"]["labels"]


def test_workload_namespace_is_one_value_used_everywhere(chart):
    docs = render(
        chart,
        **{
            **AGENT_ON,
            **MONITORING_ON,
            "workloadNamespace.name": "models",
            "hfToken.create": True,
            "hfToken.value": "hf-test",
            "frontend.mode": "shared",
        },
    )
    text = yaml.safe_dump_all(docs)
    assert "dynamo\n" not in text.replace("job_name: dynamo\n", "")
    assert get(docs, "Namespace", "models")
    assert get(docs, "PersistentVolumeClaim", "model-cache", "models")
    assert get(docs, "Secret", "hf-token-secret", "models")
    assert get(docs, "Role", "hangar-agent", "models")
    assert get(docs, "DynamoGraphDeployment", "inference", "models")
    assert get(docs, "ConfigMap", "hangar-operator")["data"]["HANGAR_WORKLOAD_NAMESPACE"] == "models"
    assert get(docs, "ConfigMap", "hangar-agent")["data"]["HANGAR_WORKLOAD_NAMESPACE"] == "models"
    scrape = get(docs, "ConfigMap", "hangar-metrics-agent")["data"]["prometheus.yml"]
    assert "names: [\"dynamo\"]" not in scrape and "names: [\"models\"]" in scrape


def test_existing_workload_namespace_is_not_created(chart):
    docs = render(chart, **{"workloadNamespace.create": False})
    assert find(docs, "Namespace", "dynamo") is None
    assert get(docs, "PersistentVolumeClaim", "model-cache", "dynamo")


# ── operator RBAC ────────────────────────────────────────────────────────────

WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection"}


def _operator_rules(docs: list[dict]) -> dict[tuple[str, str | None], list[dict]]:
    """Every rule bound to the operator's ServiceAccount, keyed by role."""
    bound = set()
    for d in docs:
        if d["kind"] in ("RoleBinding", "ClusterRoleBinding") and any(
            s.get("name") == "hangar-operator" for s in d["subjects"]
        ):
            ns = d["metadata"].get("namespace")
            bound.add((d["roleRef"]["kind"], d["roleRef"]["name"], ns))
    out = {}
    for kind, name, ns in bound:
        role = get(docs, kind, name, ns)
        out[(kind + "/" + name, ns)] = role["rules"]
    return out


def _grants(rules, resource: str, verb: str) -> bool:
    return any(resource in r["resources"] and verb in r["verbs"] for r in rules)


def test_default_is_read_only_with_no_operations(chart):
    values = yaml.safe_load((chart / "values.yaml").read_text())
    assert values["cluster"]["readOnly"] is True
    assert values["cluster"]["allowedOperations"] == ""
    docs = render(chart)
    assert get(docs, "ConfigMap", "hangar-operator")["data"]["HANGAR_ALLOWED_OPERATIONS"] == ""


def test_read_only_renders_no_write_rbac_outside_the_release_namespace(chart):
    roles = _operator_rules(render(chart))
    for (role, ns), rules in roles.items():
        if ns == "hangar-system":
            continue
        for rule in rules:
            assert not WRITE_VERBS & set(rule["verbs"]), (role, rule)


def test_no_cluster_wide_write_and_leases_are_namespaced(chart):
    roles = _operator_rules(render(chart, **{"cluster.readOnly": False}))
    for (role, ns), rules in roles.items():
        if role.startswith("ClusterRole/"):
            for rule in rules:
                writes = WRITE_VERBS & set(rule["verbs"])
                assert not writes or set(rule["resources"]) <= {"nodes", "pods/eviction"}, rule
            assert not _grants(rules, "leases", "update")
            assert not _grants(rules, "jobs", "create")
    assert _grants(roles[("Role/hangar-operator", "hangar-system")], "leases", "update")
    assert _grants(roles[("Role/hangar-operator", "hangar-system")], "syncedresources", "update")


def test_managed_cluster_gets_reconcile_and_every_operation(chart):
    roles = _operator_rules(render(chart, **{"cluster.readOnly": False}))
    workloads = roles[("Role/hangar-operator", "dynamo")]
    assert _grants(workloads, "dynamographdeployments", "create")
    assert _grants(workloads, "jobs", "create")
    assert _grants(workloads, "pods/log", "get")
    nodes = roles[("ClusterRole/hangar-operator-nodes", None)]
    assert _grants(nodes, "nodes", "patch")
    assert _grants(nodes, "pods/eviction", "create")


@pytest.mark.parametrize(
    "allowed, jobs, nodes, eviction",
    [
        ("benchmark", True, False, False),
        ("cordon", False, True, False),
        ("drain, benchmark", True, True, True),
    ],
)
def test_read_only_operations_add_only_their_own_rbac(chart, allowed, jobs, nodes, eviction):
    docs = render(chart, **{"cluster.allowedOperations": allowed.replace(",", "\\,")})
    roles = _operator_rules(docs)
    workloads = roles.get(("Role/hangar-operator", "dynamo"), [])
    assert _grants(workloads, "jobs", "create") is jobs
    assert not _grants(workloads, "dynamographdeployments", "create")
    node_rules = roles.get(("ClusterRole/hangar-operator-nodes", None), [])
    assert _grants(node_rules, "nodes", "patch") is nodes
    assert _grants(node_rules, "pods/eviction", "create") is eviction
    configured = get(docs, "ConfigMap", "hangar-operator")["data"]["HANGAR_ALLOWED_OPERATIONS"]
    assert configured == ",".join(k.strip() for k in allowed.split(","))


# What the operator's manager reads through its cache, which spans the release
# and workload namespaces (libs/operator/internal/reconcile/manager.go).
# controller-runtime opens an informer per type in each cached namespace, so
# each of these needs get/list/watch in both or the cache never syncs and the
# manager exits. The store reads SyncedResources read-only or not; the
# reconciler watches the rest only on a managed cluster.
CACHED_ALWAYS = ["syncedresources"]
CACHED_RECONCILE = ["jobs", "dynamographdeployments", "pods", "deployments", "statefulsets"]


def _can_watch(roles, resource: str, namespace: str) -> bool:
    for (role, ns), rules in roles.items():
        if ns not in (None, namespace):
            continue
        if all(_grants(rules, resource, verb) for verb in ("get", "list", "watch")):
            return True
    return False


@pytest.mark.parametrize(
    "values, cached",
    [
        ({}, CACHED_ALWAYS),
        ({"cluster.allowedOperations": "benchmark"}, CACHED_ALWAYS),
        ({"cluster.readOnly": False}, CACHED_ALWAYS + CACHED_RECONCILE),
        ({"cluster.readOnly": False, "workloadNamespace.name": "hangar-system"},
         CACHED_ALWAYS + CACHED_RECONCILE),
    ],
)
def test_operator_can_watch_every_cached_type_in_both_namespaces(chart, values, cached):
    docs = render(chart, **values)
    roles = _operator_rules(docs)
    workloads = values.get("workloadNamespace.name", "dynamo")
    for resource in cached:
        for ns in ("hangar-system", workloads):
            assert _can_watch(roles, resource, ns), (resource, ns)


def test_shared_namespace_renders_one_role_and_no_namespace(chart):
    docs = render(chart, **{"cluster.readOnly": False, "workloadNamespace.name": "hangar-system"})
    keys = [(d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"])
            for d in docs if "metadata" in d]
    assert len(keys) == len(set(keys)), [k for k in keys if keys.count(k) > 1]
    assert not [d for d in docs if d["kind"] == "Namespace"]
    roles = _operator_rules(docs)
    own = roles[("Role/hangar-operator", "hangar-system")]
    assert _grants(own, "leases", "update")
    assert _grants(own, "dynamographdeployments", "create")


def test_unknown_operation_is_refused(chart):
    assert "cordon, drain and benchmark" in refusal(chart, **{"cluster.allowedOperations": "reboot"})


def test_rbac_names_the_hangar_dev_group(chart):
    roles = _operator_rules(render(chart))
    groups = {g for rules in roles.values() for r in rules for g in r["apiGroups"]}
    assert "hangar.dev" in groups
    assert not any("placeholder" in g or g == "sync.hangar.dev" for g in groups)


# ── versioning ───────────────────────────────────────────────────────────────


def test_app_version_is_the_chart_version_and_images_default_to_it(chart):
    import re

    meta = yaml.safe_load((CHART / "Chart.yaml").read_text())
    assert re.fullmatch(r"\d+\.\d+\.\d+", meta["version"])
    assert meta["appVersion"] == meta["version"]
    for dep in meta["dependencies"]:
        assert re.fullmatch(r"\d+\.\d+\.\d+", dep["version"]), dep
    docs = render(chart, **AGENT_ON)
    for name, c in (("hangar-operator", "operator"), ("hangar-agent", "agent")):
        spec = container(get(docs, "Deployment", name), c)
        assert spec["image"].endswith(":" + meta["version"])
        assert spec["imagePullPolicy"] == "IfNotPresent"


def test_lock_matches_the_pinned_dependencies():
    meta = yaml.safe_load((CHART / "Chart.yaml").read_text())
    lock = yaml.safe_load((CHART / "Chart.lock").read_text())
    pinned = {d["name"]: d["version"] for d in meta["dependencies"]}
    assert {d["name"]: d["version"] for d in lock["dependencies"]} == pinned


def test_changelog_has_an_entry_for_this_version():
    meta = yaml.safe_load((CHART / "Chart.yaml").read_text())
    assert f"## {meta['version']}\n" in (CHART / "CHANGELOG.md").read_text()


# ── hub and tunnel TLS ───────────────────────────────────────────────────────

PEM = "-----BEGIN CERTIFICATE-----\\nMIIB\\n-----END CERTIFICATE-----\\n"


def test_plaintext_hub_is_refused_by_default(chart):
    err = refusal(chart, **{"cluster.hubUrl": "ws://plane.local/v1/hub/sync"})
    assert "hub.allowInsecure" in err
    assert "wss://" in refusal(chart, **{"cluster.hubUrl": "https://plane.example.com"})


def test_allow_insecure_accepts_ws_and_tells_the_operator(chart):
    docs = render(chart, **{"cluster.hubUrl": "ws://plane.local/v1/hub/sync", "hub.allowInsecure": True})
    data = get(docs, "ConfigMap", "hangar-operator")["data"]
    assert data["HANGAR_HUB_ALLOW_INSECURE"] == "true"
    assert "HANGAR_HUB_ALLOW_INSECURE" not in get(render(chart), "ConfigMap", "hangar-operator")["data"]


def test_hub_ca_bundle_is_mounted_into_the_operator(chart):
    docs = render(chart, **{"hub.caBundle": PEM})
    assert get(docs, "ConfigMap", "hangar-operator")["data"]["HANGAR_HUB_CA_FILE"] == "/etc/hangar/hub-ca/ca.crt"
    assert get(docs, "ConfigMap", "hangar-hub-ca")["data"]["ca.crt"].startswith("-----BEGIN")
    op = get(docs, "Deployment", "hangar-operator")
    assert container(op, "operator")["volumeMounts"][0]["mountPath"] == "/etc/hangar/hub-ca"
    assert op["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"] == "hangar-hub-ca"


def test_tunnel_server_name_and_ca_reach_the_agent(chart):
    docs = render(chart, **{**AGENT_ON, "agent.gateway.serverName": "gw.internal",
                            "agent.gateway.caBundle": PEM})
    data = get(docs, "ConfigMap", "hangar-agent")["data"]
    assert data["HANGAR_TUNNEL_SERVER_NAME"] == "gw.internal"
    assert data["HANGAR_TUNNEL_CA_FILE"] == "/etc/hangar/tunnel-ca/ca.crt"
    assert get(docs, "ConfigMap", "hangar-tunnel-ca")
    agent = get(docs, "Deployment", "hangar-agent")
    assert container(agent, "agent")["volumeMounts"][0]["mountPath"] == "/etc/hangar/tunnel-ca"


# ── pod hardening ────────────────────────────────────────────────────────────


def test_metrics_agent_container_is_hardened(chart):
    agent = get(render(chart, **MONITORING_ON), "Deployment", "hangar-metrics-agent")
    pod = agent["spec"]["template"]["spec"]
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    sc = container(agent, "prometheus")["securityContext"]
    assert sc == {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                  "capabilities": {"drop": ["ALL"]}}


def test_shared_frontend_container_is_hardened(chart):
    docs = render(chart, **{"frontend.mode": "shared"})
    dgd = get(docs, "DynamoGraphDeployment", "inference")
    spec = dgd["spec"]["components"][0]["podTemplate"]["spec"]
    assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    sc = spec["containers"][0]["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"] == {"drop": ["ALL"]}


def test_nats_has_resources():
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    resources = values["dynamo-platform"]["nats"]["container"]["merge"]["resources"]
    assert resources["requests"] and resources["limits"]["memory"]


# ── network policy and the serving route ────────────────────────────────────


def _policies(docs):
    return {(d["metadata"]["namespace"], d["metadata"]["name"]): d
            for d in docs if d["kind"] == "NetworkPolicy"}


def test_network_policies_are_on_by_default(chart):
    policies = _policies(render(chart, **AGENT_ON))
    assert set(policies) == {
        ("hangar-system", "hangar-operator"),
        ("hangar-system", "hangar-agent"),
        ("dynamo", "hangar-inference-frontend"),
        ("hangar-system", "hangar-nats"),
    }
    for p in policies.values():
        assert p["spec"]["policyTypes"] == ["Ingress"]
    frontend = policies[("dynamo", "hangar-inference-frontend")]["spec"]
    assert frontend["podSelector"] == {"matchLabels": {"nvidia.com/dynamo-component": "Frontend"}}
    peers = frontend["ingress"][0]["from"]
    assert {"podSelector": {}} in peers
    release = [p for p in peers if "namespaceSelector" in p][0]
    assert release["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "hangar-system"}
    nats = policies[("hangar-system", "hangar-nats")]["spec"]
    assert nats["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "nats"
    assert {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "dynamo"}}} in nats["ingress"][0]["from"]
    operator = policies[("hangar-system", "hangar-operator")]["spec"]
    assert "from" not in operator["ingress"][0]
    assert {"port": "health", "protocol": "TCP"} in operator["ingress"][0]["ports"]


def test_network_policies_can_be_turned_off(chart):
    assert not _policies(render(chart, **{"networkPolicy.enabled": False}))


SERVING = {
    "serving.enabled": True,
    "serving.host": "models.example.com",
    "serving.clusterIssuer": "issuer",
    "serving.backend.service": "frontend",
}


def test_serving_route_without_auth_middleware_is_refused(chart):
    err = refusal(chart, **{**SERVING, "networkPolicy.enabled": False})
    assert "serving.middlewares" in err


def test_serving_route_carries_its_middleware_and_needs_a_policy_peer(chart):
    values = {**SERVING, "serving.middlewares[0].name": "auth",
              "serving.middlewares[0].namespace": "traefik"}
    assert "networkPolicy.frontendFrom" in refusal(chart, **values)
    docs = render(chart, **values, **{
        "networkPolicy.frontendFrom[0].namespaceSelector.matchLabels.team": "edge"})
    route = get(docs, "IngressRoute", "hangar-serving", "dynamo")
    assert route["spec"]["routes"][0]["middlewares"] == [{"name": "auth", "namespace": "traefik"}]
    peers = _policies(docs)[("dynamo", "hangar-inference-frontend")]["spec"]["ingress"][0]["from"]
    assert {"namespaceSelector": {"matchLabels": {"team": "edge"}}} in peers
    render(chart, **SERVING, **{"serving.allowUnauthenticated": True, "networkPolicy.enabled": False})


# ── operator metrics ─────────────────────────────────────────────────────────


def test_operator_metrics_are_on_and_reachable(chart):
    docs = render(chart)
    assert get(docs, "ConfigMap", "hangar-operator")["data"]["HANGAR_METRICS_ADDR"] == ":8080"
    ports = container(get(docs, "Deployment", "hangar-operator"), "operator")["ports"]
    assert {"name": "metrics", "containerPort": 8080} in ports
    policy = _policies(docs)[("hangar-system", "hangar-operator")]["spec"]
    assert {"port": "metrics", "protocol": "TCP"} in policy["ingress"][0]["ports"]
    assert find(docs, "PodMonitor", "hangar-operator") is None


def test_operator_metrics_off_and_pod_monitor(chart):
    off = render(chart, **{"operator.metrics.enabled": False})
    assert get(off, "ConfigMap", "hangar-operator")["data"]["HANGAR_METRICS_ADDR"] == "0"
    docs = render(chart, **{"operator.metrics.podMonitor.enabled": True,
                            "operator.metrics.podMonitor.labels.release": "kps"})
    monitor = get(docs, "PodMonitor", "hangar-operator", "hangar-system")
    assert monitor["metadata"]["labels"]["release"] == "kps"
    assert monitor["spec"]["podMetricsEndpoints"][0]["port"] == "metrics"


# ── docs and values hygiene ──────────────────────────────────────────────────


def test_shipped_files_name_no_removed_paths_or_deployment_history():
    import re

    # Internal names are not listed here, since listing them would publish them: the prose
    # guard (scripts/check-prose.sh) checks every tracked file against the private list.
    banned = re.compile(r"infra/|HANGAR_OPERATOR_TOKEN|placeholder\.hangar|"
                        r"sync\.hangar\.dev|helm registry login",
                        re.IGNORECASE)
    for path in CHART.rglob("*"):
        if path.is_file() and "tests" not in path.parts and path.suffix in (
                ".yaml", ".yml", ".tpl", ".txt", ".md", ".lock"):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                assert not banned.search(line), f"{path.relative_to(CHART)}:{n}: {line.strip()}"


def test_frontend_preamble_switch_reaches_both_modes(chart):
    shared = render(chart, **{"frontend.mode": "shared", "frontend.anthropicApi.stripPreamble": False})
    env_vars = {e["name"]: e["value"] for e in get(shared, "DynamoGraphDeployment", "inference")
                ["spec"]["components"][0]["podTemplate"]["spec"]["containers"][0]["env"]}
    assert env_vars["DYN_STRIP_ANTHROPIC_PREAMBLE"] == "false"
    data = get(shared, "ConfigMap", "hangar-operator")["data"]
    assert data["HANGAR_FRONTEND_ANTHROPIC_STRIP_PREAMBLE"] == "false"


def test_image_digest_pins_the_build(chart):
    digest = "sha256:" + "a" * 64
    docs = render(chart, **{**AGENT_ON, "operator.image.digest": digest,
                            "agent.image.repository": "registry.example.com/agent",
                            "agent.image.tag": "custom"})
    assert container(get(docs, "Deployment", "hangar-operator"), "operator")["image"] == \
        "ghcr.io/hangar-dev/operator@" + digest
    assert container(get(docs, "Deployment", "hangar-agent"), "agent")["image"] == \
        "registry.example.com/agent:custom"
    assert "sha256:" in refusal(chart, **{"operator.image.digest": "abc"})


def test_image_allowlists_reach_the_operator(chart):
    data = get(render(chart), "ConfigMap", "hangar-operator")["data"]
    assert "HANGAR_ALLOWED_IMAGE_PREFIXES" not in data
    assert "HANGAR_BENCHMARK_IMAGES" not in data
    docs = render(chart, **{
        "operator.images.allowedPrefixes": "{nvcr.io/nvidia/ai-dynamo,registry.example.com/}",
        "operator.benchmark.images": "{registry.example.com/aiperf:1.0}",
    })
    data = get(docs, "ConfigMap", "hangar-operator")["data"]
    assert data["HANGAR_ALLOWED_IMAGE_PREFIXES"] == "nvcr.io/nvidia/ai-dynamo,registry.example.com/"
    assert data["HANGAR_BENCHMARK_IMAGES"] == "registry.example.com/aiperf:1.0"


@pytest.mark.parametrize("key,value,where", [
    ("cluster.readOnly", '"false"', "/cluster/readOnly"),
    ("frontend.mode", "sharded", "/frontend/mode"),
    ("operator.image.pullPolicy", "Sometimes", "/operator/image/pullPolicy"),
    ("cluster.logFormat", "xml", "/cluster/logFormat"),
])
def test_schema_refuses_a_wrong_type_before_rendering(chart, key, value, where):
    # A string "false" is truthy to the templates, so readOnly: "false" would
    # grant read-only RBAC to an operator that reconciles.
    cmd = ["helm", "template", "rel", str(chart), "-n", "hangar-system", *_args({}, True)]
    cmd += ["--set-json", f"{key}={value}" if value.startswith('"') else f'{key}="{value}"']
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert where in result.stderr


def test_schema_accepts_an_unquoted_numeric_cluster_name(chart):
    render(chart, **{"cluster.id": 123, "cluster.region": 7})
