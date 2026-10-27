import { POLL } from "@/lib/poll"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useAllPages, useCursorList } from "@/lib/paginate"
import {
  acknowledgeAlert,
  createAlertThreshold,
  deleteAlertThreshold,
  fetchAlertRules,
  fetchAlertThresholds,
  fetchAlerts,
  resolveAlert,
  updateAlertThreshold,
  type AlertFilters,
  type AlertThresholdFilters,
} from "./api"
import type {
  AlertThresholdCreateRequest,
  AlertThresholdUpdateRequest,
} from "./types"

/**
 * How often the alert list is re-read. Short, and unconditional: this is the
 * screen somebody leaves open during an incident, and a poll that stopped
 * because nothing was firing would never show them the moment something was.
 */
const POLL_MS = POLL.alerts

export function useAlerts(filters: AlertFilters = {}) {
  return useCursorList(
    ["alerts", filters],
    (cursor) => fetchAlerts(filters, cursor),
    { refetchInterval: POLL_MS }
  )
}

export function useAcknowledgeAlert() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: { ref: string; note?: string | null }) =>
      acknowledgeAlert(vars.ref, { note: vars.note ?? null }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["alerts"] }),
  })
}

export function useResolveAlert() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => resolveAlert(ref),
    onSuccess: () => client.invalidateQueries({ queryKey: ["alerts"] }),
  })
}

export function useAlertThresholds(filters: AlertThresholdFilters = {}) {
  return useCursorList(["alert-thresholds", filters], (cursor) =>
    fetchAlertThresholds(filters, cursor)
  )
}

/**
 * Every threshold, drained. Resolution is a question about the whole set — the
 * fleet default a row overrides may be on a page nobody has scrolled to — so it
 * is answered against all of them and never against what a filter left behind.
 */
export function useAlertThresholdOptions() {
  return useAllPages(["alert-thresholds", "options"], (cursor) =>
    fetchAlertThresholds({}, cursor, 200)
  )
}

/** The catalog of rules a threshold may name. Code, not data, so it does not move. */
export function useAlertRules() {
  return useQuery({
    queryKey: ["alert-rules"],
    queryFn: () => fetchAlertRules(),
    staleTime: Infinity,
    select: (response) => response.items,
  })
}

export function useCreateAlertThreshold() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: AlertThresholdCreateRequest) =>
      createAlertThreshold(body),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: ["alert-thresholds"] }),
  })
}

export function useUpdateAlertThreshold() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: { ref: string; body: AlertThresholdUpdateRequest }) =>
      updateAlertThreshold(vars.ref, vars.body),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: ["alert-thresholds"] }),
  })
}

export function useDeleteAlertThreshold() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => deleteAlertThreshold(ref),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: ["alert-thresholds"] }),
  })
}
