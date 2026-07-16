"""What each serving engine's own telemetry calls the numbers a run is judged on.

This is `hgrbench.metrics` pointed at the other side of the wire. That module maps a
load generator's export to shared field ids; this one maps a serving engine's
Prometheus scrape to shared series ids, in the same style and for the same reason: a
scrape is stored verbatim, normalisation happens only through an explicit per-engine
map, and a name that does not resolve is a **named miss** rather than a silent zero.

The rule this package exists to enforce is that the tool holds no numbers. Every
threshold a benchmark is judged against is either read from the deployment or measured
at bring-up. Capacity is the clearest case: vLLM publishes `kv_cache_size_tokens` on
`vllm:cache_config_info`, so the KV pool of the fleet actually under test is a reading,
not a constant. A constant goes stale silently -- the figure this project carried for a
month, 3,745,349, is a pre-expert-parallel generation's, and every generation since has
reported 3,617,511.

Reading capacity rather than deriving it also makes the attention geometry irrelevant.
Whether KV is replicated across tensor-parallel ranks (MLA, as in the Kimi and DeepSeek
families) or sharded across them (GQA, as in most others) changes what a worker's pool
is worth -- and changes it in a way the engine has already accounted for by the time it
publishes the number. A tool that reads it needs to know nothing about the model.

Adding an engine is one `Engine` entry. Nothing outside this module names a metric.
"""

from __future__ import annotations

from typing import NamedTuple


COUNTER = "counter"
GAUGE = "gauge"
INFO = "info"

KV_CAPACITY_TOKENS = "kv.capacity_tokens"
KV_BLOCKS = "kv.blocks"
KV_BLOCK_SIZE = "kv.block_size"
KV_USAGE = "kv.usage_frac"
CACHE_HITS = "cache.hits"
CACHE_QUERIES = "cache.queries"
REQUESTS_DONE = "requests.done"
REQUESTS_RUNNING = "requests.running"
REQUESTS_WAITING = "requests.waiting"
TOKENS_GENERATED = "tokens.generated"
TOKENS_PROMPT = "tokens.prompt"
PREEMPTIONS = "preemptions"
TTFT_SUM = "ttft.sum"
TTFT_COUNT = "ttft.count"
ITL_SUM = "itl.sum"
ITL_COUNT = "itl.count"
E2E_SUM = "e2e.sum"
E2E_COUNT = "e2e.count"
QUEUE_SUM = "queue.sum"
QUEUE_COUNT = "queue.count"
PREFILL_TIME_SUM = "prefill_time.sum"
ITER_TOKENS_SUM = "iteration_tokens.sum"
ITER_TOKENS_COUNT = "iteration_tokens.count"
XFER_SUM = "transfer.sum"
XFER_COUNT = "transfer.count"
XFER_FAILED = "transfer.failed"

# Series whose absence is normal rather than a defect. A transfer time exists only when
# the deployment moves KV between workers, and an aggregated one never does; an engine
# that does not disaggregate has no such metric at all. Anything not listed here is
# expected of every engine, and a miss on it is reported.
OPTIONAL = frozenset({XFER_SUM, XFER_COUNT, XFER_FAILED, PREFILL_TIME_SUM,
                      ITER_TOKENS_SUM, ITER_TOKENS_COUNT, REQUESTS_WAITING})


class Series(NamedTuple):
    """One number an engine publishes, and how to read it.

    `kind` decides what a reading means over a window. A COUNTER is differenced, a
    GAUGE is sampled, and an INFO metric carries its value in a label rather than in
    the sample -- which is where every engine hides its cache geometry.
    """

    id: str
    metric: str
    kind: str = COUNTER
    label: str = ""
    scale: float = 1.0


class Engine(NamedTuple):
    """One engine's map, valid over a half-open range of its versions.

    `max_version` is exclusive; `None` is open-ended. A scrape whose version falls
    outside every range is not read with a map written for another one -- the series
    are missing, which is the whole reason the range is recorded. Renaming metrics
    between minor versions is normal in every engine here.
    """

    engine: str
    min_version: str
    max_version: str | None
    series: tuple[Series, ...]


