package observe

import (
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

const (
	fixtureNode  = "ip-10-0-101-13.us-east-2.compute.internal"
	fixtureUUID0 = "GPU-1e7ad0a2-3a4b-11ee-be56-0242ac120002"
	fixtureUUID1 = "GPU-88162bb4-9e47-05b7-8feb-c45483f8e59c"
)

var discardLog = slog.New(slog.NewTextHandler(io.Discard, nil))

func dcgmFixture(t *testing.T) string {
	t.Helper()
	blob, err := os.ReadFile("testdata/dcgm-exporter.metrics")
	if err != nil {
		t.Fatal(err)
	}
	return string(blob)
}

func dcgmServer(t *testing.T, body string, status int) *DCGMSource {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(status)
		w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return &DCGMSource{Endpoint: srv.URL, Client: srv.Client()}
}

func TestParseDCGMReadsIdentityFromEveryDialectOfLabel(t *testing.T) {
	inv := ParseDCGM(dcgmFixture(t))
	if inv.Devices != 3 || inv.Hosts != 2 {
		t.Fatalf("parsed %d devices on %d hosts", inv.Devices, inv.Hosts)
	}
	got := inv.forNode(fixtureNode, nil)
	if len(got) != 2 {
		t.Fatalf("host has %d devices", len(got))
	}
	if got[0].Index != 0 || got[0].UUID != fixtureUUID0 {
		t.Fatalf("device 0 is %+v", got[0])
	}
	if got[1].Index != 1 || got[1].UUID != fixtureUUID1 {
		t.Fatalf("device 1 is %+v", got[1])
	}
	if got[1].Model != "NVIDIA RTX PRO 6000 Blackwell Server Edition" {
		t.Fatalf("model is %q", got[1].Model)
	}
}

// TestParseDCGMTakesTheWorkloadFromExportedLabels: `pod` on these series is the exporter's own
// pod. Anything reading a workload out of it would report every GPU in the fleet as belonging to
// dcgm-exporter.
func TestParseDCGMTakesTheWorkloadFromExportedLabels(t *testing.T) {
	got := ParseDCGM(dcgmFixture(t)).forNode(fixtureNode, nil)
	if got[1].Pod != "qwen3-32b-frontend-0" || got[1].Namespace != "dynamo" {
		t.Fatalf("workload binding is %q/%q", got[1].Namespace, got[1].Pod)
	}
	if got[0].Pod != "" {
		t.Fatalf("idle device claims workload %q", got[0].Pod)
	}
}

// TestForNodeMatchesAHostThatIsNotSpeltLikeTheNode: the exporter reports whatever the machine
// calls itself, which matches the node name in the reference fleet and is not promised to
// anywhere else.
func TestForNodeMatchesAHostThatIsNotSpeltLikeTheNode(t *testing.T) {
	inv := ParseDCGM(dcgmFixture(t))
	if len(inv.forNode("ip-10-0-101-13", nil)) != 2 {
		t.Fatal("short node name did not match the fully qualified exporter host")
	}
	byLabel := inv.forNode("gpu-node-a", map[string]string{labelKubeHostname: fixtureNode})
	if len(byLabel) != 2 {
		t.Fatal("kubernetes.io/hostname did not match the exporter host")
	}
	if inv.forNode("some-other-box", nil) != nil {
		t.Fatal("an unknown host matched")
	}
}

func TestScrapeReadsTheExporter(t *testing.T) {
	inv, err := dcgmServer(t, dcgmFixture(t), http.StatusOK).Scrape(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if inv.Devices != 3 {
		t.Fatalf("scraped %d devices", inv.Devices)
	}
}

func TestScrapeFailsSoftly(t *testing.T) {
	if _, err := dcgmServer(t, "nope", http.StatusServiceUnavailable).Scrape(t.Context()); err == nil {
		t.Fatal("a 503 was not an error")
	}
	unset := &DCGMSource{}
	inv, err := unset.Scrape(t.Context())
	if err != nil || inv != nil {
		t.Fatalf("an unconfigured source returned (%v, %v)", inv, err)
	}
	var nilSource *DCGMSource
	if _, err := nilSource.Scrape(t.Context()); err != nil {
		t.Fatalf("a nil source errored: %v", err)
	}
}

// TestDCGMSuppliesTheUUIDAndTheContractSuppliesTheClass is the whole point of the change: the id
// the plane stores for a device must be the id the metrics store knows it by.
func TestDCGMSuppliesTheUUIDAndTheContractSuppliesTheClass(t *testing.T) {
	node := gpuNode(fixtureNode, map[string]string{
		labelGPUCount:   "2",
		labelGPUProduct: "NVIDIA-RTX-PRO-6000-Blackwell-Server-Edition",
		labelGPUMemory:  "97249",
	}, nil, true, false)

	d := newKubeDiscoverer(t, node)
	d.DCGM = dcgmServer(t, dcgmFixture(t), http.StatusOK)
	snap, err := d.Discover(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	devices := snap.Nodes[0].Devices
	if len(devices) != 2 {
		t.Fatalf("discovered %d devices", len(devices))
	}
	if devices[0].UUID != fixtureUUID0 || devices[1].UUID != fixtureUUID1 {
		t.Fatalf("device uuids are %q and %q", devices[0].UUID, devices[1].UUID)
	}
	if devices[1].DeviceClass != "rtx-pro-6000-96gb-server-edition" {
		t.Fatalf("device class is %q", devices[1].DeviceClass)
	}
	if devices[1].MemoryTotalMB != 97249 {
		t.Fatalf("memory came from somewhere other than gfd: %d", devices[1].MemoryTotalMB)
	}
}

// TestDCGMClassResolvesThroughTheContractWhenGFDIsSilent: a cluster with the exporter but no
// feature discovery still names its hardware out of libs/packages/contract/devices.json, in the other dialect.
func TestDCGMClassResolvesThroughTheContractWhenGFDIsSilent(t *testing.T) {
	node := gpuNode("ip-10-0-102-7.us-east-2.compute.internal", map[string]string{}, nil, true, false)
	d := newKubeDiscoverer(t, node)
	d.DCGM = dcgmServer(t, dcgmFixture(t), http.StatusOK)
	snap, err := d.Discover(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	devices := snap.Nodes[0].Devices
	if len(devices) != 1 {
		t.Fatalf("discovered %d devices without a gpu.count label", len(devices))
	}
	if devices[0].DeviceClass != "h100-80gb-sxm" {
		t.Fatalf("device class is %q", devices[0].DeviceClass)
	}
	if devices[0].Model != "NVIDIA H100 80GB HBM3" {
		t.Fatalf("model is %q", devices[0].Model)
	}
	// Memory comes from the dictionary here, and a zero would be worse than an absence: the plane
	// filters a recipe's device_memory_mib_min against this number, so a zero makes every recipe
	// that asks for memory unplaceable and blames the recipe for it.
	if devices[0].MemoryTotalMB != 81559 {
		t.Fatalf("memory without a gfd label is %d, not the class's own", devices[0].MemoryTotalMB)
	}
}

// TestAnExplicitDeviceClassStillCarriesItsMemory covers the other way a node can name its hardware:
// the device-class label states the class outright and bypasses both dialects, which used to leave
// memory with nowhere to come from.
func TestAnExplicitDeviceClassStillCarriesItsMemory(t *testing.T) {
	node := gpuNode("gpu-labelled", map[string]string{
		labelGPUCount:    "1",
		labelDeviceClass: "rtx-pro-6000-96gb-server-edition",
	}, nil, true, false)
	d := newKubeDiscoverer(t, node)
	snap, err := d.Discover(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	devices := snap.Nodes[0].Devices
	if len(devices) != 1 {
		t.Fatalf("discovered %d devices", len(devices))
	}
	if devices[0].DeviceClass != "rtx-pro-6000-96gb-server-edition" {
		t.Fatalf("device class is %q", devices[0].DeviceClass)
	}
	if devices[0].MemoryTotalMB != 97249 {
		t.Fatalf("memory for an explicitly classed device is %d", devices[0].MemoryTotalMB)
	}
}

// TestHardwareNobodyHasDescribedReportsNoMemory: an unknown class slugifies, is not in the
// dictionary, and must stay at zero rather than borrow a number from a card it is not.
func TestHardwareNobodyHasDescribedReportsNoMemory(t *testing.T) {
	node := gpuNode("gpu-exotic", map[string]string{
		labelGPUCount:   "1",
		labelGPUProduct: "NVIDIA-Something-Nobody-Has-Named",
	}, nil, true, false)
	d := newKubeDiscoverer(t, node)
	snap, err := d.Discover(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	device := snap.Nodes[0].Devices[0]
	if device.MemoryTotalMB != 0 {
		t.Fatalf("an unknown class invented %d MiB of memory", device.MemoryTotalMB)
	}
}

// TestUnreachableDCGMDegradesRatherThanFailing: an exporter that is down must cost us joinable
// metrics, not the entire inventory.
func TestUnreachableDCGMDegradesRatherThanFailing(t *testing.T) {
	node := gpuNode("gpu-a", map[string]string{
		labelGPUCount:   "2",
		labelGPUProduct: "NVIDIA-H100-80GB-HBM3",
	}, nil, true, false)
	d := newKubeDiscoverer(t, node)
	d.DCGM = &DCGMSource{Endpoint: "http://127.0.0.1:1/metrics", Client: &http.Client{}}
	snap, err := d.Discover(t.Context())
	if err != nil {
		t.Fatalf("discovery failed because dcgm was down: %v", err)
	}
	devices := snap.Nodes[0].Devices
	if len(devices) != 2 || devices[0].UUID != "gpu-a-gpu0" {
		t.Fatalf("fallback ids are %+v", devices)
	}
}

// TestAnnotationOverridesTheSyntheticFallback keeps the escape hatch for a cluster with no
// exporter: hangar asks for nothing, but a cluster that volunteers real uuids is believed.
func TestAnnotationOverridesTheSyntheticFallback(t *testing.T) {
	node := gpuNode("gpu-a", map[string]string{labelGPUCount: "2"},
		map[string]string{annotationDeviceUUIDs: "GPU-aaaa,GPU-bbbb"}, true, false)
	d := newKubeDiscoverer(t, node)
	snap, err := d.Discover(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	devices := snap.Nodes[0].Devices
	if devices[0].UUID != "GPU-aaaa" || devices[1].UUID != "GPU-bbbb" {
		t.Fatalf("annotation lost to the fallback: %+v", devices)
	}
}

// TestDCGMOutranksTheAnnotation fixes the precedence: the exporter is the hardware talking, the
// annotation is a human asserting.
func TestDCGMOutranksTheAnnotation(t *testing.T) {
	node := gpuNode(fixtureNode, map[string]string{labelGPUCount: "2"},
		map[string]string{annotationDeviceUUIDs: "GPU-stale-0,GPU-stale-1"}, true, false)
	d := newKubeDiscoverer(t, node)
	d.DCGM = dcgmServer(t, dcgmFixture(t), http.StatusOK)
	snap, err := d.Discover(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if snap.Nodes[0].Devices[1].UUID != fixtureUUID1 {
		t.Fatalf("annotation beat dcgm: %+v", snap.Nodes[0].Devices)
	}
}

// TestGFDWinsTheCountAndDCGMWinsTheIdentity: GFD counts what the scheduler can allocate, so a
// device it does not count is not placeable however plainly DCGM can see it. Slots DCGM did not
// name fall through the same chain as a node with no exporter at all.
func TestGFDWinsTheCountAndDCGMWinsTheIdentity(t *testing.T) {
	t.Run("dcgm sees fewer", func(t *testing.T) {
		node := gpuNode(fixtureNode, map[string]string{labelGPUCount: "4"}, nil, true, false)
		d := newKubeDiscoverer(t, node)
		d.DCGM = dcgmServer(t, dcgmFixture(t), http.StatusOK)
		snap, err := d.Discover(t.Context())
		if err != nil {
			t.Fatal(err)
		}
		devices := snap.Nodes[0].Devices
		if len(devices) != 4 {
			t.Fatalf("gfd said 4, we produced %d", len(devices))
		}
		if devices[0].UUID != fixtureUUID0 || devices[1].UUID != fixtureUUID1 {
			t.Fatalf("named devices lost their uuids: %+v", devices)
		}
		if devices[2].UUID != fixtureNode+"-gpu2" || devices[3].UUID != fixtureNode+"-gpu3" {
			t.Fatalf("unnamed slots did not fall back: %+v", devices)
		}
	})

	t.Run("dcgm sees more", func(t *testing.T) {
		node := gpuNode(fixtureNode, map[string]string{labelGPUCount: "1"}, nil, true, false)
		d := newKubeDiscoverer(t, node)
		d.DCGM = dcgmServer(t, dcgmFixture(t), http.StatusOK)
		snap, err := d.Discover(t.Context())
		if err != nil {
			t.Fatal(err)
		}
		devices := snap.Nodes[0].Devices
		if len(devices) != 1 || devices[0].UUID != fixtureUUID0 {
			t.Fatalf("gfd said 1, we produced %+v", devices)
		}
	})
}

func TestDevicesForIgnoresANodeWithNoAccelerators(t *testing.T) {
	node := &corev1.Node{}
	node.Name = "cp-0"
	if got := devicesFor(node, true, ParseDCGM(dcgmFixture(t)), discardLog); got != nil {
		t.Fatalf("a control-plane node discovered %d devices", len(got))
	}
}

// --------------------------------------------------------------------------- pod expansion

// TestScrapeCoversEveryExporterPod is the duplicate-device bug: the exporter is a DaemonSet, so
// scraping the Service in front of it reaches one pod and sees one node's cards. Every other node
// then falls back to a synthetic id, and a different pod answering next tick gives that node a
// second identity.
func TestScrapeCoversEveryExporterPod(t *testing.T) {
	first := exposition("node-a", "GPU-aaaa1111-0000-0000-0000-000000000001", 0)
	second := exposition("node-b", "GPU-bbbb2222-0000-0000-0000-000000000002", 0)

	one := staticServer(t, first)
	two := staticServer(t, second)

	// No port on the endpoint: the pod addresses already carry the one their test server
	// listens on, where in a real cluster the Service's port is the right one for every pod.
	source := &DCGMSource{
		Endpoint: "http://dcgm-exporter.platform.svc.cluster.local/metrics",
		Kube:     podsAt(t, "platform", "dcgm-exporter", one, two),
		Log:      discardLog,
	}

	inv, err := source.Scrape(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if inv.Devices != 2 || inv.Hosts != 2 {
		t.Fatalf("merged inventory has %d devices across %d hosts, want 2 and 2", inv.Devices, inv.Hosts)
	}
	for _, host := range []string{"node-a", "node-b"} {
		if got := inv.forNode(host, nil); len(got) != 1 {
			t.Fatalf("node %s got %d devices from the merged scrape", host, len(got))
		}
	}
}

// TestScrapeSurvivesOneDeadExporter keeps a single unreachable pod from costing every node its
// identity, which is what failing the whole scrape would do.
func TestScrapeSurvivesOneDeadExporter(t *testing.T) {
	alive := staticServer(t, exposition("node-a", "GPU-aaaa1111-0000-0000-0000-000000000001", 0))
	dead := "http://127.0.0.1:1/metrics"

	source := &DCGMSource{
		Endpoint: "http://dcgm-exporter.platform.svc.cluster.local/metrics",
		Kube:     podsAt(t, "platform", "dcgm-exporter", alive, dead),
		Log:      discardLog,
	}
	inv, err := source.Scrape(t.Context())
	if err != nil {
		t.Fatalf("one dead exporter failed the whole scrape: %v", err)
	}
	if inv.Devices != 1 {
		t.Fatalf("scraped %d devices, want the one live pod's", inv.Devices)
	}
}

func TestParseServiceHostOnlyExpandsServiceNames(t *testing.T) {
	for _, tc := range []struct {
		host    string
		service string
		ns      string
		ok      bool
	}{
		{"dcgm-exporter.platform.svc.cluster.local", "dcgm-exporter", "platform", true},
		{"dcgm-exporter.platform.svc", "dcgm-exporter", "platform", true},
		{"prometheus.internal.example.com", "", "", false},
		{"10.0.0.1", "", "", false},
		{"localhost", "", "", false},
	} {
		service, ns, ok := parseServiceHost(tc.host)
		if ok != tc.ok || service != tc.service || ns != tc.ns {
			t.Fatalf("%s -> (%q, %q, %v), want (%q, %q, %v)", tc.host, service, ns, ok, tc.service, tc.ns, tc.ok)
		}
	}
}

func TestParseSelectorDefaultsToTheChartsLabel(t *testing.T) {
	got, err := parseSelector("", "dcgm-exporter")
	if err != nil || got["app.kubernetes.io/name"] != "dcgm-exporter" {
		t.Fatalf("default selector is %v (%v)", got, err)
	}
	got, err = parseSelector("app=dcgm,tier=gpu", "ignored")
	if err != nil || got["app"] != "dcgm" || got["tier"] != "gpu" {
		t.Fatalf("explicit selector is %v (%v)", got, err)
	}
	if _, err := parseSelector("not-a-pair", "x"); err == nil {
		t.Fatal("a selector with no = was accepted")
	}
}

// --------------------------------------------------------------------------- helpers

func exposition(host, uuid string, index int) string {
	return fmt.Sprintf(
		"DCGM_FI_DEV_GPU_UTIL{gpu=\"%d\",UUID=\"%s\",modelname=\"NVIDIA H100 80GB HBM3\",Hostname=\"%s\"} 0\n",
		index, uuid, host,
	)
}

func staticServer(t *testing.T, body string) string {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		io.WriteString(w, body)
	}))
	t.Cleanup(server.Close)
	return server.URL + "/metrics"
}

// podsAt builds a client holding one exporter pod per URL, with the pod's IP and port taken from
// that URL so the scrape reaches the test server standing in for it.
func podsAt(t *testing.T, namespace, service string, urls ...string) client.Client {
	t.Helper()
	objs := []runtime.Object{}
	for i, raw := range urls {
		parsed, err := url.Parse(raw)
		if err != nil {
			t.Fatal(err)
		}
		objs = append(objs, &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:      fmt.Sprintf("%s-%d", service, i),
				Namespace: namespace,
				Labels:    map[string]string{"app.kubernetes.io/name": service},
			},
			Status: corev1.PodStatus{
				Phase: corev1.PodRunning,
				PodIP: net.JoinHostPort(parsed.Hostname(), parsed.Port()),
			},
		})
	}
	return fake.NewClientBuilder().WithScheme(clientgoscheme.Scheme).WithRuntimeObjects(objs...).Build()
}
