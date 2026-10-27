/**
 * Isolated so the HTTP transport can read a token without importing the auth
 * store, and so real auth can replace the body of these functions.
 */
let token: string | null = null
let refreshToken: string | null = null

export function getAuthToken(): string | null {
  return token
}

export function setAuthToken(next: string | null): void {
  token = next
}

export function getRefreshToken(): string | null {
  return refreshToken
}

export function setRefreshToken(next: string | null): void {
  refreshToken = next
}

/**
 * Called when the plane rejects a token. Lives here and not in the store, so
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
