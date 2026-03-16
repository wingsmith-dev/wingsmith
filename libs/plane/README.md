# plane

Python control plane. Three concerns, deliberately kept apart:

- **Registry / intent** — CRUD over declared objects. Should stay boring.
- **Placement** — which cluster a workload goes to. A pure function over observed state, so it is
  testable without a cluster.
- **Ingestion** — writes arriving from the hub. High frequency, idempotent, last-writer-wins per
  cluster.

Runs as two processes sharing one package and one database: the HTTP API, and the **hub** that
terminates the long-lived bidirectional operator streams. They are separate because they have
different lifecycles and different failure modes — the API is request/response, the hub holds N
persistent sessions with resumption and backpressure.

Nothing here yet.
