# 0024: Endpoint names are per project, slugs are global

**Status:** accepted · 2026-11-27

## Context

An endpoint had a globally unique `name`, which was only true while there was one tenant: two
projects could not both call their chat endpoint `chat`. Workloads already have names per project
(`uq_demand_workload_project_name`), and the declarative API reads endpoints at
`/v1/manifests/endpoints/{project}/{name}`, so the key is already `(project, name)` there.

The gateway never routes on the name. It routes on the endpoint's `slug` (the public model id) and
its aliases, and resolves a duplicate key by keeping whichever binding it indexed first. Aliases
were refused when they clashed with another slug or alias, but nothing refused two endpoints with
the same slug, so the second one was silently unreachable.

## Decision

- **`name` is unique per project.** `unique(name)` becomes `unique(project_id, name)`. A bare name
  resolves when exactly one endpoint has it; when two projects share it, every endpoint-by-name
  lookup (`/v1/endpoints/{ref}` and its subresources, endpoint metrics and SLA, serving quotas,
  endpoint benchmarks) answers 400 and asks for `?project=` (or the id). Same rule as workloads.
- **`slug` is global and is the routing key.** It gets a unique index, and create or update with a
  slug another endpoint has as its slug or alias is a 409. No `spec.host` is added: the slug
  already is the host-like key, and clients keep their URLs.
- **The migration does not pick a winner.** If existing endpoints share a slug, the upgrade stops
  and lists them; an operator renames all but one and reruns it.
- **The CLI retries in the default project.** A bare name the plane calls ambiguous is retried with
  the configured project, as deployment names already are.

## Consequences

- The gateway is unchanged.
- A bare endpoint name that worked before keeps working until a second project uses it, then needs
  `project`. Ids never do.
