/**
 * Isolated so the HTTP transport can read a token without importing the auth
 * store, and so real auth can replace the body of these two functions.
 */
let token: string | null = null

export function getAuthToken(): string | null {
  return token
}

export function setAuthToken(next: string | null): void {
  token = next
}

/**
 * Called when the plane rejects a token. Lives here rather than in the store so
 * the HTTP transport can report an expiry without importing the store, which
 * imports the transport for its base URL.
 */
let unauthorized: (() => void) | null = null

export function setUnauthorizedHandler(next: (() => void) | null): void {
  unauthorized = next
}

export function reportUnauthorized(): void {
  unauthorized?.()
}
