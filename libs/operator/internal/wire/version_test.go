package wire

import "testing"

func TestEveryEnvelopeVersionInTheRangeIsRead(t *testing.T) {
	for v := MinEnvelopeVersion; v <= EnvelopeVersion; v++ {
		if !SupportedEnvelope(v) {
			t.Errorf("version %d is inside [%d, %d] and must be read", v, MinEnvelopeVersion, EnvelopeVersion)
		}
	}
	for _, v := range []int{0, MinEnvelopeVersion - 1, EnvelopeVersion + 1} {
		if v >= MinEnvelopeVersion && v <= EnvelopeVersion {
			continue
		}
		if SupportedEnvelope(v) {
			t.Errorf("version %d is outside [%d, %d] and must be refused", v, MinEnvelopeVersion, EnvelopeVersion)
		}
	}
}
