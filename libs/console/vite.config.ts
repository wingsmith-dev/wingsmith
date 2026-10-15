import path from "path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { TanStackRouterVite } from "@tanstack/router-plugin/vite"
import { defineConfig, loadEnv } from "vite"

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "")

  // The dev server forwards /v1 and /health to the plane, so the browser only
  // ever talks to :5273 and there is no cross-origin request for the plane's
  // CORS policy to reject. VITE_API_PROXY picks the target (e.g. a
  // `kubectl port-forward`); without it, the local stack from `just dev` on
  // :8000. Setting VITE_API_BASE_URL instead makes the browser call that origin
  // directly and turns the proxy off.
  const apiProxy =
    env.VITE_API_PROXY ||
    (env.VITE_API_BASE_URL ? undefined : "http://localhost:8000")

  return {
    plugins: [
      TanStackRouterVite({
        routesDirectory: "./src/routes",
        generatedRouteTree: "./src/routeTree.gen.ts",
        // page.tsx, PascalCase components and tests sit beside routes without being routes.
        routeFileIgnorePattern:
          "(^|/)(page\\.tsx|[A-Z][^/]*\\.tsx|[^/]*\\.test\\.tsx?)$",
        // Each route's component loads when the route is first visited, not
        // with the shell.
        autoCodeSplitting: true,
      }),
      react(),
      tailwindcss(),
    ],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
    // A port of Hangar's own, and a hard failure if it is taken.
    //
    // Vite's default is to walk forward from 5173 until something is free, which makes the console's
    // origin depend on what else happened to be running that morning — and the API's CORS policy is
    // an explicit list of origins with no wildcard, so a console that moved is a console that is
    // blocked, reported as an unreachable API. strictPort turns that into a startup error naming the
    // conflict, which is the failure that takes a second to read instead of an afternoon.
    server: {
      port: 5273,
      strictPort: true,
      proxy: apiProxy
        ? {
            "/v1": { target: apiProxy, changeOrigin: true },
            "/health": { target: apiProxy, changeOrigin: true },
          }
        : undefined,
    },
  }
})
