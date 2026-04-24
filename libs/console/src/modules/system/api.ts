import { api } from "@/lib/api"

import type { PlaneConfig } from "./types"

export function fetchPlaneConfig() {
  return api.get<PlaneConfig>("/system/config")
}
