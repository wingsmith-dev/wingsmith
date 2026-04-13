# `hgr`

`hgr` is the Hangar command line: one Go binary that drives the control plane's HTTP API and
nothing else. It never holds a kubeconfig, never dials a cluster, and never talks to the hub —
the same rule the console follows, for the same reason.

Every command is noun–verb (`hgr cluster ls`, `hgr cluster get iad-1`), and every command has a
machine contract: humans read a table, scripts read `-o json`, agents read a stable envelope.

> **Status: 21 of the 24 `/v1` collections are implemented.** Each has `ls` and `get`, and
> `operation` additionally has `create`, `cancel`, and `wait`. Not yet covered: `alerts`,
> `notification-channels`, `replicas`. The wire types are reconciled field-for-field against the
> plane's `schemas.py` and pinned by a corpus captured from a running server; nothing here is a
> guess.

---

## Quickstart

Against a plane:

```sh
go build -o bin/hgr ./cmd/hgr
hgr config set context.server http://localhost:8000
hgr config set context.token  hgr_pat_…
hgr whoami
hgr cluster ls
hgr device ls --cluster iad-1 --health unhealthy
hgr operation create node.drain iad-1-n01 --reason firmware --wait
```

Or offline, with `--fake`:

```sh
./bin/hgr --fake cluster ls
./bin/hgr --fake node ls --cluster iad-1 --cordoned
./bin/hgr --fake device ls --cluster iad-1 --allocatable --group-by domain
./bin/hgr --fake device ls --all -o json | jq '.count'
```

```
$ hgr --fake cluster ls
NAME   REGION          PHASE                 REACHABLE  SYNC             SYNCED
iad-1  us-east-1       active                yes        connected           12s
sjc-1  us-west-2       active (unreachable)  no         disconnected        11m
ams-1  eu-west-1       draining              yes        connected           12s
sin-1  ap-southeast-1  pending               no         never_connected   never
↳ hgr cluster get iad-1  Inspect one
```

`sjc-1` is the point of the whole layout: it is `active` because that is what we declared, and
unreachable because that is what is true. Neither answer is allowed to overwrite the other.

```
$ hgr --fake cluster get iad-1
iad-1 clu_01m0dj00000000000000000001

declared
  display name  iad-1 region
  region        us-east-1
  zone          us-east-1b
  residency     us
  phase         active
  capabilities  mig nvlink rdma
  labels        fabric=nvswitch tier=prod
  enrolled      210d
  revision      1769023398698256

observed
  reachable   yes
  sync state  connected
  agent       0.4.2
  lag         340ms
  last sync   12s
  last seen   8s
  last error  -
```

```
$ hgr --fake operation create node.drain iad-1-n02 --reason firmware --wait
✓ node.drain succeeded

observed
  phase     succeeded
  attempts  1
  took      0s
  result    evicted_replicas=3
```

---

## The noun set

The nouns are the object model in [`docs/plans/01-object-model-and-architecture.md`](../../docs/plans/01-object-model-and-architecture.md),
grouped in help the way the model groups them.

| Group | Nouns | Kind |
|---|---|---|
| Supply | `cluster`, `node`, `device`, `topology-domain`, `pool` | observed, except Cluster and Pool (declared halves too) |
| Demand | `artifact`, `recipe`, `deployment`, `endpoint` | declared |
| Binding | `project`, `quota`, `priority-class`, `policy` | declared |
| Machinery | `operation`, `allocation`, `component`, `event` | imperative, observed, and mixed |

Plus CLI-local commands: `context`, `config`, `whoami`, `explain`, `version`.

Declared, observed, and operations stay three different things at the CLI surface too. An
operation is never a status field: `hgr operation ls` is where drain, warm, and upgrade will
live, not a `--drain` flag hiding on `cluster`.

---

## Output modes

`--output` / `-o`, or `HANGAR_OUTPUT`, or `output:` in the config file.

