//go:build !(linux && cgo)

package host

// SystemNVML is nil where go-nvml cannot link (no cgo, or not Linux); NVMLSource then reports
// itself unavailable and Fallback reads nvidia-smi instead.
func SystemNVML() NVMLLibrary { return nil }
