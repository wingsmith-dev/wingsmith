package host

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/observe"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

func fixture(t *testing.T, name string) []byte {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestParseSMIReadsCurrentDrivers(t *testing.T) {
	inv, err := ParseSMI(fixture(t, "nvidia-smi-h100x2.xml"))
	if err != nil {
		t.Fatal(err)
	}
	if inv.DriverVersion != "570.86.15" || inv.CUDAVersion != "12.8" || len(inv.GPUs) != 2 {
		t.Fatalf("inventory %+v", inv)
	}
	a, b := inv.GPUs[0], inv.GPUs[1]
	if a.UUID != "GPU-3f1a2b4c-0000-1111-2222-333344445555" || a.Name != "NVIDIA H100 80GB HBM3" || a.MemoryMiB != 81559 {
		t.Fatalf("gpu0 %+v", a)
	}
	if !a.ECCEnabled || a.VolatileCorrected != 12 || a.VolatileUncorrected != 0 || a.MIGEnabled {
		t.Fatalf("gpu0 ecc/mig %+v", a)
	}
	if len(a.ThrottleReasons) != 0 {
		t.Fatalf("an idle card read as throttled: %v", a.ThrottleReasons)
	}
	if b.Index != 1 || !b.MIGEnabled || b.VolatileUncorrected != 2 || b.AggregateUncorrected != 3 {
		t.Fatalf("gpu1 %+v", b)
	}
	if !reflect.DeepEqual(b.ThrottleReasons, []string{"sw_thermal_slowdown"}) {
		t.Fatalf("gpu1 reasons %v", b.ThrottleReasons)
	}
}

func TestParseSMIReadsTheLegacyShape(t *testing.T) {
	inv, err := ParseSMI(fixture(t, "nvidia-smi-t4-legacy.xml"))
	if err != nil {
		t.Fatal(err)
	}
	g := inv.GPUs[0]
	if g.Name != "Tesla T4" || g.MemoryMiB != 15109 || g.VolatileCorrected != 3 || g.AggregateUncorrected != 0 {
		t.Fatalf("t4 %+v", g)
	}
	if !reflect.DeepEqual(g.ThrottleReasons, []string{"sw_power_cap"}) {
		t.Fatalf("t4 reasons %v", g.ThrottleReasons)
	}
}

func TestParseSMIRefusesGarbage(t *testing.T) {
	if _, err := ParseSMI([]byte("NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver")); err == nil {
		t.Fatal("non-XML parsed")
	}
}

type fakeDevice struct {
	gpu     GPU
	noECC   bool
	reasons uint64
}

func (d fakeDevice) UUID() (string, error)             { return d.gpu.UUID, nil }
func (d fakeDevice) Index() (int, error)               { return d.gpu.Index, nil }
func (d fakeDevice) Name() (string, error)             { return d.gpu.Name, nil }
func (d fakeDevice) MemoryTotalBytes() (uint64, error) { return uint64(d.gpu.MemoryMiB) << 20, nil }
func (d fakeDevice) ECC() (bool, uint64, uint64, uint64, error) {
	if d.noECC {
		return false, 0, 0, 0, errors.New("not supported")
	}
	return d.gpu.ECCEnabled, uint64(d.gpu.VolatileCorrected), uint64(d.gpu.VolatileUncorrected), uint64(d.gpu.AggregateUncorrected), nil
}
func (d fakeDevice) ThrottleReasons() ([]string, error) { return ThrottleReasonNames(d.reasons), nil }
func (d fakeDevice) MIGEnabled() (bool, error)          { return d.gpu.MIGEnabled, nil }

type fakeNVML struct {
	initErr error
	inits   int
	devices []fakeDevice
}

func (f *fakeNVML) Init() error                     { f.inits++; return f.initErr }
func (f *fakeNVML) DriverVersion() (string, error)  { return "570.86.15", nil }
func (f *fakeNVML) CUDADriverVersion() (int, error) { return 12080, nil }
func (f *fakeNVML) DeviceCount() (int, error)       { return len(f.devices), nil }
func (f *fakeNVML) Device(i int) (NVMLDevice, error) {
	return f.devices[i], nil
}

func h100(i int, uuid string) fakeDevice {
	return fakeDevice{gpu: GPU{UUID: uuid, Index: i, Name: "NVIDIA H100 80GB HBM3", MemoryMiB: 81559, ECCEnabled: true}}
}

func TestNVMLSourceReadsEveryDeviceAndInitsOnce(t *testing.T) {
	lib := &fakeNVML{devices: []fakeDevice{h100(0, "GPU-a"), {gpu: GPU{UUID: "GPU-b", Index: 1, Name: "NVIDIA L4", MemoryMiB: 23034}, noECC: true, reasons: 0x1 | 0x4}}}
	s := &NVMLSource{Lib: lib}
	for range 2 {
		inv, err := s.Read(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		if inv.CUDAVersion != "12.8" || len(inv.GPUs) != 2 {
			t.Fatalf("inventory %+v", inv)
		}
		if inv.GPUs[1].ECCEnabled || !reflect.DeepEqual(inv.GPUs[1].ThrottleReasons, []string{"sw_power_cap"}) {
			t.Fatalf("gpu1 %+v", inv.GPUs[1])
		}
	}
	if lib.inits != 1 {
		t.Fatalf("nvml initialised %d times", lib.inits)
	}
}

func TestFallbackReadsSMIWhenNVMLIsMissing(t *testing.T) {
	smi := &SMISource{Run: func(context.Context) ([]byte, error) { return fixture(t, "nvidia-smi-h100x2.xml"), nil }}
	for _, nvml := range []*NVMLSource{{Lib: nil}, {Lib: &fakeNVML{initErr: errors.New("libnvidia-ml.so.1: not found")}}} {
		inv, err := Fallback{nvml, smi}.Read(context.Background())
		if err != nil || len(inv.GPUs) != 2 {
			t.Fatalf("fallback: %v %+v", err, inv)
		}
	}
	broken := &SMISource{Run: func(context.Context) ([]byte, error) { return nil, errors.New("not found") }}
	if _, err := (Fallback{&NVMLSource{}, broken}).Read(context.Background()); err == nil {
		t.Fatal("two failed sources read as a success")
	}
}

func testMachine() Machine {
	return Machine{Hostname: "gpu-box-1", KernelVersion: "6.8.0-45-generic", OSImage: "Ubuntu 24.04.1 LTS", CPUCores: 64, MemoryMiB: 515000, CPUArch: "amd64"}
}

func TestDiscoverReportsOneNodeClassedThroughNVMLNames(t *testing.T) {
	bad := h100(1, "GPU-b")
	bad.gpu.VolatileUncorrected = 1
	d := &Discoverer{
		Devices: &NVMLSource{Lib: &fakeNVML{devices: []fakeDevice{h100(0, "GPU-a"), bad}}},
		Machine: testMachine,
		Labels:  map[string]string{"site": "lab"},
	}
	snap, err := d.Discover(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(snap.Nodes) != 1 {
		t.Fatalf("a host reported %d nodes", len(snap.Nodes))
	}
	n := snap.Nodes[0]
	if n.Name != "gpu-box-1" || !n.Ready || n.Distribution != Distribution || n.DriverVersion != "570.86.15" || n.Labels["site"] != "lab" {
		t.Fatalf("node %+v", n)
	}
	if len(n.Devices) != 2 || n.Devices[0].DeviceClass != "h100-80gb-sxm" || n.Devices[0].DomainKey != "gpu-box-1" {
		t.Fatalf("devices %+v", n.Devices)
	}
	if n.Devices[0].Health != "healthy" || n.Devices[1].Health != "unhealthy" {
		t.Fatalf("health %s %s", n.Devices[0].Health, n.Devices[1].Health)
	}
}

func TestDiscoverUnknownCardGetsASlugClass(t *testing.T) {
	d := &Discoverer{Devices: &SMISource{Run: func(context.Context) ([]byte, error) { return fixture(t, "nvidia-smi-t4-legacy.xml"), nil }}, Machine: testMachine}
	snap, _ := d.Discover(context.Background())
	if got := snap.Nodes[0].Devices[0].DeviceClass; got != "tesla-t4" {
		t.Fatalf("t4 class %q", got)
	}
}

func TestDiscoverWithoutADriverIsANotReadyMember(t *testing.T) {
	at := time.Date(2026, 11, 27, 10, 0, 0, 0, time.UTC)
	d := &Discoverer{Devices: Fallback{&NVMLSource{}}, Machine: testMachine, Now: func() time.Time { return at }}
	snap, err := d.Discover(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	n := snap.Nodes[0]
	if n.Ready || len(n.Devices) != 0 || len(n.Conditions) != 1 || n.Conditions[0].Reason != "driver_unavailable" {
		t.Fatalf("node %+v", n)
	}
}

func TestEnrolWritesATokenOnlyRootReads(t *testing.T) {
	dir := t.TempDir()
	cfg := Config{Plane: "wss://hub.example/v1/hub", Cluster: "gpu-box-1", TokenPath: filepath.Join(dir, "etc", "token"), StateDir: filepath.Join(dir, "state")}
	if err := cfg.Validate(false); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(dir, "etc", "host.json")
	if err := Enrol(configPath, cfg, "wso1.secret\n"); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(cfg.TokenPath)
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("token mode %v %v", info.Mode().Perm(), err)
	}
	loaded, token, err := LoadConfig(configPath)
	if err != nil || token != "wso1.secret" || loaded.Cluster != "gpu-box-1" || loaded.Plane != cfg.Plane {
		t.Fatalf("load %+v %q %v", loaded, token, err)
	}
	if err := os.Chmod(cfg.TokenPath, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, _, err := LoadConfig(configPath); err == nil {
		t.Fatal("a world-readable token loaded")
	}
}

func TestEnrolRefusesBadInput(t *testing.T) {
	for _, c := range []Config{
		{Plane: "https://hub.example", Cluster: "a"},
		{Plane: "ws://hub.example", Cluster: "a"},
		{Plane: "wss://hub.example"},
	} {
		if err := c.Validate(false); err == nil {
			t.Fatalf("%+v validated", c)
		}
	}
	if err := Enrol(filepath.Join(t.TempDir(), "c"), Config{TokenPath: filepath.Join(t.TempDir(), "t")}, "hunter2"); err == nil {
		t.Fatal("a non-enrolment token was written")
	}
}

type sent struct {
	typ, subject string
	payload      any
}

type recorder struct{ frames []sent }

func (r *recorder) Send(t, subject string, payload any) error {
	r.frames = append(r.frames, sent{t, subject, payload})
	return nil
}

func (r *recorder) SendCorrelated(t, subject string, payload any, _ string) (string, error) {
	return "id", r.Send(t, subject, payload)
}

// TestObserverTickReportsAHost: the same observation loop the operator runs carries the host's
// snapshot out as cluster.observed (distribution host), one node, its devices and capacity.
func TestObserverTickReportsAHost(t *testing.T) {
	r := &recorder{}
	o := observe.New(&observe.Observer{
		ClusterID: "gpu-box-1", OperatorVersion: "dev", ReadOnly: true, Sender: r, Interval: time.Hour,
		Discoverer: &Discoverer{Devices: &NVMLSource{Lib: &fakeNVML{devices: []fakeDevice{h100(0, "GPU-a"), h100(1, "GPU-b")}}}, Machine: testMachine},
	})
	if err := o.Tick(context.Background()); err != nil {
		t.Fatal(err)
	}
	count := map[string]int{}
	for _, f := range r.frames {
		count[f.typ]++
		if c, ok := f.payload.(wire.ClusterObserved); ok && (c.Distribution != Distribution || c.Nodes != 1 || c.Devices != 2) {
			t.Fatalf("cluster.observed %+v", c)
		}
	}
	if count[observe.TypeClusterObserved] != 1 || count[observe.TypeNodeObserved] != 1 || count[observe.TypeDeviceObserved] != 2 || count[observe.TypeCapacityObserved] == 0 {
		t.Fatalf("frames %v", count)
	}
}

func TestMetricsTargetsListenIsLoopbackOnly(t *testing.T) {
	for addr, ok := range map[string]bool{"127.0.0.1:9479": true, "localhost:9479": true, "[::1]:9479": true, "0.0.0.0:9479": false, "10.0.0.1:9479": false, "9479": false} {
		c := Config{Serve: true, Gateway: "gw:443", AllowedImagePrefixes: []string{"ghcr.io/x"}, MetricsTargetsListen: addr}
		err := c.ValidateServing()
		if refused := err != nil && strings.Contains(err.Error(), "metrics_targets_listen"); refused == ok {
			t.Errorf("%s: err = %v", addr, err)
		}
	}
}