| Mode | For |
|---|---|
| `auto` (default) | human table on a TTY, `json` when piped |
| `human` / `wide` | table; `wide` reveals columns marked wide (id, agent version) |
| `json` | the stable machine contract, indented |
| `jsonl` | one object per line, for streaming consumers |
| `yaml` | same data, round-tripped through the JSON tags |
| `tsv` | the same columns the table shows, tab-separated |
| `name` | bare identifiers, for `xargs` |
| `template=…` | Go text/template over the JSON |
| `jsonpath=…` | dotted path with `[n]` and `[*]` projection |

The JSON contract for a list is the API's own envelope, an **object** and never a bare array:

```json
{ "items": [ … ], "count": 50, "next_cursor": "MTAwMQ" }
```

`count` is the size of *this page*. A listing with a `next_cursor` is not the whole collection,
and the human output says so; `--cursor` takes the next page and `--all` follows the cursors to
the end.

**Agent mode** (`--agent` or `HANGAR_AGENT=1`) wraps everything — success and failure alike — in
one envelope: `schema_version`, `ok`, `command`, `data`, `error`, `warnings`, `next_actions`,
`meta`. Prompts and animation are off, and `meta.simulated` is `true` whenever the fake answered.

---

## The fake backend

`internal/fake` is a complete, offline stand-in for the control plane. It exists so this track
could be built and tested before the API exists, and it stays useful afterwards for demos, tests,
and CI without a server.

- **Selected by** `--fake`, `HANGAR_FAKE=1`, or `fake: true` in the config file. It is opt-in on
  purpose: silently simulating a plane the caller believes is real is worse than failing to reach
  one.
- **Implements `api.Backend`**, the same interface the HTTP client implements. Commands cannot
  tell them apart, so a command tested against the fake is a command tested for real shape.
- **Shaped like the server** — every field name, the envelope, cursor behaviour and the error
  vocabulary match a real plane, and a test compares the fake's output key-for-key against a
  corpus captured from one. A fixture that disagrees with the server is worse than no fixture:
  it makes the client's tests pass while the client is broken.
- **Seeded, not random** — four clusters in every phase, including an unreachable one and one
  that never connected, plus a cordoned node, an unhealthy device, a partially satisfied gang
  and a quota over its limit. `HANGAR_FAKE_STATE=/path/to/state.json` pins the state to a file
  and persists mutations, so create-then-poll workflows work offline.
- **Failure injection** — `HANGAR_FAKE_FAIL=NOT_FOUND` (any code from `hgr explain`) makes every
  call fail with that code, which is how error paths and exit codes get tested.

---

## Configuration

`~/.config/hangar/config.yaml` (or `HANGAR_CONFIG`, or `$XDG_CONFIG_HOME/hangar/config.yaml`),
written `0600`.

```yaml
current_context: prod
contexts:
  prod:
    server: https://plane.hangar.dev
    token: hgr_pat_…
    project: default
output: auto
fake: false
```

Precedence is flag → environment → config file → default.

| Flag | Env | Config key |
|---|---|---|
| `--context` | `HANGAR_CONTEXT` | `current_context` |
| `--server` | `HANGAR_SERVER` | `context.server` |
| `--token` | `HANGAR_TOKEN` | `context.token` |
| `--project` | `HANGAR_PROJECT` | `context.project` |
| `--output` | `HANGAR_OUTPUT` | `output` |
| `--fake` | `HANGAR_FAKE` | `fake` |
| `--agent` | `HANGAR_AGENT` | — |
| `--timeout` | `HANGAR_TIMEOUT` | — |

Auth is a static bearer token for now: it goes out as `Authorization: Bearer …`. There is no
login flow, no refresh, and no keychain yet.

---

## Errors and exit codes

Every failure is an `errs.Error` with a stable `code`, so humans, scripts, and agents branch on
the same thing. `hgr explain` lists them; `hgr explain NOT_FOUND` describes one.

