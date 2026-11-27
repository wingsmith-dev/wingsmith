// Package serve is the host serving backend (P2.4, ADR 0033): one plain engine container per
// replica on the local Docker Engine, GPUs pinned by UUID, the model cache bind-mounted read-only
// and the serving port published on 127.0.0.1 only. It implements backend.Backend and must stay
// free of Kubernetes imports.
package serve

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// DefaultDockerSocket is where dockerd listens.
const DefaultDockerSocket = "/var/run/docker.sock"

// dockerAPI is the Engine API version the client speaks. 1.43 is Docker 24, the first with every
// field used here; a newer daemon answers older versions.
const dockerAPI = "/v1.43"

// Docker is the slice of the Engine API the backend uses. The HTTP client below implements it;
// tests hand in a fake daemon over the same wire.
type Docker interface {
	List(ctx context.Context, labels map[string]string) ([]ContainerSummary, error)
	Create(ctx context.Context, name string, spec ContainerCreate) (string, error)
	Start(ctx context.Context, id string) error
	Inspect(ctx context.Context, id string) (ContainerInspect, error)
	Remove(ctx context.Context, id string) error
	Pull(ctx context.Context, image string) error
}

// ContainerSummary is one row of GET /containers/json.
type ContainerSummary struct {
	ID     string            `json:"Id"`
	Names  []string          `json:"Names"`
	Image  string            `json:"Image"`
	Labels map[string]string `json:"Labels"`
	State  string            `json:"State"`
}

// Name is the container's name without Docker's leading slash.
func (c ContainerSummary) Name() string {
	if len(c.Names) == 0 {
		return ""
	}
	return strings.TrimPrefix(c.Names[0], "/")
}

// ContainerCreate is the body of POST /containers/create.
type ContainerCreate struct {
	Image        string              `json:"Image"`
	Entrypoint   []string            `json:"Entrypoint,omitempty"`
	Cmd          []string            `json:"Cmd,omitempty"`
	Env          []string            `json:"Env"`
	Labels       map[string]string   `json:"Labels"`
	ExposedPorts map[string]struct{} `json:"ExposedPorts"`
	HostConfig   HostConfig          `json:"HostConfig"`
}

// HostConfig is the part of a create body that decides what the container may touch.
type HostConfig struct {
	Binds          []string                 `json:"Binds"`
	PortBindings   map[string][]PortBinding `json:"PortBindings"`
	SecurityOpt    []string                 `json:"SecurityOpt"`
	CapDrop        []string                 `json:"CapDrop"`
	ReadonlyRootfs bool                     `json:"ReadonlyRootfs"`
	Tmpfs          map[string]string        `json:"Tmpfs"`
	Privileged     bool                     `json:"Privileged"`
	DeviceRequests []DeviceRequest          `json:"DeviceRequests,omitempty"`
	RestartPolicy  RestartPolicy            `json:"RestartPolicy"`
	ShmSize        int64                    `json:"ShmSize,omitempty"`
	Memory         int64                    `json:"Memory,omitempty"`
	NanoCPUs       int64                    `json:"NanoCpus,omitempty"`
}

// PortBinding is where a container port is published on the host.
type PortBinding struct {
	HostIP   string `json:"HostIp"`
	HostPort string `json:"HostPort"`
}

// DeviceRequest asks the runtime for devices: the NVIDIA Container Toolkit for driver "nvidia",
// CDI for driver "cdi".
type DeviceRequest struct {
	Driver       string     `json:"Driver"`
	DeviceIDs    []string   `json:"DeviceIDs"`
	Capabilities [][]string `json:"Capabilities,omitempty"`
}

// RestartPolicy is how dockerd restarts a container that exits.
type RestartPolicy struct {
	Name              string `json:"Name"`
	MaximumRetryCount int    `json:"MaximumRetryCount,omitempty"`
}

// ContainerInspect is the part of GET /containers/{id}/json the backend reads.
type ContainerInspect struct {
	ID           string `json:"Id"`
	Name         string `json:"Name"`
	RestartCount int    `json:"RestartCount"`
	State        struct {
		Status     string `json:"Status"`
		Running    bool   `json:"Running"`
		Restarting bool   `json:"Restarting"`
		ExitCode   int    `json:"ExitCode"`
		Error      string `json:"Error"`
		StartedAt  string `json:"StartedAt"`
	} `json:"State"`
	Config struct {
		Labels map[string]string `json:"Labels"`
	} `json:"Config"`
	NetworkSettings struct {
		Ports map[string][]PortBinding `json:"Ports"`
	} `json:"NetworkSettings"`
}

