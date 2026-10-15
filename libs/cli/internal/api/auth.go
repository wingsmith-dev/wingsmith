package api

import (
	"context"
	"net/http"
)

// LoginRequest trades a password for a session. slug names the principal and
// defaults to "admin" on the plane when empty.
type LoginRequest struct {
	Password string `json:"password"`
	Slug     string `json:"slug,omitempty"`
}

// LoginResult is what the plane returns for a successful login. The access
// token is short-lived and signed; the refresh token renews it. Only the
// non-secret fields are ever surfaced to a caller.
type LoginResult struct {
	AccessToken  string `json:"access_token"`
	RefreshToken string `json:"refresh_token"`
	ExpiresIn    int    `json:"expires_in"`
	Kind         string `json:"kind"`
	Tenant       string `json:"tenant,omitempty"`
}

// refreshRequest and logoutRequest carry the refresh token, which is the only
// thing either route reads: neither is behind the bearer check.
type refreshRequest struct {
	RefreshToken string `json:"refresh_token"`
}

// Login posts the password to the resolved server. It is the one call made
// without a bearer token, so it builds its client with an empty one.
func (c *Client) Login(ctx context.Context, req LoginRequest) (LoginResult, error) {
	var out LoginResult
	err := c.send(ctx, http.MethodPost, "/v1/auth/login", nil, req, nil, &out)
	return out, err
}

// Refresh trades a refresh token for a new session. The plane rotates the
// refresh token on every use, so the caller must store the one returned.
func (c *Client) Refresh(ctx context.Context, refreshToken string) (LoginResult, error) {
	var out LoginResult
	err := c.send(ctx, http.MethodPost, "/v1/auth/refresh", nil, refreshRequest{RefreshToken: refreshToken}, nil, &out)
	return out, err
}

// Logout revokes a refresh token server-side. The access token is signed and
// short-lived, so the refresh token is what keeps a session alive and the only
// thing there is to revoke.
func (c *Client) Logout(ctx context.Context, refreshToken string) error {
	return c.send(ctx, http.MethodPost, "/v1/auth/logout", nil, refreshRequest{RefreshToken: refreshToken}, nil, nil)
}
