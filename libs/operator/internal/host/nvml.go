package host

import (
	"context"
	"fmt"
	stdsync "sync"
)

// NVMLDevice is the slice of an NVML device handle this package reads.
type NVMLDevice interface {
	UUID() (string, error)
	Index() (int, error)
	Name() (string, error)
	MemoryTotalBytes() (uint64, error)
	// ECC reports whether ECC is on and the volatile corrected, volatile uncorrected and
	// aggregate uncorrected counts. An error means the card does not support ECC.
	ECC() (enabled bool, volCorrected, volUncorrected, aggUncorrected uint64, err error)
	// ThrottleReasons is the active clock-event bitmask decoded into names.
	ThrottleReasons() ([]string, error)
	MIGEnabled() (bool, error)
}

// NVMLLibrary is the slice of the NVML API this package calls. go-nvml implements it on Linux
// (nvml_linux.go); tests implement it with a fake, because CI has no GPUs.
type NVMLLibrary interface {
	Init() error
	DriverVersion() (string, error)
	CUDADriverVersion() (int, error)
	DeviceCount() (int, error)
	Device(index int) (NVMLDevice, error)
}

// NVMLSource reads the inventory through NVML. The library is initialised once, on the first
// read that succeeds in doing so, and kept for the life of the process.
type NVMLSource struct {
	Lib NVMLLibrary

	mu   stdsync.Mutex
	init bool
}

func (s *NVMLSource) Read(_ context.Context) (Inventory, error) {
	if s == nil || s.Lib == nil {
		return Inventory{}, fmt.Errorf("nvml: not available in this build")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.init {
		if err := s.Lib.Init(); err != nil {
			return Inventory{}, fmt.Errorf("nvml: init: %w", err)
		}
		s.init = true
	}
	driver, err := s.Lib.DriverVersion()
	if err != nil {
		return Inventory{}, fmt.Errorf("nvml: driver version: %w", err)
	}
	inv := Inventory{DriverVersion: driver}
	if cuda, err := s.Lib.CUDADriverVersion(); err == nil && cuda > 0 {
		inv.CUDAVersion = fmt.Sprintf("%d.%d", cuda/1000, (cuda%1000)/10)
	}
	count, err := s.Lib.DeviceCount()
	if err != nil {
		return Inventory{}, fmt.Errorf("nvml: device count: %w", err)
	}
	for i := 0; i < count; i++ {
		d, err := s.Lib.Device(i)
		if err != nil {
			return Inventory{}, fmt.Errorf("nvml: device %d: %w", i, err)
		}
		g, err := readNVMLDevice(i, d)
		if err != nil {
			return Inventory{}, err
		}
		inv.GPUs = append(inv.GPUs, g)
	}
	return inv, nil
}

func readNVMLDevice(i int, d NVMLDevice) (GPU, error) {
	g := GPU{Index: i}
	var err error
	if g.UUID, err = d.UUID(); err != nil {
		return GPU{}, fmt.Errorf("nvml: device %d uuid: %w", i, err)
	}
	if idx, err := d.Index(); err == nil {
		g.Index = idx
	}
	if g.Name, err = d.Name(); err != nil {
		return GPU{}, fmt.Errorf("nvml: device %d name: %w", i, err)
	}
	if total, err := d.MemoryTotalBytes(); err == nil {
		g.MemoryMiB = int64(total / (1 << 20))
	}
	// The optional readings degrade to absent instead of failing the device: a card without ECC
	// or MIG is still a card.
	if on, vc, vu, au, err := d.ECC(); err == nil {
		g.ECCEnabled, g.VolatileCorrected, g.VolatileUncorrected, g.AggregateUncorrected = on, int(vc), int(vu), int(au)
	}
	if reasons, err := d.ThrottleReasons(); err == nil {
		g.ThrottleReasons = reasons
	}
	if mig, err := d.MIGEnabled(); err == nil {
		g.MIGEnabled = mig
	}
	return g, nil
}
