package observe

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"

	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Labels the DCGM exporter puts on every series. They are read for identity only: the sample
// value on the line is telemetry and is never looked at, let alone forwarded (ADR 0007).
//
// The workload holding a card is spelled two different ways depending on who scraped the exporter,
// and both have to be read. Scraped through Prometheus, the scraper stamps its own `pod` and
// `namespace` on every series and the exporter's own values are renamed out of the collision to
// `exported_pod` / `exported_namespace`. Scraped directly, as this operator does, no
// collision happens, nothing is renamed, and the workload is in plain `pod` / `namespace`.
//
// Reading only the `exported_` spelling is why the binding was empty on every real cluster: the
// prefix appears precisely when something else has already claimed the plain name, so it is the
// exception and not the rule. `exported_` still wins where present, because when both exist the
// plain one is the scraper's own pod and the prefixed one is the card's.
const (
	dcgmLabelUUID            = "uuid"
	dcgmLabelIndex           = "gpu"
	dcgmLabelModel           = "modelname"
	dcgmLabelHost            = "hostname"
	dcgmLabelPod             = "exported_pod"
	dcgmLabelNamespace       = "exported_namespace"
	dcgmLabelDirectPod       = "pod"
	dcgmLabelDirectNamespace = "namespace"

	labelKubeHostname = "kubernetes.io/hostname"
)

// boundPod is the workload pod a series attributes its card to.
//
// Which label holds it is a property of the whole scrape, never of the line, which is why
// `renamed` is decided once over the body and passed in. Deciding per line looks equivalent and is
// not: in a renamed body an idle card carries no `exported_pod` at all, and falling back to the
// plain one there would attribute every unused accelerator in the fleet to the exporter's own pod.
func boundPod(labels map[string]string, renamed bool) (string, string) {
	if renamed {
		return labels[dcgmLabelPod], labels[dcgmLabelNamespace]
	}
	return labels[dcgmLabelDirectPod], labels[dcgmLabelDirectNamespace]
}

// DCGMDevice is one accelerator as the exporter names it.
type DCGMDevice struct {
	UUID      string
	Index     int
	Model     string
	Host      string
	Pod       string
	Namespace string
}

// DCGMInventory is one scrape, indexed by the host names a node might be known by.
//
// The exporter's Hostname is whatever the node calls itself, which is usually the Kubernetes node
// name but is not guaranteed to be: the index therefore holds both the
// full name and the segment before the first dot, and a node is looked up by its name and by its
// kubernetes.io/hostname label under the same two spellings.
type DCGMInventory struct {
	byHost  map[string][]DCGMDevice
	Devices int
	Hosts   int
}

// DCGMSource scrapes the in-cluster DCGM exporters for device identity.
//
// This is a cluster-local read on the observation loop's own cadence, and the merged result is
// handed to every node in the snapshot.
//
// The exporter is a DaemonSet and each pod reports only its own node's cards, so scraping the
// Service in front of them once per tick reaches one arbitrary pod and sees one node. Every other
// node then falls back to a synthetic device id, and because a different pod answers each tick,
// a node accumulates one row under its real UUID and another under the fallback. Expanding the
// Service to its pods and scraping all of them is what keeps device identity stable.
type DCGMSource struct {
	Endpoint string
	Client   *http.Client

	// Kube expands Endpoint to the pods behind it. Optional: without it, or when Endpoint is not
	// a Service's cluster-DNS name, Endpoint is scraped exactly as given, which is what a
	// single exporter, a literal address, or a test server wants.
	Kube client.Client

	// Selector overrides the label selector used to find those pods. Empty means
	// `app.kubernetes.io/name=<service>`, which is what the exporter's own chart sets.
	Selector string

	Log *slog.Logger
}

func (s *DCGMSource) log() *slog.Logger {
	if s.Log == nil {
		return slog.Default()
	}
	return s.Log
}

