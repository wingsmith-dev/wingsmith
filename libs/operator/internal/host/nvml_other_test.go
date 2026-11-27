//go:build !(linux && cgo)

package host

import (
	"context"
	"testing"
)

// A CGO_ENABLED=0 build has no NVML; the source wingsmith-host builds must still report the cards
// through nvidia-smi.
func TestStaticBuildReportsDevicesThroughSMI(t *testing.T) {
	if SystemNVML() != nil {
		t.Fatal("non-cgo build linked NVML")
	}
	smi := &SMISource{Run: func(context.Context) ([]byte, error) { return fixture(t, "nvidia-smi-h100x2.xml"), nil }}
	inv, err := Fallback{&NVMLSource{Lib: SystemNVML()}, smi}.Read(context.Background())
	if err != nil || len(inv.GPUs) != 2 || inv.DriverVersion == "" {
		t.Fatalf("static build fallback: %v %+v", err, inv)
	}
}
