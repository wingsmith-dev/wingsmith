package observe

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"sort"
	"strconv"
	"strings"
)

// Labels the DCGM exporter puts on every series. They are read for identity only: the sample
// value on the line is telemetry and is never looked at, let alone forwarded (ADR 0007).
//
// `pod` and `namespace` describe the exporter's own pod. The workload holding the card is
// `exported_pod` / `exported_namespace`, because the exporter's pod labels win the collision and
// Prometheus renames the scraped ones.
const (
	dcgmLabelUUID      = "uuid"
	dcgmLabelIndex     = "gpu"
	dcgmLabelModel     = "modelname"
	dcgmLabelHost      = "hostname"
	dcgmLabelPod       = "exported_pod"
	dcgmLabelNamespace = "exported_namespace"

	labelKubeHostname = "kubernetes.io/hostname"
)

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
// The exporter's Hostname is whatever the node calls itself, which in the reference fleet is the
// Kubernetes node name but is not guaranteed to be anywhere: the index therefore holds both the
// full name and the segment before the first dot, and a node is looked up by its name and by its
// kubernetes.io/hostname label under the same two spellings.
type DCGMInventory struct {
	byHost  map[string][]DCGMDevice
	Devices int
	Hosts   int
}

// DCGMSource scrapes the in-cluster DCGM exporter for device identity.
//
// This is a cluster-local read on the observation loop's own cadence: one HTTP GET per tick,
// whatever the node count, and the result is handed to every node in the snapshot.
type DCGMSource struct {
	Endpoint string
	Client   *http.Client
	Log      *slog.Logger
}

func (s *DCGMSource) log() *slog.Logger {
	if s.Log == nil {
		return slog.Default()
	}
	return s.Log
}

// Scrape reads the exporter and returns what it says is attached to which host.
func (s *DCGMSource) Scrape(ctx context.Context) (*DCGMInventory, error) {
	if s == nil || strings.TrimSpace(s.Endpoint) == "" {
		return nil, nil
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, s.Endpoint, nil)
	if err != nil {
		return nil, fmt.Errorf("dcgm request: %w", err)
	}
	c := s.Client
	if c == nil {
		c = http.DefaultClient
	}
	resp, err := c.Do(req)
	if err != nil {
		return nil, fmt.Errorf("scrape dcgm: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<10))
		return nil, fmt.Errorf("scrape dcgm: status %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
	if err != nil {
		return nil, fmt.Errorf("read dcgm: %w", err)
	}
	return ParseDCGM(string(body)), nil
}

// ParseDCGM reads a Prometheus text exposition into a device inventory.
//
// It is deliberately not a full exposition parser: every series the exporter emits carries the
// same identity labels, so the first line mentioning a UUID settles that device and the rest are
// only consulted for a workload binding the first line happened not to carry.
func ParseDCGM(body string) *DCGMInventory {
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
			d.Pod, d.Namespace = labels[dcgmLabelPod], labels[dcgmLabelNamespace]
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
