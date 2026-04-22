import path from "path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { TanStackRouterVite } from "@tanstack/router-plugin/vite"
import { defineConfig, loadEnv } from "vite"

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "")

  // Point the dev server at an API on another origin without tripping its CORS
  // allowlist: set VITE_API_PROXY (e.g. a `kubectl port-forward` of the prod
  // api on http://localhost:8000) and leave VITE_API_BASE_URL empty. The browser
  // then only ever talks to :5273, and Vite forwards /v1 and /health server-side,
  // so there is no cross-origin request for the plane's CORS policy to reject.
  const apiProxy = env.VITE_API_PROXY

  return {
    plugins: [
      TanStackRouterVite({
        routesDirectory: "./src/routes",
        generatedRouteTree: "./src/routeTree.gen.ts",
        routeFileIgnorePattern: "(^|/)(page\\.tsx|[A-Z][^/]*\\.tsx)$",
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
