/**
 * The offline plane, imported for side effects only. Registering every
 * collection in one place is what lets `VITE_API_MODE=fixtures` answer the same
 * paths, with the same envelope and the same errors, as the real thing.
 */
import "./offline"
