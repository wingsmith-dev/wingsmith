"""Placement — which cluster a workload goes to.

Deliberately a pure function over observed state: it takes a workload's
requirements plus a snapshot of capacity and returns a decision. It performs no
I/O, opens no session, and imports nothing from `app.db` or `app.api`, so it is
testable without a cluster and without a database.

Callers read the snapshot, call in, and persist the result themselves.
"""
