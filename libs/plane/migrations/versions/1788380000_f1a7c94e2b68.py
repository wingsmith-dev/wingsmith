"""a runtime version is not an engine version

Revision ID: f1a7c94e2b68
Revises: e4b9d2a15c73
Create Date: 2026-09-02 17:10:00.000000

`engine_version` never held a version of the engine. Its only consumer pasted it onto
`ENGINE_RUNTIME_REPOS[engine]` to make `nvcr.io/nvidia/ai-dynamo/vllm-runtime:<tag>`, and the tags
that repository publishes are Dynamo releases — `1.4.1`. vLLM's own versions are `0.28.0`. The two
count in different sequences and the field only ever accepted the second one.

The name invited exactly one mistake, and the console had already made it. The vLLM recipe corpus
publishes `min_vllm_version` per model, the deploy flow read it as the obvious answer to a field
called "engine version", and a model with a recipe was therefore seeded with `0.28.0` — resolving
to `vllm-runtime:0.28.0`, an address that has never existed. A model *without* a recipe fell
through to `latest` and served. Upstream evidence made the outcome worse, which is the tell that
the field was misnamed rather than misused.

So `engine_version` becomes `engine_runtime_version` and `engine_image` becomes
`engine_runtime_image`, on `demand_workload` and on `demand_replica`. `engine` stays: it names the
module the worker launches (`python3 -m dynamo.<engine>`) and the flag dialect `SERVING_FLAGS`
renders, both of which are the engine and neither of which is a version. `engine_args` stay engine
arguments. The recipe's `engine_version_min` keeps its name too, and only now reads correctly —
beside `engine_runtime_version` the pair says the thing that was being conflated: engine vLLM
>= 0.28.0, runtime 1.4.1.

The prefix is `engine_runtime_` rather than `runtime_` for two reasons. `supply_node` already
reports a `runtime_version`, which is the machine's container runtime (`containerd://1.7.27`), and
two unrelated `runtime_version` fields on one wire is the next confusion rather than the end of
this one. And the serving block carries two runtime images, not one: `frontend_image` is also a
Dynamo build, so a bare `runtime_image` would claim a singular that is not there.

**No digest moves.** `serving_digest` hashes a fixed list of keys, and `ops_benchmark_run`
.`serving_digest` names the configuration a measurement was taken against — a run whose digest
matches no workload has lost the only link back to what it measured. So `DIGEST_FIELDS` is now
`(hash key, attribute)` pairs and the hash keys stay `engine_version` and `engine_image` for ever.
Renaming a column is a statement about how the code reads. It is not a statement that the
configuration changed and must not be recorded as one, which is why this migration rewrites no
digest and touches no benchmark row.

Column renames only — no type change, no default, no data rewrite — so this is metadata on both
tables and does not rewrite the heap.
"""

from typing import Union, Sequence

from alembic import op


revision: str = "f1a7c94e2b68"
down_revision: Union[str, None] = "e4b9d2a15c73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


RENAMES = (
    ("demand_workload", "engine_version", "engine_runtime_version"),
    ("demand_workload", "engine_image", "engine_runtime_image"),
    ("demand_replica", "engine_version", "engine_runtime_version"),
)


def upgrade() -> None:
    for table, old, new in RENAMES:
        op.alter_column(table, old, new_column_name=new)


def downgrade() -> None:
    for table, old, new in RENAMES:
        op.alter_column(table, new, new_column_name=old)
