// Package host is the supply half of a bare Linux GPU host: what the machine is and which
// accelerators it holds, read from NVML or, failing that, from nvidia-smi. It imports no
// Kubernetes code, so the wingsmith-host binary links without client-go.
package host

import (
	"context"
	"errors"
	"fmt"
)

// GPU is one accelerator as the driver reports it, before it is classed or named for the wire.
type GPU struct {
	UUID      string
	Index     int
	Name      string
	MemoryMiB int64

	ECCEnabled           bool
	VolatileCorrected    int
	VolatileUncorrected  int
	AggregateUncorrected int

	ThrottleReasons []string
	MIGEnabled      bool
}

// Inventory is one reading of the driver.
type Inventory struct {
	DriverVersion string
	CUDAVersion   string
	GPUs          []GPU
}

// Source reads the host's accelerators.
type Source interface {
	Read(ctx context.Context) (Inventory, error)
}

// Fallback tries each source in order and returns the first reading that succeeds. NVML first,
// nvidia-smi second: both read the same driver, but NVML is a library call and nvidia-smi is a
// process per tick whose XML shape drifts across driver releases.
type Fallback []Source

func (f Fallback) Read(ctx context.Context) (Inventory, error) {
	var errs []error
	for _, s := range f {
		if s == nil {
			continue
		}
		inv, err := s.Read(ctx)
		if err == nil {
			return inv, nil
		}
		errs = append(errs, err)
	}
	if len(errs) == 0 {
		return Inventory{}, fmt.Errorf("no device source configured")
	}
	return Inventory{}, errors.Join(errs...)
}

// throttleBits names NVML's clock-event reasons in the wire's snake_case. Idle is left out: a card
// clocked down because it has nothing to do is not throttled.
var throttleBits = []struct {
	bit  uint64
	name string
}{
	{0x2, "applications_clocks_setting"},
	{0x4, "sw_power_cap"},
	{0x8, "hw_slowdown"},
	{0x10, "sync_boost"},
	{0x20, "sw_thermal_slowdown"},
	{0x40, "hw_thermal_slowdown"},
	{0x80, "hw_power_brake_slowdown"},
	{0x100, "display_clock_setting"},
}

// ThrottleReasonNames decodes an NVML clock-event bitmask.
func ThrottleReasonNames(mask uint64) []string {
	out := []string{}
	for _, b := range throttleBits {
		if mask&b.bit != 0 {
			out = append(out, b.name)
		}
	}
	return out
}
