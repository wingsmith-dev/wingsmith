# 0012: The plane is the source of the metrics write htpasswd

**Status:** accepted · 2026-08-27

**Extends:** [0011](0011-a-cluster-carries-its-own-metrics-label.md), which moved the *read* side of a
cluster's metrics identity onto the registry row. This does the same for the *write* side, and
finishes the job 0011 started: enrolling a cluster touches nothing outside the console.

## Context

0011 removed the config edit that enrolling a cluster used to need. One manual step survived it, and
it was the worse of the two.

The fleet remote-writes into one Prometheus in the plane's own cluster. Its write ingress is fronted
by a Traefik `basicAuth` middleware reading an htpasswd file from a Secret. That Secret was
populated from a secret manager outside the product, and adding a cluster meant a human generating a
bcrypt line, editing a value in that secret manager, and waiting for the sync to refresh. A file
maintained that way drifts: it ends up holding lines for clusters that no longer exist and missing
lines for clusters that do.

That step fails in the way infrastructure steps outside the product always fail: silently and on the
cluster's side. An agent with a wrong credential gets a 401 on every send, logs it in its own pod,
and reports nothing to anyone; the console shows an enrolled, connected, healthy cluster with empty
charts. The signal is on the wrong side of the fence from the person who would act on it.

The plane already holds both halves of the answer. It mints the credential (the enrolment token)
and it knows the fleet. The htpasswd file is a pure function of those two facts, which makes it a
reconciliation the plane can own instead of a document a human maintains.

## Decision

1. **One credential, two verifiers.** `issue_enrolment_token` stores the sha256 the hub looks the
   token up by and compares in constant time (`Cluster.token_hash`) *and* a bcrypt hash of the same
   plaintext (`Cluster.remote_write_hash`). Neither hash can serve both jobs: the hub needs an
   indexed lookup, which bcrypt forbids, and Traefik can only check a crypt-style hash. Both are
   written in the same act, so rotation stays one act: a cluster that rotates its token keeps its
   telemetry.
2. **A reactor pass renders the file and patches the Secret.**
   [`libs/plane/app/reactor/remote_write.py`](../../libs/plane/app/reactor/remote_write.py) selects
   every cluster with a hash, renders `<cluster-name>:<bcrypt-hash>` one per line sorted by name, and
   patches the Secret's `users` key. It reads and compares first, so the unchanged case (every
   tick but the few after a register or a rotate) writes nothing.
3. **It lives in the reactor because of the lease.** The reactor already elects one holder
   fleet-wide. This is the only thing in the plane with an external side effect that must have
   exactly one writer, and the machinery for that already existed.
4. **The plane talks to the Kubernetes API directly, over `httpx`.** Two verbs on one resource kind
   ([`libs/plane/app/integrations/kubernetes.py`](../../libs/plane/app/integrations/kubernetes.py))
   and a `SecretStore` protocol so the reconciler is testable without a cluster. No
   `kubernetes-asyncio`: a generated client's model hierarchy, watch implementation and discovery
   cache are all cost with no use here.
5. **It degrades to nothing.** Gated on `WINGSMITH_REMOTE_WRITE_AUTH_ENABLED`, and additionally inert
   with no in-cluster service account. Both cases log once and return. A plane that could not boot
   without a ServiceAccount would be a plane that could not run on a laptop.
6. **The values.yaml carries no credential.** The plane emits exactly `cluster.region`,
   `monitoring.enabled` and `monitoring.remoteWrite.url`. The chart derives the basicAuth username
   and password from the enrolment-token Secret it already creates. A second copy of the token in a
   file an operator downloads, pastes and probably keeps is a second place it can leak from.

## The one widening this accepts

**The plane now writes a Kubernetes Secret.** That is a real change in kind and it deserves to be
named, not buried. One of the founding rules is that the plane does not know what a Kubernetes
Deployment is and the operator is the only thing that talks to Kubernetes. This does not breach that
rule, and the distinction is the whole justification:

