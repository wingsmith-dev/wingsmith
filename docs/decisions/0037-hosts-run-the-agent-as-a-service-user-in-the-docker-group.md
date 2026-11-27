# 0037: Hosts run the agent as a service user in the docker group

**Status:** accepted · 2026-11-27

## Context

ADR 0030 ran `wingsmith-host` as root for supply only, with a TODO to drop that. ADR 0034 made the
host drive Docker through the Engine API socket, and ADR 0035 added prefetch containers that write
into the agent's model cache. P2.6 ships the agent as `.deb` and `.rpm` packages, so the account it
runs as and the files it owns are now set by the package rather than by each operator.

## Decision

- The packaged unit runs the agent as a `wingsmith` system user and group, with `docker` as a
  supplementary group. The package's post-install step creates the user, `/etc/wingsmith`
  (root:wingsmith 0750), `/etc/wingsmith/credentials` (wingsmith 0700) and `/var/lib/wingsmith`
  (wingsmith 0750), and reloads systemd without enabling the unit.
- The `docker` group is root-equivalent, and the docs say so. We accept that: the agent has to
  create GPU containers, and rootless Podman, the only way around it, is not a priority yet
  (`docker_socket` keeps the Docker-compatible API open for it). Running as a user still removes
  every capability from the agent process itself (empty bounding set, `ProtectKernel*`,
  `ProtectControlGroups`, `RestrictSUIDSGID`, `LockPersonality`, `RestrictNamespaces`).
- NVML stays in the agent, so the unit keeps `DeviceAllow` for `/dev/nvidia*` under a closed device
  policy; the driver creates those nodes world-accessible, so no `video` group is needed.
- One rule for every credential file (enrolment token, `credentials/*.token`): a regular file,
  mode 0600, owned by root or by the uid the agent runs as, opened without following symlinks.
  Root stays allowed so an operator can drop a token with plain `install -m 600`, and an older
  install running as root keeps working.
- `enrol`, run as root with the service user present, chowns the token to it. Prefetch containers
  run as the agent's uid:gid, so files they write into the cache are ones the agent can evict
  without `CAP_DAC_OVERRIDE`.
- Packages are built with nfpm in an AlmaLinux 8 container (cgo for NVML against glibc 2.28) on
  native amd64 and arm64 runners, and attached to the tag's GitHub release. No package repository.

## Consequences

- Anyone who can become `wingsmith` can become root through Docker; the host's `docker` group is
  the boundary to guard.
- Upgrading from a root install is installing the package: post-install hands the token and state
  to `wingsmith`. Containers keep running across agent restarts, as before.
- Rootless Podman, a package repository and signed packages are later work.
