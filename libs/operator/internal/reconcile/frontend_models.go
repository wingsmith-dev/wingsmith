package reconcile

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"slices"
	"strconv"
	"time"

	corev1 "k8s.io/api/core/v1"
)

// FrontendModels answers which models one frontend pod is actually serving.
//
// An interface instead of a bare function because it is the only thing in this loop that leaves
// the API server, and a reconciler that cannot reach the data path must still reconcile. Left nil
// on the reconciler, no frontend is ever asked and the readiness of its pod stands on its own,
// which is what every test that does not care about registration wants.
type FrontendModels interface {
	// Serving lists the model ids one frontend answers to. An error means the question could
	// not be put, which is not the same answer as an empty list.
	Serving(ctx context.Context, podIP string) ([]string, error)
}

// frontendPort is the port `type: frontend` gives every Dynamo frontend, and the one the agent
// proxies to. Fixed by the same CRD that fixes the command line, so it is not ours to configure.
const frontendPort = 8000

// HTTPFrontendModels reads `/v1/models` off a frontend pod directly.
//
// Pod addresses instead of the Service, because the caller has already decided which pods are
// ready and asking the Service would put a load balancer between a specific judgement and the pod
// it was made about: the answer would come back from a pod this loop never looked at.
type HTTPFrontendModels struct {
	client *http.Client
}

// NewHTTPFrontendModels builds the probe with timeouts short enough that a frontend which has
// stopped answering costs one reconcile a couple of seconds instead of blocking it.
func NewHTTPFrontendModels() *HTTPFrontendModels {
	return &HTTPFrontendModels{client: &http.Client{Timeout: 2 * time.Second}}
}

// Serving asks one frontend what it serves.
func (h *HTTPFrontendModels) Serving(ctx context.Context, podIP string) ([]string, error) {
	if podIP == "" {
		return nil, fmt.Errorf("no address for the frontend")
	}
	return h.servingAt(ctx, "http://"+net.JoinHostPort(podIP, strconv.Itoa(frontendPort))+"/v1/models")
}

// servingAt is the request itself, split from the address so a test can put the question to a
// server it stood up, not to a pod address it would have to invent a network for.
func (h *HTTPFrontendModels) servingAt(ctx context.Context, url string) ([]string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	resp, err := h.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("GET %s: %s", url, resp.Status)
	}

	var body struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, fmt.Errorf("decode %s: %w", url, err)
	}
	ids := make([]string, 0, len(body.Data))
	for _, model := range body.Data {
		ids = append(ids, model.ID)
	}
	return ids, nil
}

// frontendServes says whether one ready frontend pod lists this deployment's model, and whether
// that could be established at all.
//
// The second return is the whole restraint. `false, false` is "nothing was asked or nothing
// answered" and leaves the pod's own readiness standing; only `false, true` is a frontend that
// answered and did not name the model. Keeping them apart is what stops a NetworkPolicy, a
// restarting pod or a missing probe from emptying the routing table of a cluster that is serving.
func (r *SyncedResourceReconciler) frontendServes(
	ctx context.Context,
	pod *corev1.Pod,
	model string,
) (serves bool, known bool) {
	// No probe, or no model to look for, is the behaviour that predates this check.
	if r.Frontend == nil || model == "" || pod.Status.PodIP == "" {
		return false, false
	}
	ids, err := r.Frontend.Serving(ctx, pod.Status.PodIP)
	if err != nil {
		r.Log.Debug("could not ask a frontend what it serves",
			"pod", pod.Name, "ip", pod.Status.PodIP, "error", err)
		return false, false
	}
	return slices.Contains(ids, model), true
}
