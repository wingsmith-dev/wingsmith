// Package api holds the wire types and the HTTP client for the Hangar
// control plane. The CLI speaks to the plane's API and to nothing else: it
// never holds a kubeconfig and never dials a cluster or the hub.
package api

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/hangar/cli/internal/errs"
)

// Client is the live control plane transport.
type Client struct {
	Server  string
	Token   string
	Project string
	HTTP    *http.Client
	Version string
}

// NewClient builds a client against the given base URL.
func NewClient(server, token, project, version string, timeout time.Duration) (*Client, error) {
	if server == "" {
		return nil, errs.New(errs.CodeUsage, "no control plane URL configured").
			WithFix("Set one", "hgr config set context.server https://plane.hangar.dev").
			WithFix("Or run offline", "hgr --fake cluster ls")
	}
	if _, err := url.Parse(server); err != nil {
		return nil, errs.Wrap(err, errs.CodeUsage, "invalid control plane URL %q", server)
	}
	return &Client{
		Server:  strings.TrimRight(server, "/"),
		Token:   token,
		Project: project,
		Version: version,
		HTTP:    &http.Client{Timeout: timeout},
	}, nil
}

func (c *Client) Simulated() bool { return false }

// Whoami resolves the active token.
func (c *Client) Whoami(ctx context.Context) (Identity, error) {
	var out Identity
	err := c.get(ctx, "/v1/whoami", nil, &out)
	out.Server = c.Server
	return out, err
}

// Clusters lists enrolled clusters.
func (c *Client) Clusters(ctx context.Context, opts ListOpts) ([]Cluster, error) {
	q := url.Values{}
	if opts.Status != "" {
		q.Set("phase", opts.Status)
	}
	if opts.Region != "" {
		q.Set("region", opts.Region)
	}
	if opts.Search != "" {
		q.Set("search", opts.Search)
	}
	if opts.Limit > 0 {
		q.Set("limit", strconv.Itoa(opts.Limit))
	}
	if opts.Cursor != "" {
		q.Set("cursor", opts.Cursor)
	}
	for k, v := range opts.Labels {
		q.Add("label", k+"="+v)
	}
	var out struct {
		Items []Cluster `json:"items"`
	}
	if err := c.get(ctx, "/v1/clusters", q, &out); err != nil {
		return nil, err
	}
	return out.Items, nil
}

// Nodes lists cluster members.
func (c *Client) Nodes(ctx context.Context, opts ListOpts) ([]Node, error) {
	var out struct {
		Items []Node `json:"items"`
	}
	err := c.get(ctx, "/v1/nodes", supplyQuery(opts), &out)
	return out.Items, err
}

// Node fetches one node by ID or name.
func (c *Client) Node(ctx context.Context, ref string) (Node, error) {
	var out Node
	err := c.get(ctx, "/v1/nodes/"+url.PathEscape(ref), nil, &out)
	return out, err
}

// Devices lists individual accelerators.
func (c *Client) Devices(ctx context.Context, opts ListOpts) ([]Device, error) {
	var out struct {
		Items []Device `json:"items"`
	}
	err := c.get(ctx, "/v1/devices", supplyQuery(opts), &out)
	return out.Items, err
}

// Device fetches one device by ID or UUID.
func (c *Client) Device(ctx context.Context, ref string) (Device, error) {
	var out Device
	err := c.get(ctx, "/v1/devices/"+url.PathEscape(ref), nil, &out)
	return out, err
}

// Pools lists declared provisioning specs.
func (c *Client) Pools(ctx context.Context, opts ListOpts) ([]Pool, error) {
	var out struct {
		Items []Pool `json:"items"`
	}
	err := c.get(ctx, "/v1/pools", supplyQuery(opts), &out)
	return out.Items, err
}

// Pool fetches one pool by ID or name.
func (c *Client) Pool(ctx context.Context, ref string) (Pool, error) {
	var out Pool
	err := c.get(ctx, "/v1/pools/"+url.PathEscape(ref), nil, &out)
	return out, err
}

// supplyQuery maps ListOpts onto the query params the supply endpoints share.
func supplyQuery(opts ListOpts) url.Values {
	q := url.Values{}
	for k, v := range map[string]string{
		"status":  opts.Status,
		"cluster": opts.Cluster,
		"pool":    opts.Pool,
		"node":    opts.Node,
		"class":   opts.Class,
		"health":  opts.Health,
		"domain":  opts.Domain,
		"region":  opts.Region,
		"search":  opts.Search,
		"cursor":  opts.Cursor,
	} {
		if v != "" {
			q.Set(k, v)
		}
	}
	if opts.All {
		q.Set("all", "true")
	}
	if opts.Cordoned {
		q.Set("cordoned", "true")
	}
	if opts.Limit > 0 {
		q.Set("limit", strconv.Itoa(opts.Limit))
	}
	for k, v := range opts.Labels {
		q.Add("label", k+"="+v)
	}
	return q
}

