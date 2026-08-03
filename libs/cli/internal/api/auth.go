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
}

// Login posts the admin password to the resolved server. It is the one call
// made without a bearer token, so it builds its client with an empty one.
func (c *Client) Login(ctx context.Context, req LoginRequest) (LoginResult, error) {
	var out LoginResult
	err := c.send(ctx, http.MethodPost, "/v1/auth/login", nil, req, nil, &out)
	return out, err
}

// Logout best-effort revokes the current session token server-side. The token
// is already carried on the client, so the body is empty.
func (c *Client) Logout(ctx context.Context) error {
	return c.send(ctx, http.MethodPost, "/v1/auth/logout", nil, struct{}{}, nil, nil)
}
