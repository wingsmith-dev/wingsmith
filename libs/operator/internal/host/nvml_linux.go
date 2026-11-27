//go:build linux && cgo

package host

import (
	"github.com/NVIDIA/go-nvml/pkg/nvml"
)

// SystemNVML is the host's NVML, loaded from libnvidia-ml.so.1 at Init. A host without the
// driver fails Init, and Fallback moves on to nvidia-smi.
func SystemNVML() NVMLLibrary { return &goNVML{lib: nvml.New()} }

type goNVML struct{ lib nvml.Interface }

func check(r nvml.Return) error {
	if r == nvml.SUCCESS {
		return nil
	}
	return r
}

func (g *goNVML) Init() error { return check(g.lib.Init()) }

func (g *goNVML) DriverVersion() (string, error) {
	v, r := g.lib.SystemGetDriverVersion()
	return v, check(r)
}

func (g *goNVML) CUDADriverVersion() (int, error) {
	v, r := g.lib.SystemGetCudaDriverVersion()
	return v, check(r)
}

func (g *goNVML) DeviceCount() (int, error) {
	n, r := g.lib.DeviceGetCount()
	return n, check(r)
}

func (g *goNVML) Device(i int) (NVMLDevice, error) {
	d, r := g.lib.DeviceGetHandleByIndex(i)
	if err := check(r); err != nil {
		return nil, err
	}
	return goDevice{d}, nil
}

type goDevice struct{ d nvml.Device }

func (d goDevice) UUID() (string, error) { v, r := d.d.GetUUID(); return v, check(r) }
func (d goDevice) Index() (int, error)   { v, r := d.d.GetIndex(); return v, check(r) }
func (d goDevice) Name() (string, error) { v, r := d.d.GetName(); return v, check(r) }

func (d goDevice) MemoryTotalBytes() (uint64, error) {
	m, r := d.d.GetMemoryInfo()
	return m.Total, check(r)
}

func (d goDevice) ECC() (bool, uint64, uint64, uint64, error) {
	current, _, r := d.d.GetEccMode()
	if err := check(r); err != nil {
		return false, 0, 0, 0, err
	}
	vc, _ := d.d.GetTotalEccErrors(nvml.MEMORY_ERROR_TYPE_CORRECTED, nvml.VOLATILE_ECC)
	vu, _ := d.d.GetTotalEccErrors(nvml.MEMORY_ERROR_TYPE_UNCORRECTED, nvml.VOLATILE_ECC)
	au, _ := d.d.GetTotalEccErrors(nvml.MEMORY_ERROR_TYPE_UNCORRECTED, nvml.AGGREGATE_ECC)
	return current == nvml.FEATURE_ENABLED, vc, vu, au, nil
}

func (d goDevice) ThrottleReasons() ([]string, error) {
	mask, r := d.d.GetCurrentClocksEventReasons()
	if err := check(r); err != nil {
		return nil, err
	}
	return ThrottleReasonNames(mask), nil
}

func (d goDevice) MIGEnabled() (bool, error) {
	current, _, r := d.d.GetMigMode()
	if err := check(r); err != nil {
		return false, err
	}
	return current == nvml.DEVICE_MIG_ENABLE, nil
}