// ErrNoSuchImage is a create refused because the image is not on the host yet.
var ErrNoSuchImage = errors.New("no such image")

// ErrNotFound is a container that does not exist.
var ErrNotFound = errors.New("no such container")

// DockerClient speaks the Engine API over the daemon's unix socket.
type DockerClient struct {
	http *http.Client
}

// NewDockerClient dials the daemon at socket.
func NewDockerClient(socket string) *DockerClient {
	return &DockerClient{http: &http.Client{Transport: &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{Timeout: 5 * time.Second}).DialContext(ctx, "unix", socket)
		},
	}}}
}

func (c *DockerClient) do(ctx context.Context, method, path string, query url.Values, body, out any) error {
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(raw)
	}
	u := "http://docker" + dockerAPI + path
	if len(query) > 0 {
		u += "?" + query.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, method, u, reader)
	if err != nil {
		return err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("docker %s %s: %w", method, path, err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode >= 300 {
		var msg struct {
			Message string `json:"message"`
		}
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
		_ = json.Unmarshal(raw, &msg)
		if msg.Message == "" {
			msg.Message = strings.TrimSpace(string(raw))
		}
		switch {
		case resp.StatusCode == http.StatusNotFound && strings.Contains(strings.ToLower(msg.Message), "no such image"):
			return fmt.Errorf("%w: %s", ErrNoSuchImage, msg.Message)
		case resp.StatusCode == http.StatusNotFound:
			return fmt.Errorf("%w: %s", ErrNotFound, msg.Message)
		}
		return fmt.Errorf("docker %s %s: %d %s", method, path, resp.StatusCode, msg.Message)
	}
	if out == nil {
		// A pull streams progress and reports failure inside the stream.
		return drainStream(resp.Body)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// drainStream reads a JSON progress stream to the end and returns the first error it carries.
func drainStream(r io.Reader) error {
	dec := json.NewDecoder(r)
	for {
		var msg struct {
			Error string `json:"error"`
		}
		if err := dec.Decode(&msg); err != nil {
			if errors.Is(err, io.EOF) {
				return nil
			}
			return nil //nolint:nilerr // a body that is not a stream (a bare 204) is success
		}
		if msg.Error != "" {
			return errors.New(msg.Error)
		}
	}
}

// List implements Docker. Every label must match.
func (c *DockerClient) List(ctx context.Context, labels map[string]string) ([]ContainerSummary, error) {
	filter := make([]string, 0, len(labels))
	for k, v := range labels {
		filter = append(filter, k+"="+v)
	}
	raw, err := json.Marshal(map[string][]string{"label": filter})
	if err != nil {
		return nil, err
	}
	var out []ContainerSummary
	err = c.do(ctx, http.MethodGet, "/containers/json", url.Values{"all": {"1"}, "filters": {string(raw)}}, nil, &out)
	return out, err
}

// Create implements Docker.
func (c *DockerClient) Create(ctx context.Context, name string, spec ContainerCreate) (string, error) {
	var out struct {
		ID string `json:"Id"`
	}
	err := c.do(ctx, http.MethodPost, "/containers/create", url.Values{"name": {name}}, spec, &out)
	return out.ID, err
}

// Start implements Docker.
func (c *DockerClient) Start(ctx context.Context, id string) error {
	return c.do(ctx, http.MethodPost, "/containers/"+url.PathEscape(id)+"/start", nil, nil, nil)
}

// Inspect implements Docker.
func (c *DockerClient) Inspect(ctx context.Context, id string) (ContainerInspect, error) {
	var out ContainerInspect
	err := c.do(ctx, http.MethodGet, "/containers/"+url.PathEscape(id)+"/json", nil, nil, &out)
	return out, err
}

// Remove implements Docker: stop and delete. Absent is success.
func (c *DockerClient) Remove(ctx context.Context, id string) error {
	err := c.do(ctx, http.MethodDelete, "/containers/"+url.PathEscape(id), url.Values{"force": {"1"}}, nil, nil)
	if errors.Is(err, ErrNotFound) {
		return nil
	}
	return err
}

// Pull implements Docker.
func (c *DockerClient) Pull(ctx context.Context, image string) error {
	return c.do(ctx, http.MethodPost, "/images/create", url.Values{"fromImage": {image}}, nil, nil)
}
