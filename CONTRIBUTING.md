# Contributing to Hangar

Thanks for looking. Hangar is pre-release and maintained by a small team, so the process below is
light. If something here is unclear or gets in your way, open an issue about that too.

- **Bugs and feature requests**: open an issue using one of the templates.
- **Security problems**: do not open an issue. See [SECURITY.md](SECURITY.md).
- **Larger changes** (a new object, a wire-protocol change, a new component): open an issue first so
  the design can be agreed before you write it. Anything that changes a recorded decision needs an
  ADR (below).

Everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

You need [`just`](https://github.com/casey/just), Docker, [`uv`](https://docs.astral.sh/uv/),
`pnpm`, Go and Python 3.12. `kind`, `kubectl` and `helm` are only needed for the cluster recipes.

```sh
git config core.hooksPath .githooks   # once per clone: the pre-commit and commit-msg hooks
just setup                            # console packages, Python virtualenvs, Go modules
just dev                              # Postgres, migrations, api and hub in compose, plus the console
```

`just` on its own lists every recipe. Each component under `libs/` has its own README describing
its layout and how to run it on its own.

## Checks

| Command | What it runs | Needs |
|---|---|---|
| `just fmt` | ruff, gofmt, prettier (`--check` to verify without writing) | nothing |
| `just lint` | ruff, `go vet`, eslint, tsc | nothing |
| `just test` | pytest, `go test`, vitest | nothing: no database, no cluster |
| `just test-db` | the database-backed plane tests | Docker (or `HANGAR_TEST_DATABASE_URL`) |
| `just interop` | the real Go operator client against the real Python hub | nothing beyond `just setup` |
| `just check` | all of the above, in order | Docker |

Run `just check` before you open a pull request. CI runs the same checks on every pull request
(`.github/workflows/ci.yml`), including the database suite against a Postgres service, so if you
cannot run that suite locally, say so in the pull request and rely on CI for it. `just chart-lint` covers the Helm chart, and `just e2e`
runs the reconciler against a real API server; point it at a local kind cluster, never at one
serving traffic. `just check-docs` fails on a dead relative link, anchor or backticked path in the
Markdown, and `just conformance-gateway` runs the conformance probes through a locally built
gateway; CI runs both.

## Commits

- **Small and logical.** One behaviour change per commit, with its tests in the same commit or the
  one right after it.
- **Subject style**: a lowercase conventional type and scope, then a full sentence saying what
  changed in behaviour, not which files were touched:

  ```
  fix(plane): refuse a metrics window that is not a plain duration
  feat(console): show the deployment's admission reason on the workload page
  docs: describe the gateway's cold-start dependency on the plane
  test(gateway): pin that a tenant's model on another host reads as 404
  ```

  Types in use: `feat`, `fix`, `perf`, `test`, `docs`, `chore`, `style`, `refactor`. Scopes are
  component names: `plane`, `operator`, `gateway`, `agent`, `tunnel`, `console`, `cli`, `chart`,
  `bench`, `conformance`, `transit`.
- **The body explains why**, when the subject cannot. Link the issue if there is one.
- Do not commit generated files, local configuration, credentials or real hostnames. The
  pre-commit hook and CI run a leak check and gitleaks over every change.

## Code conventions

The formatters and linters are the style guide; `just fmt` and `just lint` settle most questions.
Beyond them:

- **plane** (Python). One package per object under `app/modules/<object>/`, each with the same five
  files: `routes.py` (HTTP only), `services.py` (the logic, and the only place a transaction is
  committed), `repository.py` (queries, never commits), `schemas.py` (wire shapes) and `depends.py`
  (dependency wiring). Observed state is written only by `app/ingestion/`; placement
  (`app/modules/placement/`) writes nothing. A module for an observed object has no write methods.
  Database-backed tests carry the `database` marker so `just test` stays offline.
- **Go components** (operator, gateway, agent, cli, tunnel, transit). Each is its own module;
  everything not meant to be imported lives under `internal/`. `libs/packages/tunnel` depends on
  `x/net/http2` and nothing else, and must stay that way.
- **Wire protocol.** Every message type in `libs/packages/contract` has a fixture that both the Go
  and the Python side test against. A message type with no fixture is not part of the contract. A
  change to the protocol changes the fixture, both sides and `PROTOCOL.md`/`MESSAGES.md` together.
- **console** (TypeScript). Talks to the plane's HTTP API only, and runs against built-in fixtures
  with no backend. A number on screen must come from the API; tests fail if a mock marker reaches a
  rendered page.
- **Comments** say why, not what. A comment that restates the code is noise.

## Architecture decision records

Decisions that shape the system are recorded in [`docs/decisions/`](docs/decisions/), and
[`docs/architecture/`](docs/architecture/README.md) is the overview that links them.

- Add one when a change picks between real alternatives, changes the object model or the wire, or
  reverses an earlier record.
- Name it `NNNN-the-decision-as-a-sentence.md`, with the next free number. Use the existing records
  as the template: a `**Status:**` line (`proposed`, `accepted`, `superseded by NNNN`), then
  **Context**, **Decision** and **Consequences**.
- An accepted record is not rewritten. A later decision that changes it gets its own record, and the
  old one gains an "amended by" or "superseded by" note pointing at it.
- Propose an ADR in the same pull request as the change it records, or on its own first if the
  design needs agreeing before the code.

## Pull requests

- Keep a pull request to one concern. Several small ones review faster than one large one.
- Describe what changed in behaviour and how you checked it. The template asks for this.
- Add or update tests for every behaviour change, and update the README or docs a change makes
  wrong.
- CI must be green: format, lint and tests for every module, the database suite and the leak
  checks.
- A maintainer reviews every pull request (see [`.github/CODEOWNERS`](.github/CODEOWNERS)). Expect
  questions about why, not just what; the answers often belong in the commit message.
- Maintainers merge. History on `main` is kept as committed, so a clean series of commits matters
  more than a single squashed one.

## How this code is written

Much of the code in this repository was written with AI coding assistants, under a single
maintainer. The history before the public release was not reviewed by a second person; the test
suites were the main check on it, and they still are. From here on, changes arrive as pull requests
that CI tests and a maintainer reviews, whoever or whatever wrote them.

## License

By contributing you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE). The project name and logo are covered separately, by
[TRADEMARKS.md](TRADEMARKS.md).