// Scrape reads every exporter and returns what they say is attached to which host.
//
// Partial failure is not fatal: a target that will not answer costs its own node's identity, and
// failing the whole scrape would cost every node's. The count is logged so a fleet that is
// quietly half-covered does not look healthy.
func (s *DCGMSource) Scrape(ctx context.Context) (*DCGMInventory, error) {
	if s == nil || strings.TrimSpace(s.Endpoint) == "" {
		return nil, nil
	}
	targets := s.targets(ctx)

	bodies := make([]string, len(targets))
	errs := make([]error, len(targets))
	var wg sync.WaitGroup
	for i, target := range targets {
		wg.Add(1)
		go func() {
			defer wg.Done()
			bodies[i], errs[i] = s.fetch(ctx, target)
		}()
	}
	wg.Wait()

	got, failed := make([]string, 0, len(targets)), 0
	for i := range targets {
		if errs[i] != nil {
			failed++
			s.log().Warn("dcgm target failed", "target", targets[i], "error", errs[i])
			continue
		}
		got = append(got, bodies[i])
	}
	if len(got) == 0 {
		return nil, fmt.Errorf("scrape dcgm: all %d target(s) failed: %w", len(targets), errs[0])
	}
	if failed > 0 {
		s.log().Warn("dcgm coverage is partial", "scraped", len(got), "failed", failed)
	}

	// One parse over the concatenated exposition instead of a merge of inventories: the parser
	// already keys devices by UUID and indexes them by host, so joining the bodies is the merge.
	return ParseDCGM(strings.Join(got, "\n")), nil
}

