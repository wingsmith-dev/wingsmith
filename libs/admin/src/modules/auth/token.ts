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