| Exit | Codes |
|---|---|
| 0 | success |
| 1 | `INTERNAL` |
| 2 | `USAGE`, `INPUT_REQUIRED` |
| 3 | `AUTH_REQUIRED`, `AUTH_EXPIRED`, `FORBIDDEN` |
| 4 | `NOT_FOUND` |
| 5 | `CONFLICT`, `INVALID_STATE`, `AMBIGUOUS`, `CONFIRMATION_REQUIRED`, `INTERACTIVE_UNAVAILABLE`, `UNIMPLEMENTED` |
| 6 | `QUOTA_EXCEEDED` |
| 7 | `CAPACITY_UNAVAILABLE` |
| 8 | `TIMEOUT` |
| 9 | `RATE_LIMITED` |
| 10 | `SERVER_ERROR`, `NETWORK` |
| 130 | `INTERRUPTED` |

Human failures print the message, the cause, runnable fixes, and the code:

```
✗ no token for context "default"
  cause  looked in --token, HANGAR_TOKEN, and ~/.config/hangar/config.yaml
  try    hgr config set context.token <token>  Store a token
         hgr cluster ls --fake  Or work offline
  code   AUTH_REQUIRED · hgr explain AUTH_REQUIRED
```

---

## Layout

```
cmd/hgr/            entrypoint; nothing but os.Exit(cli.Execute())
internal/cli/       cobra tree, global flags, App session, one file per noun
internal/api/       wire types + HTTP client; the Backend interface
internal/fake/      offline control plane implementing the same interface
internal/output/    Result → human / json / jsonl / yaml / tsv / name / template
internal/config/    config file, contexts, env precedence
internal/errs/      typed errors, exit codes, explanations
internal/ui/        style, tables, key-value blocks, spinners, prompts
```

The rule that keeps this honest: **a command builds an `output.Result` and returns it.** It never
writes to stdout, never formats for a specific consumer, and never touches a transport. Adding a
noun means adding one file under `internal/cli/` and one method per verb on `api.Backend`.

---

## The API this speaks

Everything is `/v1` on the plane's HTTP API, as specified in
[`libs/packages/contract/API.md`](../../libs/packages/contract/API.md) and implemented by `libs/plane`. The client was
reconciled against the server's own `schemas.py`, and where the two disagreed the server won.

- **Envelope** — `{items, count, next_cursor}` on every list; a detail response is the object
  itself, unwrapped.
- **Errors** — `{code, message, detail}`, with `detail` an object. `code` is what the CLI
  branches on: `not_found`, `conflict`, `invalid`, `invalid_request`, `not_authorized`,
  `forbidden`, `internal`.
- **Identifiers** — prefixed, lowercase, 30 characters. A path segment takes an id or a name.
- **Filters** — each endpoint declares its own; the client sends only the ones that endpoint
  accepts, because a parameter the server does not know is one it ignores in silence. Shared:
  `limit`, `cursor`, `search`, repeatable `label=k=v`.
- **Declared and observed are never merged** — a cluster has a declared `phase` beside observed
  `reachable` and `sync`; a node has observed `condition`, `ready` and `cordoned` as three
  separate facts; a component has `desired_version` beside `status.observed_version`.
- **No `/v1/whoami`** — auth is stubbed to one static principal, so `hgr whoami` reports the
  configured context and whether `/health` answers, and invents no user.
- **Writes** — `Idempotency-Key` on `POST /v1/operations`; `If-Match` against `revision` for
  conditional updates.

## Development

```sh
go build ./... && go vet ./... && gofmt -l . && go test ./...
go build -o bin/hgr ./cmd/hgr
```

`internal/api/testdata/` is a corpus captured verbatim from a running plane. The unit tests
decode it with `DisallowUnknownFields` and compare the client's own serialisation key-for-key,
so a server-side schema change fails the build rather than silently dropping a field. Refresh it
by curling each collection when the API changes.

The transport itself is exercised against a live plane when one is pointed at:

```sh
HANGAR_TEST_SERVER=http://localhost:8000 go test ./internal/api/
```

Structure and conventions are lifted from Acme's `libs/cli` deliberately — same shape, no
shared code. See the root [`CLAUDE.md`](../../CLAUDE.md) for why.