// Cluster fetches one cluster by ID or name.
func (c *Client) Cluster(ctx context.Context, ref string) (Cluster, error) {
	var out Cluster
	err := c.get(ctx, "/v1/clusters/"+url.PathEscape(ref), nil, &out)
	return out, err
}

func (c *Client) get(ctx context.Context, path string, q url.Values, out any) error {
	u := c.Server + path
	if len(q) > 0 {
		u += "?" + q.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return errs.Wrap(err, errs.CodeInternal, "cannot build request")
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "hgr/"+c.Version)
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	if c.Project != "" {
		req.Header.Set("X-Hangar-Project", c.Project)
	}

	resp, err := c.HTTP.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return errs.Wrap(err, errs.CodeTimeout, "request to %s timed out", c.Server)
		}
		return errs.Wrap(err, errs.CodeNetwork, "cannot reach the control plane at %s", c.Server).
			WithFix("Run offline against the fake backend", "hgr --fake cluster ls")
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if resp.StatusCode >= 400 {
		return httpError(resp, body)
	}
	if out == nil {
		return nil
	}
	if err := json.Unmarshal(body, out); err != nil {
		return errs.Wrap(err, errs.CodeServerError, "control plane returned a response the CLI cannot decode")
	}
	return nil
}

// wireError is the plane's error body. Its shape is an assumption until the
// contract lands; an unparseable body degrades to the status line.
type wireError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
	Detail  string `json:"detail"`
}

func httpError(resp *http.Response, body []byte) error {
	var we wireError
	_ = json.Unmarshal(body, &we)
	msg := we.Message
	if msg == "" {
		msg = strings.TrimSpace(string(body))
	}
	if msg == "" {
		msg = http.StatusText(resp.StatusCode)
	}

	code := errs.CodeServerError
	switch resp.StatusCode {
	case http.StatusBadRequest, http.StatusUnprocessableEntity:
		code = errs.CodeUsage
	case http.StatusUnauthorized:
		code = errs.CodeAuthRequired
	case http.StatusForbidden:
		code = errs.CodeForbidden
	case http.StatusNotFound:
		code = errs.CodeNotFound
	case http.StatusConflict:
		code = errs.CodeConflict
	case http.StatusTooManyRequests:
		code = errs.CodeRateLimited
	}
	if we.Code != "" {
		if _, ok := errs.Explain(errs.ParseCode(we.Code)); ok {
			code = errs.ParseCode(we.Code)
		}
	}

	e := errs.New(code, "%s", msg).WithCausef("HTTP %d from %s", resp.StatusCode, resp.Request.URL.Path)
	if we.Detail != "" {
		e = e.WithCause(we.Detail)
	}
	if code == errs.CodeRateLimited {
		if s := resp.Header.Get("Retry-After"); s != "" {
			if n, err := strconv.Atoi(s); err == nil {
				e = e.WithRetry(n)
			}
		}
	}
	return e
}

// ParseDuration accepts Go durations plus a bare number of seconds.
func ParseDuration(s string) (time.Duration, error) {
	s = strings.TrimSpace(s)
	if n, err := strconv.Atoi(s); err == nil {
		return time.Duration(n) * time.Second, nil
	}
	d, err := time.ParseDuration(s)
	if err != nil {
		return 0, errs.New(errs.CodeUsage, "%q is not a duration", s).
			WithCause("use a Go duration such as 30s, 5m, 1h, or a bare number of seconds")
	}
	return d, nil
}

// ParseKV turns repeated KEY=VALUE flags into a map.
func ParseKV(pairs []string, what string) (map[string]string, error) {
	if len(pairs) == 0 {
		return nil, nil
	}
	out := make(map[string]string, len(pairs))
	for _, p := range pairs {
		k, v, ok := strings.Cut(p, "=")
		if !ok || strings.TrimSpace(k) == "" {
			return nil, errs.New(errs.CodeUsage, "%s %q is not KEY=VALUE", what, p)
		}
		out[k] = v
	}
	return out, nil
}

// Age renders a duration since t the way every listing prints it.
func Age(t time.Time) string {
	if t.IsZero() {
		return "-"
	}
	d := time.Since(t)
	switch {
	case d < time.Minute:
		return fmt.Sprintf("%ds", int(d.Seconds()))
	case d < time.Hour:
		return fmt.Sprintf("%dm", int(d.Minutes()))
	case d < 48*time.Hour:
		return fmt.Sprintf("%dh", int(d.Hours()))
	default:
		return fmt.Sprintf("%dd", int(d.Hours()/24))
	}
}
