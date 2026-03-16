# cli

Go. One binary for driving the control plane from a terminal — noun–verb commands
(`cluster ls`, `deployment create`, `node drain`), scriptable, with a machine-readable output mode
so humans, shell scripts, and agents share one surface.

Same shape as Acme's `air` CLI: cobra, `cmd/` for the entrypoint, `internal/` split into
api / config / output / ui / errs, and a fake backend so the whole surface works offline.

It talks to `libs/plane` over HTTP — the same API the console uses, never to a cluster or the hub
directly. Worth building early: on past projects the CLI proved the write path long before the
console existed, and it stays the operator's tool forever.

Nothing here yet.