VLLM = Engine(
    engine="vllm",
    min_version="0.24.0",
    max_version=None,
    series=(
        Series(KV_CAPACITY_TOKENS, "vllm:cache_config_info", INFO, "kv_cache_size_tokens"),
        Series(KV_BLOCKS, "vllm:cache_config_info", INFO, "num_gpu_blocks"),
        Series(KV_BLOCK_SIZE, "vllm:cache_config_info", INFO, "block_size"),
        Series(KV_USAGE, "vllm:kv_cache_usage_perc", GAUGE),
        Series(CACHE_HITS, "vllm:prefix_cache_hits_total"),
        Series(CACHE_QUERIES, "vllm:prefix_cache_queries_total"),
        Series(REQUESTS_DONE, "vllm:request_success_total"),
        Series(REQUESTS_RUNNING, "vllm:num_requests_running", GAUGE),
        Series(REQUESTS_WAITING, "vllm:num_requests_waiting", GAUGE),
        Series(TOKENS_GENERATED, "vllm:generation_tokens_total"),
        Series(TOKENS_PROMPT, "vllm:prompt_tokens_total"),
        Series(PREEMPTIONS, "vllm:num_preemptions_total"),
        Series(TTFT_SUM, "vllm:time_to_first_token_seconds_sum"),
        Series(TTFT_COUNT, "vllm:time_to_first_token_seconds_count"),
        Series(ITL_SUM, "vllm:inter_token_latency_seconds_sum"),
        Series(ITL_COUNT, "vllm:inter_token_latency_seconds_count"),
        Series(E2E_SUM, "vllm:e2e_request_latency_seconds_sum"),
        Series(E2E_COUNT, "vllm:e2e_request_latency_seconds_count"),
        Series(QUEUE_SUM, "vllm:request_queue_time_seconds_sum"),
        Series(QUEUE_COUNT, "vllm:request_queue_time_seconds_count"),
        Series(PREFILL_TIME_SUM, "vllm:request_prefill_time_seconds_sum"),
        Series(ITER_TOKENS_SUM, "vllm:iteration_tokens_total_sum"),
        Series(ITER_TOKENS_COUNT, "vllm:iteration_tokens_total_count"),
        Series(XFER_SUM, "vllm:nixl_xfer_time_seconds_sum"),
        Series(XFER_COUNT, "vllm:nixl_xfer_time_seconds_count"),
        Series(XFER_FAILED, "vllm:nixl_num_failed_transfers_total"),
    ),
)

# Declared and empty on purpose. An engine with no map reads as every series missing,
# which is the honest answer and says which map has to be written; the alternative --
# falling through to vLLM's names -- would report zeros for a running deployment.
SGLANG = Engine(engine="sglang", min_version="0", max_version=None, series=())
TRTLLM = Engine(engine="trtllm", min_version="0", max_version=None, series=())

ENGINES: tuple[Engine, ...] = (VLLM, SGLANG, TRTLLM)


def _version_key(v: str) -> tuple[int, ...]:
    out = []
    for part in v.split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def engine_for(name: str, version: str = "") -> Engine | None:
    """The map for this engine at this version, or None when none covers it."""
    for e in ENGINES:
        if e.engine != name:
            continue
        if not version:
            return e
        v = _version_key(version)
        if v < _version_key(e.min_version):
            continue
        if e.max_version and v >= _version_key(e.max_version):
            continue
        return e
    return None


def detect(metric_names: set[str]) -> Engine | None:
    """Which engine wrote this scrape, from the names in it.

    Every engine here prefixes its metrics with its own name, so detection is a prefix
    count rather than a guess. Detecting beats being told: the deployment is the
    authority on what it runs, and a mis-declared engine reads as a fleet that
    published nothing.
    """
    best, best_n = None, 0
    for e in ENGINES:
        prefix = f"{e.engine}:"
        n = sum(1 for m in metric_names if m.startswith(prefix))
        if n > best_n:
            best, best_n = e, n
    return best
