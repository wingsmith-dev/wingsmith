const ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"

let counter = 0

/**
 * Mints an id shaped like the plane's: a prefix and a 26-character lowercase
 * Crockford base32 ULID. Deterministic, because a fixture that produced a new
 * id on every reload would defeat the point of a fixture, and lowercase
 * because `contract/API.md` §3 has exactly one canonical spelling.
 */
export function uid(prefix: string): string {
  counter += 1
  let value = counter * 2654435761
  let body = ""
  for (let i = 0; i < 26; i++) {
    body = ALPHABET[(value + i * 7) % 32] + body
    value = Math.floor(value / 3) + i * 13
  }
  return `${prefix}${body}`
}

/** `Event.subject_id` is untyped, so a subject reference is still a bare UUID. */
export function uuid(seed: number): string {
  const hex = (n: number, len: number) =>
    Math.abs(Math.floor(n)).toString(16).padStart(len, "0").slice(0, len)
  return [
    hex(seed * 2654435761, 8),
    hex(seed * 40503, 4),
    hex(seed * 65521, 4),
    hex(seed * 2246822519, 4),
    hex(seed * 3266489917, 12),
  ].join("-")
}

/** `revision` is a microsecond timestamp rendered as a string, never a number. */
export function revision(iso: string): string {
  return String(new Date(iso).getTime() * 1000)
}
