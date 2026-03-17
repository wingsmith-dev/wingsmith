# `hgr`

`hgr` is the Hangar command line: one Go binary that drives the control plane's HTTP API and
nothing else. It never holds a kubeconfig, never dials a cluster, and never talks to the hub —
the same rule the console follows, for the same reason.

Every command is noun–verb (`hgr cluster ls`, `hgr cluster get iad-1`), and every command has a
machine contract: humans read a table, scripts read `-o json`, agents read a stable envelope.

> **Status: Supply implemented.** `cluster`, `node`, `device`, and `pool` are implemented end to
> end. The Demand, Binding, and Machinery nouns are registered so the surface is reviewable, but
> their verbs return `UNIMPLEMENTED` until the API contract lands. Nothing here guesses at an
> endpoint that has not been agreed.

---

## Quickstart

Nothing is deployed yet, so start offline:

```sh
go build -o bin/hgr ./cmd/hgr
./bin/hgr --fake cluster ls
./bin/hgr --fake cluster get iad-1
./bin/hgr --fake cluster ls -o json | jq '.items[].name'
./bin/hgr --fake node ls --cluster iad-1
./bin/hgr --fake device ls --cluster iad-1 --status free --group-by domain
./bin/hgr --fake pool ls
```

```
$ hgr --fake cluster ls
NAME   REGION        PHASE     NODES  DEVICES  DEPLOYS  CLASSES           SYNCED
ams-1  eu-west       draining      8     8/64        2  a100-80               3m
iad-1  us-east       active       24  168/192       11  h100-80               9s
sin-1  ap-southeast  pending       0      0/0        0  -                  never
sjc-1  us-west       active       16   52/128        7  h200-141,l40s-48     14s
↳ hgr cluster get ams-1  Inspect one
```

```
$ hgr --fake cluster get iad-1
iad-1 clu_01hq0v8p2m
  region        us-east
  phase         active
  reachable     yes
  agent         0.4.2
  capabilities  nvlink rdma mig
  nodes         24
  deployments   11
  labels        fabric=nvswitch tier=prod
  enrolled      210d ago
  last sync     9s

devices
CLASS    TOTAL  HEALTHY  ALLOCATED  FREE
h100-80    192      190        168    22
```

Against a real plane:

```sh
hgr config set context.server https://plane.hangar.dev
hgr config set context.token  hgr_pat_…
hgr whoami
hgr cluster ls
```

---

## The noun set

The nouns are the object model in [`docs/plans/01-object-model-and-architecture.md`](../../docs/plans/01-object-model-and-architecture.md),
grouped in help the way the model groups them.

| Group | Nouns | Kind |
|---|---|---|
| Supply | `cluster` ✅, `node` ✅, `device` ✅, `pool` ✅ | observed, except Pool (declared) |
| Demand | `artifact`, `recipe`, `deployment`, `endpoint` | declared |
| Binding | `project`, `quota` | declared |
| Machinery | `operation` | imperative, long-running |

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

The JSON contract for a list is an **object**, never a bare array, so counts and cursors can be
added later without breaking a parser:

```json
{ "items": [ … ], "count": 4 }
```

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
- **Seeded, not random** — five clusters across four regions in every phase, including one
  disabled and one never-synced, so filters and edge rendering have something to bite on.
  `HANGAR_FAKE_STATE=/path/to/state.json` swaps the seed for a fixture.
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

## API assumptions

These were needed to make `cluster` work end to end and are **guesses until the contract track
confirms them**:

- `GET /v1/nodes`, `/v1/devices`, `/v1/pools` list the rest of Supply, with the same envelope and
  the shared query params `cluster`, `pool`, `node`, `class`, `health`, `status`, `domain`,
  `cordoned`, `all`, `search`, `limit`, `cursor`, and repeated `label=k=v`. `GET /v1/{noun}/{ref}`
  resolves by id or by the noun's natural key (node name, pool name, device UUID).
- Node cordon state is an observed boolean beside status, never a status value.
- A device carries its topology domain inline (`{id, name, kind, devices}`) so "eight devices in
  one NVLink domain" is answerable without a second call.
- Pool is declared (`device_class`, `node_shape`, `min_nodes`, `max_nodes`, `provisioning_source`)
  with observed membership (`nodes`, `ready_nodes`, `devices`) on the same object.
- `GET /v1/clusters` returns `{"items": [...]}`; filters are query params `phase`, `region`,
  `search`, `limit`, `cursor`, and repeated `label=k=v`.
- `GET /v1/clusters/{id_or_name}` resolves by either identifier.
- `GET /v1/whoami` returns the active identity.
- Project scope travels as the `X-Hangar-Project` header.
- Cluster phases are `pending | active | draining | disabled`, with reachability a separate
  boolean rather than a phase.
- Device counts on a cluster are a per-class rollup (`class`, `total`, `healthy`, `allocated`),
  not a single integer.
- Errors carry `{"code", "message", "detail"}`, with the code drawn from the same vocabulary
  `hgr explain` lists.

---

## Development

```sh
go build ./...
go vet ./...
go test ./...
go build -o bin/hgr ./cmd/hgr
```

Structure and conventions are lifted from Acme's `libs/cli` deliberately — same shape, no
shared code. See the root [`CLAUDE.md`](../../CLAUDE.md) for why.
