# console

The Hangar console. React 19, TypeScript, TanStack Router and Query, shadcn/ui.

It talks to `libs/plane` over HTTP and nothing else: never to a cluster, never to the hub.
Every cluster fact it shows reached the plane through the operator's sync stream first.

## Commands

```bash
pnpm install
pnpm dev            # dev server on :5273 (route tree regenerated on change)
pnpm build          # production build, then type-check
pnpm typecheck
pnpm lint           # ESLint, then knip for dead files, dependencies and exports
pnpm format         # Prettier (format:check to verify)
pnpm test           # Vitest, once, with coverage; fails below the floor in vitest.config.ts
pnpm test:watch
pnpm test:coverage  # the same run
```

From the repo root, `just setup` installs everything and `just dev` runs the plane in compose
with this dev server beside it.

Tests are `*.test.ts` / `*.test.tsx` beside the code they cover. Render components through
`src/test/render.tsx`, which supplies the providers the app mounts at its root. Page tests
render the whole app at a URL against the offline plane through `src/test/app.tsx`; name them
in PascalCase (`OverviewPage.test.tsx`) so the router does not take them for routes.
`vitest.config.ts` is separate from `vite.config.ts` because the router plugin in the latter
rewrites `routeTree.gen.ts`, and a test run must not change a checked-in file.

## Configuration

Copy `.env.example` to `.env.local` for development. Every variable is optional.

| Variable | Default | Meaning |
|---|---|---|
| `VITE_API_MODE` | `http` | `fixtures` runs against the in-browser offline plane instead (see below). |
| `VITE_API_BASE_URL` | empty | Origin the browser calls for `/v1`. Empty means the console's own origin. |
| `VITE_API_PROXY` | `http://localhost:8000` | Dev server only: where it forwards `/v1` and `/health`. Ignored when `VITE_API_BASE_URL` is set. |
| `VITE_READ_ONLY` | `false` | Start new browser sessions in read-only mode. |

In development the dev server proxies `/v1` to the plane, so the browser makes no
cross-origin requests and the plane's CORS allowlist does not need the dev origin.

### Container image

The `Dockerfile` builds a static bundle served by nginx on port 8080. Nothing about the
plane is baked in: `docker/40-hangar-config.sh` writes `config.js` at container start from

| Variable | Default | Meaning |
|---|---|---|
| `HANGAR_API_BASE_URL` | empty | Plane origin (plus optional path). Empty means same origin, behind one ingress. |
| `HANGAR_READ_ONLY` | `false` | Start new browser sessions in read-only mode. |

so one image serves any plane. Runtime values win over the `VITE_*` build-time ones. The
image also sets a Content-Security-Policy (whose `connect-src` allows the configured plane
origin), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and a
`Referrer-Policy`.

The plane image can serve this bundle itself on its own origin; then no configuration is
needed.

## Sign-in

`/login` has one form for two audiences. The account `admin` with the plane's admin password
opens the console. Any other account name is a tenant slug with that tenant's token, and opens
the tenant serving view at `/serving` instead.

Human access is a single operator: the plane issues one admin session for one admin password
and has no per-person users or roles. The session's access and refresh tokens are kept in
session storage, so they survive a reload but not the tab. A 401 triggers one refresh through
`POST /v1/auth/refresh`; if that fails, the console signs out and returns to `/login`.

## Read-only mode

A switch on the Settings page (and `VITE_READ_ONLY` / `HANGAR_READ_ONLY` for the default) makes the
console refuse every write before it leaves the browser. It is a guard against accidents, not
an access control: the plane enforces nothing different for it.

## Fixtures mode

`VITE_API_MODE=fixtures` swaps the HTTP transport for an in-memory plane in
`src/modules/offline`, so the console runs with no backend. Sign-in accepts any admin
password without calling anything; tenant sign-in needs a real plane. A **Fixtures** badge
shows in the sidebar while it is on.

The offline plane is loaded through a dynamic import inside the fixtures branch of
`src/lib/api/index.ts`, so a normal build does not ship it. The seam is the `Transport`
interface:

```ts
interface Transport {
  readonly kind: "http" | "fixtures"
  request<T>(method, path, options?): Promise<T>
}
```

Fixtures register against path templates (`src/lib/api/fixtures.ts`) and answer with the
plane's list envelope and error shapes. An unregistered path throws `NotFoundError`, so a
screen built against an endpoint the offline plane does not know fails loudly.

## Layout

```
src/
├── lib/
│   ├── api/             # transport seam: http.ts, fixtures.ts, index.ts (`api`)
│   ├── config.ts        # runtime config (config.js) over build-time VITE_* values
│   └── ...              # formatting, pagination, keyboard helpers
├── modules/<domain>/    # types, API calls, queries, derivations; no JSX
│   └── offline/         # the fixtures-mode plane
├── components/
│   ├── layout/          # shell, tables, page header, status, shared tiles and panels
│   ├── <domain>/        # components shared by more than one route
│   └── ui/              # shadcn/ui, generated; do not edit by hand
├── routes/              # file-based routes (see below)
└── routeTree.gen.ts     # generated by the router plugin; do not edit
```

Data access and business rules live in `src/modules/`. A route folder holds its page and the
components only that page uses.

## Routing

Only `route.tsx` is a route. `page.tsx` and `PascalCase.tsx` files beside it are co-located
components, ignored by the router through `routeFileIgnorePattern` in `vite.config.ts`.
`__root.tsx` and `_app.tsx` are layout routes; `_app` guards everything under it on a signed-in
admin session. Route components are code-split and load on first visit.

Wrap every page in `withPage(Page, "<routeId>")` so a route renders `<Outlet />` when a child
route is active.

## What is there

Under `/_app`, for the admin session:

- **Overview**: fleet health, what needs attention, cluster reachability.
- **Serving**: endpoints, workloads (create, edit, scale, pause, drain), deployments,
  artifacts (with hardware fitment), discovered workloads awaiting adoption.
- **Fleet**: clusters (including connecting a new one), nodes, devices, device classes,
  pools, and the allocation board.
- **Access**: tenants and their keys, projects, quotas, priority classes, policies.
- **Activity**: alerts, workbench (benchmarks and evaluations), operations (node cordon and
  drain; workload scale, pause and drain), events.
- **Settings**: plane configuration as the plane reports it, and console preferences.

`/serving` is the tenant's own view: the models it is served and their metrics.

## Theming

Press **`d`** to toggle dark and light. Colours are OKLch CSS variables in `src/index.css`;
see `DESIGN.md` for the tokens and the contrast rules the tests enforce.

## Adding shadcn components

```bash
pnpm dlx shadcn@latest add <component-name>
```

They land in `src/components/ui/`.
