package host

import (
	"bytes"
	"context"
	"encoding/xml"
	"fmt"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

// SMISource reads the inventory from `nvidia-smi -q -x`. It is the fallback for a host where
// NVML cannot be loaded by this process (a static build, an unusual library path) but the
// driver's own tool works.
type SMISource struct {
	// Run returns the XML. Nil runs nvidia-smi from PATH.
	Run func(ctx context.Context) ([]byte, error)
}

func (s *SMISource) Read(ctx context.Context) (Inventory, error) {
	run := s.Run
	if run == nil {
		run = runSMI
	}
	raw, err := run(ctx)
	if err != nil {
		return Inventory{}, fmt.Errorf("nvidia-smi: %w", err)
	}
	return ParseSMI(raw)
}

func runSMI(ctx context.Context) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	var stderr bytes.Buffer
	cmd := exec.CommandContext(ctx, "nvidia-smi", "-q", "-x")
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("%w: %s", err, strings.TrimSpace(stderr.String()))
	}
	return out, nil
}

type smiLog struct {
	Driver string   `xml:"driver_version"`
	CUDA   string   `xml:"cuda_version"`
	GPUs   []smiGPU `xml:"gpu"`
}

type smiGPU struct {
	ProductName string `xml:"product_name"`
	UUID        string `xml:"uuid"`
	MinorNumber string `xml:"minor_number"`
	MIG         struct {
		Current string `xml:"current_mig"`
	} `xml:"mig_mode"`
	FB struct {
		Total string `xml:"total"`
	} `xml:"fb_memory_usage"`
	ECCMode struct {
		Current string `xml:"current_ecc"`
	} `xml:"ecc_mode"`
	ECCErrors struct {
		Volatile  smiECCCounts `xml:"volatile"`
		Aggregate smiECCCounts `xml:"aggregate"`
	} `xml:"ecc_errors"`
	// Renamed from clocks_throttle_reasons to clocks_event_reasons in driver 535; both are read.
	Throttle smiReasons `xml:"clocks_throttle_reasons"`
	Events   smiReasons `xml:"clocks_event_reasons"`
}

type smiECCCounts struct {
	// Pre-Ampere drivers split counts by memory; Ampere and later report dram/sram.
	SingleBit struct {
		Total string `xml:"total"`
	} `xml:"single_bit"`
	DoubleBit struct {
		Total string `xml:"total"`
	} `xml:"double_bit"`
	SRAMCorrectable   string `xml:"sram_correctable"`
	SRAMUncorrectable string `xml:"sram_uncorrectable"`
	DRAMCorrectable   string `xml:"dram_correctable"`
	DRAMUncorrectable string `xml:"dram_uncorrectable"`
}

func (c smiECCCounts) corrected() int {
	return atoiNA(c.SingleBit.Total) + atoiNA(c.SRAMCorrectable) + atoiNA(c.DRAMCorrectable)
}

func (c smiECCCounts) uncorrected() int {
	return atoiNA(c.DoubleBit.Total) + atoiNA(c.SRAMUncorrectable) + atoiNA(c.DRAMUncorrectable)
}

type smiReasons struct {
	Items []struct {
		XMLName xml.Name
		Value   string `xml:",chardata"`
	} `xml:",any"`
}

// active names the reasons reported Active, with the element prefix stripped so both spellings
// land on the same names NVML's bitmask decodes to.
func (r smiReasons) active() []string {
	var out []string
	for _, item := range r.Items {
		if strings.TrimSpace(item.Value) != "Active" {
			continue
		}
		name := item.XMLName.Local
		for _, prefix := range []string{"clocks_event_reason_", "clocks_throttle_reason_"} {
			name = strings.TrimPrefix(name, prefix)
		}
		if name == "gpu_idle" {
			continue
		}
		out = append(out, name)
	}
	return out
}

// ParseSMI reads `nvidia-smi -q -x` output.
func ParseSMI(raw []byte) (Inventory, error) {
	var log smiLog
	dec := xml.NewDecoder(bytes.NewReader(raw))
	// The document declares a DTD nvidia-smi ships beside itself; nothing here needs it.
	dec.Strict = false
	if err := dec.Decode(&log); err != nil {
		return Inventory{}, fmt.Errorf("nvidia-smi: parse: %w", err)
	}
	inv := Inventory{DriverVersion: strings.TrimSpace(log.Driver), CUDAVersion: strings.TrimSpace(log.CUDA)}
	for i, g := range log.GPUs {
		gpu := GPU{
			UUID:                 strings.TrimSpace(g.UUID),
			Index:                i,
			Name:                 strings.TrimSpace(g.ProductName),
			MemoryMiB:            mib(g.FB.Total),
			ECCEnabled:           strings.TrimSpace(g.ECCMode.Current) == "Enabled",
			VolatileCorrected:    g.ECCErrors.Volatile.corrected(),
			VolatileUncorrected:  g.ECCErrors.Volatile.uncorrected(),
			AggregateUncorrected: g.ECCErrors.Aggregate.uncorrected(),
			MIGEnabled:           strings.TrimSpace(g.MIG.Current) == "Enabled",
			ThrottleReasons:      append(g.Throttle.active(), g.Events.active()...),
		}
		if gpu.ThrottleReasons == nil {
			gpu.ThrottleReasons = []string{}
		}
		if n, err := strconv.Atoi(strings.TrimSpace(g.MinorNumber)); err == nil {
			gpu.Index = n
		}
		if gpu.UUID == "" {
			return Inventory{}, fmt.Errorf("nvidia-smi: gpu %d has no uuid", i)
		}
		inv.GPUs = append(inv.GPUs, gpu)
	}
	return inv, nil
}

// mib reads "81559 MiB".
func mib(s string) int64 {
	n, _ := strconv.ParseInt(strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(s), "MiB")), 10, 64)
	return n
}

// atoiNA reads a counter nvidia-smi may print as "N/A".
func atoiNA(s string) int {
	n, _ := strconv.Atoi(strings.TrimSpace(s))
	return n
}
