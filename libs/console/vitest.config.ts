import path from "path"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vitest/config"

// Deliberately separate from vite.config.ts. The dev server's config carries the router plugin,
// which regenerates routeTree.gen.ts on every run — a test run that rewrote a checked-in file
// would make `just fmt --check` depend on whether the suite had been run.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    // text-contrast.test.ts reads the theme tokens as text. Without this vitest
    // hands every .css import back empty, `?raw` included.
    css: { include: [/src\/index\.css/] },
    include: ["src/**/*.test.{ts,tsx}"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.test.{ts,tsx}",
        "src/test/**",
        "src/routeTree.gen.ts",
        "src/main.tsx",
        "src/components/ui/**",
      ],
      // A floor, not a target: `pnpm test` (what CI runs) collects coverage and
      // fails if a change drops below it. Raise it when coverage rises.
      thresholds: {
        lines: 40,
        statements: 40,
        functions: 38,
        branches: 78,
      },
    },
  },
})
