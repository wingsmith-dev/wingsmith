# `hgr`

`hgr` is the Hangar command line: one Go binary that talks to the control plane's HTTP API and
nothing else. It never holds a kubeconfig, never dials a cluster, and never talks to the hub,
the same as the console.

Every command is noun–verb (`hgr cluster ls`, `hgr cluster get iad-1`). Humans read a table,
scripts read `-o json`, and agents read a stable envelope.

**Status.** Every noun has `ls` and `get`. The commands that change something are:

| Noun | Verbs |
|---|---|
| `workload` | `create`, `scale`, `pause`, `resume`, `drain`, `rm` |
| `operation` | `create` (`node.drain`, `node.cordon`, `node.uncordon`, and the `workload.*` types), `cancel` |
| `serving-quota` | `create`, `set`, `rm` |
| `alert-threshold` | `create`, `set`, `rm` |
| `benchmark` | `run`, `request create` |
| `discovered` | `track`, `ignore`, `promote`, `rm`, `restore` |

Enrolling clusters, publishing artifacts, and editing endpoints, projects, quotas and policies
are done in the console for now. Not covered at all yet: `alerts`, `notification-channels`.

The wire types are checked field by field against a committed snapshot of the plane's OpenAPI
schemas, which has to be regenerated when a plane schema changes (see [Development](#development)).

---

## Quickstart

Against a plane:

```sh
go build -o bin/hgr ./cmd/hgr
hgr config set context.server http://localhost:8000
hgr login                      # prompts for the admin password
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

`sjc-1` shows why declared and observed state are kept apart: it is `active` because that is
what was declared, and unreachable because that is what the plane sees.

```
$ hgr --fake cluster get iad-1
iad-1 clu_01m0dj00000000000000000001

declared
  display name   iad-1 region
  region         us-east-1
  zone           us-east-1b
  residency      us
  metrics label  iad-1
  phase          active
  capabilities   mig nvlink rdma
  labels         fabric=nvswitch tier=prod
  enrolled       210d
  revision       1773962191207025

observed
  reachable   yes
  sync state  connected
  operator    0.4.2
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

The nouns are the object model in [`docs/architecture`](../../docs/architecture/README.md),
grouped in help the way the model groups them.

| Group | Nouns | Kind |
|---|---|---|
| Supply | `cluster`, `node`, `device`, `device-class`, `topology-domain`, `pool` | observed, except Cluster and Pool (declared halves too) |
| Demand | `artifact`, `workload`, `deployment`, `endpoint`, `discovered` | declared, except Discovered (observed) |
| Binding | `tenant`, `project`, `quota`, `serving-quota`, `priority-class`, `policy` | declared |
| Machinery | `operation`, `allocation`, `component`, `event`, `benchmark`, `alert-threshold` | imperative, observed, and mixed |

Plus CLI-local commands: `login`, `logout`, `context`, `config`, `whoami`, `explain`, `version`.

`hgr endpoint sla <endpoint>` is the one measurement command: an endpoint's service level for the
current month to date, a `--month YYYY-MM`, or a `--from`/`--to` range, with the verdict against the
success-rate objective its serving quota declares (`GET /v1/metrics/endpoints/{ref}/sla`).

Declared state, observed state and operations stay separate at the CLI too. Draining a node is
an operation (`hgr operation create node.drain …`), not a flag on `node`, so it has a record of
who asked, why, and how it ended.

---

## Output modes

`--output` / `-o`, or `HANGAR_OUTPUT`, or `output:` in the config file.

| Mode | For |
|---|---|
| `auto` (default) | human table on a TTY, `json` when piped |
| `human` / `wide` | table; `wide` reveals columns marked wide (id, operator version) |
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

**Agent mode** (`--agent` or `HANGAR_AGENT=1`) wraps everything, success and failure alike, in
one envelope: `schema_version`, `ok`, `command`, `data`, `error`, `warnings`, `next_actions`,
`meta`. Prompts and animation are off, and `meta.simulated` is `true` whenever the fake answered.

---

## The fake backend

`internal/fake` is an offline stand-in for the control plane, for demos, tests and CI without a
server.

- **Selected by** `--fake`, `HANGAR_FAKE=1`, or `fake: true` in the config file. It is opt-in, so
  a command never answers from the fake when the caller expects a real plane. `login` and
  `logout` never touch the network under `--fake`.
- **Implements `api.Backend`**, the same interface the HTTP client implements. Commands cannot
  tell them apart, so a command tested against the fake is a command tested for real shape.
- **Shaped like the server**: field names, the envelope, cursor behaviour and the error
  vocabulary match the plane, and a test compares the fake's output key-for-key against the
  fixtures in `internal/api/testdata`.
- **Seeded, not random**: four clusters in every phase, including an unreachable one and one
  that never connected, plus a cordoned node, an unhealthy device, an allocation holding fewer
  devices than it asked for and a quota over its limit. `HANGAR_FAKE_STATE=/path/to/state.json` pins the state to a file
  and persists mutations, so create-then-poll workflows work offline.
- **Failure injection**: `HANGAR_FAKE_FAIL=NOT_FOUND` (any code from `hgr explain`) makes every
  call fail with that code, which is how error paths and exit codes get tested.

---

## Configuration

`~/.config/hangar/config.yaml` (or `HANGAR_CONFIG`, or `$XDG_CONFIG_HOME/hangar/config.yaml`),
written `0600`.

```yaml
current_context: prod
contexts:
  prod:
    server: https://plane.example.com
    token: …          # written by hgr login
    refresh_token: …  # written by hgr login
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
| `--agent` | `HANGAR_AGENT` | none |
| `--timeout` | `HANGAR_TIMEOUT` | none |
| `--wait-timeout` | `HANGAR_WAIT_TIMEOUT` | none |

There is no default server: until one is set, every command that needs the plane fails with
`USAGE` and says how to set it. `config`, `context`, `explain` and `version` work without a server
or a token.

The file is written atomically with mode `0600`, and an existing file with looser permissions is
tightened on the next write. Values are checked when they are set, so a bad `output` cannot lock
you out of `hgr config`.

### Authentication

Human access to a plane is a single admin login. `hgr login` reads the admin password from
`--password-stdin`, else `HANGAR_ADMIN_PASSWORD`, else a no-echo prompt (never from an argument),
and stores the access and refresh tokens in the active context together with the server it logged
in to. The access token is short-lived; the CLI renews it with the refresh token when it expires,
and `AUTH_EXPIRED` means the refresh token was refused too. `hgr logout` revokes the refresh token
on the plane and clears both locally, and warns if the revoke failed.

For CI, log in as the admin in the job and let the session expire with it:

```sh
printf '%s' "$HANGAR_ADMIN_PASSWORD" | hgr login --server "$HANGAR_SERVER" --password-stdin
```

Do not give CI the operator or gateway service credentials: those are narrow service principals
for the cluster operator and the gateway, with no admin rights on the plane API. `HANGAR_TOKEN`
and `--token` supply an access token without storing it. Tokens are never printed, in any output mode,
and are redacted from the re-run commands suggested in errors. No keychain yet.

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
| 5 | `CONFLICT`, `INVALID_STATE`, `AMBIGUOUS`, `CONFIRMATION_REQUIRED` |
| 8 | `TIMEOUT` |
| 9 | `RATE_LIMITED` |
| 10 | `SERVER_ERROR`, `NETWORK` |
| 11 | `OPERATION_FAILED`: a waited operation or benchmark ended failed or cancelled |
| 130 | `INTERRUPTED` |

6 and 7 are unused. An unknown command, a wrong number of arguments and a bad flag are all
`USAGE`.

Human failures print the message, the cause, runnable fixes, and the code:

```
✗ no control plane URL configured for context "default"
  cause  looked in --server, HANGAR_SERVER, and ~/.config/hangar/config.yaml
  try    hgr config set context.server http://localhost:8000  Set one
         hgr cluster ls --fake  Or work offline
  code   USAGE · hgr explain USAGE
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

The rule that keeps this accurate: **a command builds an `output.Result` and returns it.** It never
writes to stdout, never formats for a specific consumer, and never touches a transport. Adding a
noun means adding one file under `internal/cli/` and one method per verb on `api.Backend`.

---

## The API this speaks

Everything is `/v1` on the plane's HTTP API, as specified in
[`libs/packages/contract/API.md`](../../libs/packages/contract/API.md) and implemented by `libs/plane`. Where the client
and the plane disagree, the plane is right. The schema test catches a disagreement with the
snapshot in `internal/api/testdata/plane-schemas.json`, so a plane change reaches it only once the
snapshot is regenerated.

- **Envelope**: `{items, count, next_cursor}` on every list; a detail response is the object
  itself, unwrapped.
- **Errors**: `{code, message, detail}`, with `detail` an object. `code` is what the CLI
  branches on: `not_found`, `conflict`, `invalid`, `invalid_request`, `not_authorized`,
  `forbidden`, `internal`.
- **Identifiers**: prefixed, lowercase, 30 characters. A path segment takes an id or a name.
  `get` also accepts a unique prefix; verbs that change or delete something take only a whole id
  or name.
- **Filters**: each endpoint declares its own; the client sends only the ones that endpoint
  accepts, because a parameter the server does not know is one it ignores in silence. Shared:
  `limit`, `cursor`, `search`, repeatable `label=k=v`.
- **Declared and observed are never merged**: a cluster has a declared `phase` beside observed
  `reachable` and `sync`; a node has observed `condition`, `ready` and `cordoned` as three
  separate facts; a component has `desired_version` beside `status.observed_version`.
- **No `/v1/whoami`**: human access is a single admin, so `hgr whoami` reports the configured
  context and whether `/health` answers.
- **No project header**: the configured project (`--project`, `HANGAR_PROJECT` or
  `context.project`) limits every listing that takes a project filter and is the project new
  workloads belong to; `--project ''` lists every project. `X-Hangar-Project` is not sent; the
  plane does not scope requests by it.
- **Writes**: every `POST /v1/operations` carries an `Idempotency-Key`: `--idempotency-key`
  when given, otherwise a fresh one per invocation, reported in the agent envelope's
  `meta.idempotency_key` so a retry can reuse it. Conditional updates send `If-Match` against
  `revision`.

## Development

```sh
go build ./... && go vet ./... && gofmt -l . && go test ./...
go build -o bin/hgr ./cmd/hgr
```

Two things keep the client in step with the plane:

- `internal/api/testdata/plane-schemas.json` is every component schema in the plane's OpenAPI
  document, reduced to field names and types. `TestWireTypesMatchThePlaneSchemas` checks each
  wire type against its schema: a read type must have exactly the schema's fields, and a request
  type may not send a field the plane does not accept. The file is a snapshot: nothing
  regenerates it on its own, so regenerate it after changing a plane schema (no database
  needed) and commit the result:

  ```sh
  cd libs/plane && HANGAR_APP_ENV=local uv run python ../cli/scripts/plane-schemas.py \
      > ../cli/internal/api/testdata/plane-schemas.json
  ```

  `--check ../cli/internal/api/testdata/plane-schemas.json` instead exits non-zero and names the
  schemas that differ, which is how a CI job can catch a stale snapshot.

- The other files in `internal/api/testdata/` are example list responses, first captured from a
  running plane and since kept in step with the schemas. The decode tests read them with
  `DisallowUnknownFields` and compare the client's own serialisation key-for-key, and the fake's
  output is compared against them too.

The transport itself is exercised against a live plane when one is pointed at:

```sh
HANGAR_TEST_SERVER=http://localhost:8000 go test ./internal/api/
```

