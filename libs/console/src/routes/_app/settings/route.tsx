import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { SettingsPage } from "./page"

export const SETTINGS_SECTIONS = [
  "gpu-models",
  "providers",
  "priority",
  "policies",
  "configuration",
  "preferences",
  "access",
] as const

export type SettingsSection = (typeof SETTINGS_SECTIONS)[number]

export interface SettingsSearch {
  section: SettingsSection
}

function section(value: unknown): SettingsSection {
  return SETTINGS_SECTIONS.includes(value as SettingsSection)
    ? (value as SettingsSection)
    : "gpu-models"
}

export const Route = createFileRoute("/_app/settings")({
  validateSearch: (search: Record<string, unknown>): SettingsSearch => ({
    section: section(search.section),
  }),
  component: withPage(SettingsPage, "/_app/settings"),
})
