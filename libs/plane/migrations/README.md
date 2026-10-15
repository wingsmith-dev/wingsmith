Plane database migrations
=========================

Alembic, async environment. The database URL comes from `HANGAR_DATABASE_URL`
(app.config.settings), never from alembic.ini. The version table is `_version`.
Postgres 15 or later is required: several constraints use NULLS NOT DISTINCT.

The chain starts at `272245edb7d9` (initial schema) and is linear; there is one
head. Files are named `<epoch>_<revision>.py`, and the epoch prefix only orders
them in a listing; `down_revision` is what defines the chain.


Running them
------------

From a checkout:

    cd libs/plane
    uv run alembic upgrade head
    uv run alembic current
    uv run alembic check          # fails if the models and the schema disagree

From the plane image, the `migrate` mode passes its arguments to alembic:

    docker run --rm -e HANGAR_DATABASE_URL=... <plane image> migrate upgrade head

`just dev-up` runs the same thing as the compose `migrate` service before api
and hub start.

An online run takes a Postgres advisory lock (`pg_advisory_xact_lock`) for its
whole transaction, so two runs started together (two replicas, a retried Job)
apply each revision once: the second waits, then finds nothing to do. All
revisions in one run share one transaction, so a failure leaves the database at
the revision it started from.


Upgrading a running plane
-------------------------

1. Back up the database.
2. Run `migrate upgrade head` with the NEW image, to completion.
3. Then roll out api, hub and reactor on the new image.

Running new code against the old schema is not supported, and neither is old
code against the new schema in general: some existing revisions rename or drop
columns in place (`f1a7c94e2b68` renames `engine_version`/`engine_image`,
`e4b9d2a15c73` drops the old model-name columns). Between steps 2 and 3, old
api and hub pods may fail requests that touch those columns. Plan for a short
window where the plane API errors, or scale api and hub to zero during step 2.

Revisions that rewrite tables hold an exclusive lock for as long as the copy
takes. `b6f96e7c388b` (seq becomes bigint) rewrites every table, so on a large
`ops_event` expect it to take minutes.

Some revisions refuse to run rather than guess:

- `e5b2c73a9d18` lists workloads that have no deployment and stops. Give them a
  deployment, or delete them deliberately with
  `alembic -x drop_orphan_workloads=true upgrade head`.
- `730d51320227` lists duplicate fleet-wide quotas or credentials and stops.
  Delete all but one of each before upgrading.

One revision deletes rows without asking: `9d05d968be9c` removes policy
attachments whose tenant, project, cluster, endpoint or workload no longer
exists. They point at nothing, and from that revision on the database deletes
an attachment together with its subject.


Downgrades
----------

Every revision has a downgrade, and `tests/test_migrations.py` runs
upgrade head -> downgrade base -> upgrade head and checks the result against
the models. Downgrades restore the schema, not always the data: where an upgrade
discarded information (for example `a7c31f8e64b2`, which folds recipes into
workloads), the downgrade rebuilds the old shape from what is left and says so in
its docstring. Restore from the backup taken in step 1 if you need the data back.


Writing a migration
-------------------

    uv run alembic revision --autogenerate -m "what changes, as a sentence"

Then read what autogenerate wrote; it is a draft. Rules for new revisions:

- Expand, then contract. A release adds columns, tables and indexes in a shape
  the previous release's code tolerates (nullable, or with a server default),
  and backfills. Code stops reading the old shape in that same release. Only a
  later release drops or renames what is no longer read. Never rename or drop a
  column the running release still reads.
- Do not import application code (`app.*`). A migration runs against the schema
  as it was at that point in history, and live code changes under it. Copy what
  you need into the revision and freeze it.
- No deployment-specific data: no cluster names, ids or hosts. A data change
  must be correct for every database the revision will ever run on.
- Refuse rather than delete. If existing rows cannot be carried over, raise with
  the offending ids, and offer an explicit `-x` flag for the destructive path.
- Write a downgrade that restores the previous schema exactly (indexes,
  constraint names, identity options), and keep `alembic check` clean: the
  models and the migrations must agree.
- Run the database suite before committing: `just test-db`.
