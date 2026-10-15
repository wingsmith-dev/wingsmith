"""Domain modules.

One package per object in the model. Everything that is not a domain object —
the database, configuration, logging, the HTTP application, the hub — lives
outside this package, because those are machinery and these are the product.

Most modules have the same five files:

    <name>/routes.py       Controller class, `PascalCase + Routes`; thin, no queries
    <name>/schemas.py      msgspec request/response structs
    <name>/services.py     business logic, `PascalCase + Service`, owns the commit
    <name>/repository.py   database access, `PascalCase + Repository`, flushes only
    <name>/depends.py      service generator wired into the controller

The layout is the default, not a law, and the exceptions are deliberate:

- `metrics` has no repository. Its job is querying the metrics store, and the
  SQL it issues only resolves what a query is about (the cluster, node, pool,
  device, deployment or endpoint a reference names, and the labels to match
  them by), so those reads live in its service beside the query they scope.
- `tenants` and `serving` issue a handful of reads from their services where a
  read is the whole of the operation and a repository method would only rename it.
- `placement` keeps its pure decision code (`engine`, `snapshot`, `decision`)
  beside the usual files, and that code never touches a session.
- Larger modules add files for their own concerns (`benchmarks/harnesses.py`,
  `artifacts/resolve.py`, `clusters/enrolment.py`).

What does hold everywhere: a service owns its transaction, a repository never
commits, and only `app/ingestion` writes observed state. The declared/observed
split from `libs/packages/contract/API.md` means a module whose object is
observed (nodes, devices, topology domains) is read-only, and its service
exposes no create or update at all.
"""
