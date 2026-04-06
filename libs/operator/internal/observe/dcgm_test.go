package observe

import (
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"

	corev1 "k8s.io/api/core/v1"
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
