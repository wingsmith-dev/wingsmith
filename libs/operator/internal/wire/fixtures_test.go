package wire

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// fixtureDir is a symlink to libs/packages/contract/fixtures. It is package-local on purpose: Go's test
// result cache only tracks files opened inside the package directory, so a corpus referenced by
// a relative path outside the module would let a stale PASS survive a fixture edit.
const fixtureDir = "fixtures"

func fixturePaths(t *testing.T) []string {
	t.Helper()
	paths, err := filepath.Glob(filepath.Join(fixtureDir, "*.json"))
	if err != nil {
		t.Fatalf("glob fixtures: %v", err)
	}
	if len(paths) == 0 {
		t.Fatal("fixture corpus is empty")
	}
	return paths
}

func fixtureName(path string) string {
	return strings.TrimSuffix(filepath.Base(path), ".json")
}

// normalise drops null-valued keys recursively. PROTOCOL §2 treats an absent optional field and
// an explicit null as the same thing, so semantic equality must too.
func normalise(value any) any {
	switch typed := value.(type) {
	case map[string]any:
		out := make(map[string]any, len(typed))
		for key, item := range typed {
			if item == nil {
				continue
			}
			out[key] = normalise(item)
		}
		return out
	case []any:
		out := make([]any, 0, len(typed))
		for _, item := range typed {
			out = append(out, normalise(item))
		}
		return out
	default:
		return value
	}
}

func TestEveryTypeHasAFixture(t *testing.T) {
	present := map[string]bool{}
	for _, path := range fixturePaths(t) {
		present[fixtureName(path)] = true
	}
	for name := range MessageTypes {
		if !present[name] {
			t.Errorf("registered type %q has no fixture", name)
		}
	}
}

func TestEveryFixtureHasAType(t *testing.T) {
	for _, path := range fixturePaths(t) {
		if _, ok := MessageTypes[fixtureName(path)]; !ok {
			t.Errorf("fixture %q has no registered type", fixtureName(path))
		}
	}
}

func TestFixtureRoundTrips(t *testing.T) {
	for _, path := range fixturePaths(t) {
		t.Run(fixtureName(path), func(t *testing.T) {
			data, err := os.ReadFile(path)
			if err != nil {
				t.Fatalf("read: %v", err)
			}

			frame, err := Decode(data)
			if err != nil {
				t.Fatalf("decode: %v", err)
			}
			if frame.Type != fixtureName(path) {
				t.Fatalf("fixture filename %q does not equal envelope type %q", fixtureName(path), frame.Type)
			}

			encoded, err := Encode(frame)
			if err != nil {
				t.Fatalf("encode: %v", err)
			}

			var original, reencoded any
			if err := json.Unmarshal(data, &original); err != nil {
				t.Fatalf("unmarshal original: %v", err)
			}
			if err := json.Unmarshal(encoded, &reencoded); err != nil {
				t.Fatalf("unmarshal reencoded: %v", err)
			}
			if !reflect.DeepEqual(normalise(reencoded), normalise(original)) {
				t.Errorf("round trip diverged\n original: %s\nreencoded: %s", data, encoded)
			}
		})
	}
}

// TestFixtureFieldsAreAllKnown fails when a fixture carries a field this side would silently
// discard. Decode itself is permissive, per PROTOCOL §2; the contract test is not.
func TestFixtureFieldsAreAllKnown(t *testing.T) {
	for _, path := range fixturePaths(t) {
		t.Run(fixtureName(path), func(t *testing.T) {
			data, err := os.ReadFile(path)
			if err != nil {
				t.Fatalf("read: %v", err)
			}

			var env Envelope
			envDecoder := json.NewDecoder(bytes.NewReader(data))
			envDecoder.DisallowUnknownFields()
			if err := envDecoder.Decode(&env); err != nil {
				t.Fatalf("envelope has fields unknown to wire.Envelope: %v", err)
			}

			build, ok := MessageTypes[env.Type]
			if !ok {
				t.Fatalf("unregistered type %q", env.Type)
			}
			msg := build()
			payloadDecoder := json.NewDecoder(bytes.NewReader(env.Payload))
			payloadDecoder.DisallowUnknownFields()
			if err := payloadDecoder.Decode(msg); err != nil {
				t.Fatalf("payload has fields unknown to the Go type: %v", err)
			}
		})
	}
}

func TestEnvelopeConventions(t *testing.T) {
	for _, path := range fixturePaths(t) {
		t.Run(fixtureName(path), func(t *testing.T) {
			data, err := os.ReadFile(path)
			if err != nil {
				t.Fatalf("read: %v", err)
			}
			var env Envelope
			if err := json.Unmarshal(data, &env); err != nil {
				t.Fatalf("unmarshal: %v", err)
			}
			if env.V != EnvelopeVersion {
				t.Errorf("v = %d, want %d", env.V, EnvelopeVersion)
			}
			if len(env.ID) != 26 {
				t.Errorf("id %q is not a 26-character ULID", env.ID)
			}
			if !TimestampPattern.MatchString(env.Ts) {
				t.Errorf("ts %q is not RFC 3339 UTC with milliseconds", env.Ts)
			}
			if len(env.Payload) == 0 || env.Payload[0] != '{' {
				t.Errorf("payload must be an object, got %s", env.Payload)
			}
			if env.Sequenced() {
				if env.Seq == nil || *env.Seq < 1 {
					t.Errorf("sequenced frame %q must carry seq >= 1", env.Type)
				}
			} else if env.Seq != nil {
				t.Errorf("unsequenced frame %q must not carry seq", env.Type)
			}
		})
	}
}

func TestEntityIDsArePrefixedULIDs(t *testing.T) {
	for _, path := range fixturePaths(t) {
		t.Run(fixtureName(path), func(t *testing.T) {
			data, err := os.ReadFile(path)
			if err != nil {
				t.Fatalf("read: %v", err)
			}
			var doc map[string]any
			if err := json.Unmarshal(data, &doc); err != nil {
				t.Fatalf("unmarshal: %v", err)
			}
			var walk func(any)
			walk = func(value any) {
				switch typed := value.(type) {
				case map[string]any:
					for _, item := range typed {
						walk(item)
					}
				case []any:
					for _, item := range typed {
						walk(item)
					}
				case string:
					if len(typed) < 4 {
						return
					}
					for _, prefix := range IDPrefixes {
						if strings.HasPrefix(typed, prefix) && len(typed) != 30 {
							t.Errorf("%q is not a prefixed 26-character ULID", typed)
						}
					}
				}
			}
			walk(doc["payload"])
		})
	}
}

func TestDecodeRejectsUnregisteredType(t *testing.T) {
	frame := []byte(`{"v":1,"type":"nonsense.verb","id":"01J9X2E5G7QK8V3B0YHN4TZC6M","ts":"2026-08-19T14:02:11.482Z","payload":{}}`)
	if _, err := Decode(frame); err == nil {
		t.Fatal("expected an error for an unregistered type")
	}
}