func (s *DCGMSource) fetch(ctx context.Context, endpoint string) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return "", fmt.Errorf("dcgm request: %w", err)
	}
	c := s.Client
	if c == nil {
		c = http.DefaultClient
	}
	resp, err := c.Do(req)
	if err != nil {
		return "", fmt.Errorf("scrape dcgm: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<10))
		return "", fmt.Errorf("scrape dcgm: status %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
	if err != nil {
		return "", fmt.Errorf("read dcgm: %w", err)
	}
	return string(body), nil
}

// targets expands the configured endpoint to one URL per exporter pod, falling back to the
// endpoint itself whenever that cannot be done: no client, not a Service name, or a Service
// nothing is behind. The fallback is the old single-GET behaviour, never an empty scrape.
func (s *DCGMSource) targets(ctx context.Context) []string {
	single := []string{s.Endpoint}
	if s.Kube == nil {
		return single
	}
	parsed, err := url.Parse(s.Endpoint)
	if err != nil {
		return single
	}
	service, namespace, ok := parseServiceHost(parsed.Hostname())
	if !ok {
		return single
	}
	selector, err := parseSelector(s.Selector, service)
	if err != nil {
		s.log().Warn("dcgm selector is not usable, scraping the endpoint as given", "error", err)
		return single
	}

	var pods corev1.PodList
	if err := s.Kube.List(ctx, &pods, client.InNamespace(namespace), client.MatchingLabels(selector)); err != nil {
		s.log().Warn("dcgm pod discovery failed, scraping the endpoint as given", "error", err)
		return single
	}

	out := []string{}
	for i := range pods.Items {
		pod := &pods.Items[i]
		if pod.Status.PodIP == "" || pod.Status.Phase != corev1.PodRunning {
			continue
		}
		host := pod.Status.PodIP
		if port := parsed.Port(); port != "" {
			host = net.JoinHostPort(host, port)
		}
		out = append(out, (&url.URL{Scheme: parsed.Scheme, Host: host, Path: parsed.Path, RawQuery: parsed.RawQuery}).String())
	}
	if len(out) == 0 {
		return single
	}
	sort.Strings(out)
	return out
}

// parseServiceHost reads `<service>.<namespace>.svc[.cluster.local]`, the only host shape that
// names something with pods behind it. Anything else (an address, an external name, a Prometheus
// federate URL) is a single target and is left alone.
func parseServiceHost(host string) (service, namespace string, ok bool) {
	parts := strings.Split(strings.TrimSuffix(host, "."), ".")
	if len(parts) < 3 || parts[2] != "svc" {
		return "", "", false
	}
	if parts[0] == "" || parts[1] == "" {
		return "", "", false
	}
	return parts[0], parts[1], true
}

// parseSelector reads `k=v,k2=v2`, defaulting to the label the exporter's chart sets.
func parseSelector(spec, service string) (map[string]string, error) {
	spec = strings.TrimSpace(spec)
	if spec == "" {
		return map[string]string{"app.kubernetes.io/name": service}, nil
	}
	out := map[string]string{}
	for _, pair := range strings.Split(spec, ",") {
		pair = strings.TrimSpace(pair)
		if pair == "" {
			continue
		}
		key, value, found := strings.Cut(pair, "=")
		key = strings.TrimSpace(key)
		if !found || key == "" {
			return nil, fmt.Errorf("selector %q is not a comma-separated list of key=value", spec)
		}
		out[key] = strings.TrimSpace(value)
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("selector %q names no labels", spec)
	}
	return out, nil
}

// ParseDCGM reads a Prometheus text exposition into a device inventory.
//
// It does not try to be a full exposition parser: every series the exporter emits carries the
// same identity labels, so the first line mentioning a UUID settles that device and the rest are
// only consulted for a workload binding the first line happened not to carry.
func ParseDCGM(body string) *DCGMInventory {
	// Whether this body came through a scraper that stamped its own pod on every series. The
	// prefixed spelling exists only as the loser of that collision, so one occurrence anywhere
	// settles it for the whole body, and its absence settles the other way just as firmly.
	renamed := strings.Contains(body, dcgmLabelPod+"=")

	seen := map[string]*DCGMDevice{}
	order := []string{}
	for _, line := range strings.Split(body, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		open := strings.IndexByte(line, '{')
		close := strings.LastIndexByte(line, '}')
		if open < 0 || close < open {
			continue
		}
		labels := parseDCGMLabels(line[open+1 : close])
		uuid := labels[dcgmLabelUUID]
		if uuid == "" {
			continue
		}
		d, ok := seen[uuid]
		if !ok {
			index, err := strconv.Atoi(labels[dcgmLabelIndex])
			if err != nil {
				index = -1
			}
			d = &DCGMDevice{
				UUID:  uuid,
				Index: index,
				Model: labels[dcgmLabelModel],
				Host:  labels[dcgmLabelHost],
			}
			seen[uuid] = d
			order = append(order, uuid)
		}
		if d.Pod == "" {
			d.Pod, d.Namespace = boundPod(labels, renamed)
		}
	}

	inv := &DCGMInventory{byHost: map[string][]DCGMDevice{}}
	perHost := map[string][]DCGMDevice{}
	for _, uuid := range order {
		d := *seen[uuid]
		perHost[strings.ToLower(d.Host)] = append(perHost[strings.ToLower(d.Host)], d)
		inv.Devices++
	}
	for host, list := range perHost {
		sort.Slice(list, func(i, j int) bool {
			if list[i].Index != list[j].Index {
				return list[i].Index < list[j].Index
			}
			return list[i].UUID < list[j].UUID
		})
		inv.Hosts++
		inv.byHost[host] = list
		if short, _, ok := strings.Cut(host, "."); ok && short != "" {
			if _, clash := inv.byHost[short]; !clash {
				inv.byHost[short] = list
			}
		}
	}
	return inv
}

// parseDCGMLabels splits one series' label set. Label names are lowercased because the exporter
// spells them Hostname and UUID while Prometheus relabelling and other builds spell them
// hostname and uuid, and none of that is worth a second code path.
func parseDCGMLabels(s string) map[string]string {
	out := map[string]string{}
	for len(s) > 0 {
		eq := strings.IndexByte(s, '=')
		if eq < 0 {
			break
		}
		name := strings.ToLower(strings.TrimSpace(strings.TrimPrefix(s[:eq], ",")))
		rest := strings.TrimSpace(s[eq+1:])
		if !strings.HasPrefix(rest, `"`) {
			break
		}
		rest = rest[1:]
		var value strings.Builder
		i := 0
		for i < len(rest) {
			c := rest[i]
			if c == '\\' && i+1 < len(rest) {
				switch rest[i+1] {
				case 'n':
					value.WriteByte('\n')
				default:
					value.WriteByte(rest[i+1])
				}
				i += 2
				continue
			}
			if c == '"' {
				break
			}
			value.WriteByte(c)
			i++
		}
		out[name] = value.String()
		if i >= len(rest) {
			break
		}
		s = strings.TrimPrefix(strings.TrimSpace(rest[i+1:]), ",")
	}
	return out
}

// forNode returns what the exporter attached to this node, matched on the node's name and on its
// kubernetes.io/hostname label, in full and short form.
func (inv *DCGMInventory) forNode(name string, labels map[string]string) []DCGMDevice {
	if inv == nil {
		return nil
	}
	candidates := []string{name, labels[labelKubeHostname]}
	for _, c := range candidates {
		if c == "" {
			continue
		}
		if d, ok := inv.byHost[strings.ToLower(c)]; ok {
			return d
		}
		if short, _, ok := strings.Cut(strings.ToLower(c), "."); ok {
			if d, ok := inv.byHost[short]; ok {
				return d
			}
		}
	}
	return nil
}
