/**
 * The offline plane. Registering every collection in one place is what lets
 * `VITE_API_MODE=fixtures` answer the same paths, with the same envelope and the
 * same errors, as the real thing. Loaded only by the fixtures branch of
 * `lib/api`'s transport, through a dynamic import.
 */
import "./offline"

export { fixtureTransport } from "@/lib/api/fixtures"
