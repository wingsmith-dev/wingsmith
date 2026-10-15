// Dead-code check: `pnpm knip`. The source-scanning tests read every file through
// import.meta.glob, which would count every file as used, so they are not entries here; the
// CSS compiler is off for the same reason (Tailwind scans every file for class names).
export default {
  entry: [
    "src/routes/**/route.tsx",
    "src/routes/__root.tsx",
    "src/routes/_app.tsx",
    "src/routes/index.tsx",
    "src/test/**/*.{ts,tsx}",
    // Generated shadcn components export their whole API whether or not it is used.
    "src/components/ui/*.tsx",
    "src/**/*.test.{ts,tsx}",
    "!src/no-invented-data.test.ts",
    "!src/text-contrast.test.ts",
  ],
  project: ["src/**/*.{ts,tsx}"],
  // Still run by Vitest; listed so knip does not report them as orphans.
  ignore: ["src/no-invented-data.test.ts", "src/text-contrast.test.ts"],
  ignoreDependencies: [
    // Loaded by `vitest --coverage`.
    "@vitest/coverage-v8",
    // Imported from src/index.css.
    "@fontsource-variable/geist-mono",
    "@fontsource-variable/plus-jakarta-sans",
    "shadcn",
    "tailwindcss",
    "tw-animate-css",
  ],
  ignoreExportsUsedInFile: true,
  vitest: false,
  compilers: { css: false },
}