- It is the plane's **own** cluster, not a fleet cluster. No cluster is dialed; nothing about a
  cluster's desired state moves through Kubernetes.
- It is **one object**, by name, with two verbs. The `Role` uses `resourceNames` to scope to the one
  Secret (`prom-remote-write-auth` by default) and grants `get` and `patch` only. `create` is
  intentionally absent: `resourceNames` cannot scope it, so granting it would mean "create any Secret
  in this namespace".
- It is **not a controller**. There is no watch, no informer, no owner reference and no finalizer.
  It is a periodic diff-and-patch of one key.

The alternative was a separate credential for telemetry with its own lifecycle: a second thing to
mint, store, rotate and revoke, and a second thing that can be out of step with the first. That is
more surface, not less.

## Limitations

**Per-cluster credentials buy revocation granularity, not label integrity.** Traefik authenticates
the *connection*. It does not inspect the body of a remote-write request and it does not police
`external_labels`, so nothing stops cluster A from writing series stamped `cluster: B`. What this
change actually buys is that one cluster's credential can be revoked without touching any other's,
and that a decommissioned cluster's line disappears from the file when its row does. It does not
make the `cluster` label trustworthy. Anything that needs that (a per-tenant view, a billing
number) must not read the label as an authenticated claim. Enforcing it would need a write proxy
that rewrites labels from the authenticated identity, which is a data-plane component and therefore
squarely against the rule that the data plane never crosses the control plane.

**Bcrypt on the ingress is CPU, and on a public endpoint it is a DoS lever.** Traefik verifies the
hash on *every* remote-write request; at a 15s send interval that is a steady per-cluster cost, and
an unauthenticated flood of requests to `/api/v1/write` makes the ingress spend bcrypt time per
attempt. Bcrypt also buys much less here than it would for a human password: the credential is
`secrets.token_urlsafe(32)`, so an attacker holding the hash has no dictionary to walk and no
recovery to attempt: the slow KDF is protecting against an attack that does not apply.

So the cost factor is **8**, not the `bcrypt` library's default of 12: roughly a sixteenth of the
verification cost, chosen because the entropy of the secret is doing the work the cost factor
normally does. It is recorded in one place, `auth.REMOTE_WRITE_BCRYPT_ROUNDS`, and asserted by a
test so it cannot drift silently. Raising it is a decision about ingress CPU, not about security.
The residual DoS exposure is not fixed by this change and would be addressed with a rate limit in
front of the middleware.

## Consequences

- **Existing clusters get NULL and must re-issue their token.** The plaintext exists only at mint,
  so `remote_write_hash` cannot be backfilled. A cluster registered before this migration is absent
  from the htpasswd and its agent gets a 401 until someone re-issues its enrolment token and
  reinstalls with the new values. This is the one visible cost of the change.
- **Whatever populated the Secret before must stop.** Two controllers writing one key is a flap: the
  reactor patches `users` from the registry and an external sync restores it on its own refresh,
  forever. If that sync owns the Secret, deleting it may delete the Secret too, so it has to be
  *replaced* by a plain, empty Secret stub (`users: ""`), not simply removed. The plane fills
  it on the next tick.
- **One manual step remains, and it is cluster-level, not per-cluster.** The `Role` and
  `RoleBinding` that let the plane patch the Secret are applied once by hand, in the namespace the
  Secret lives in, which is generally not the plane's own, so they cannot ride along with a
  kustomization that pins the plane's namespace.
- **The hub has its own ServiceAccount now** (`wingsmith-hub`) instead of `default`, because the
  RoleBinding must name something narrower than "every pod in the plane's namespace".
- **`GET /v1/system/config` reports the write URL and the Secret's coordinates.** Both are
  diagnostic and neither is secret; "the plane is not maintaining that file" is exactly the kind of
  fault this endpoint exists to make visible, and it renders as empty when the flag is off.
