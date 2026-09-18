package serving

import (
	"bytes"
	"encoding/json"
	"reflect"
	"testing"
)

var spliceScope = cacheScope{Tenant: "tnt_a", Caller: "org_1"}

// expectedSalted is what any correct salting of body decodes to: the same object with
// nvext.cache_salt set to the scope's identity, folding in a caller salt if there was one.
func expectedSalted(t *testing.T, body string) map[string]any {
	t.Helper()
	var fields map[string]json.RawMessage
	if err := json.Unmarshal([]byte(body), &fields); err != nil {
		t.Fatalf("fixture is not an object: %v", err)
	}
	var hints map[string]json.RawMessage
	if raw, ok := fields["nvext"]; ok {
		if err := json.Unmarshal(raw, &hints); err != nil {
			t.Fatalf("fixture nvext: %v", err)
		}
	}
	encoded, err := saltedHints(hints, cacheSalt(spliceScope, "", false, hints))
	if err != nil {
		t.Fatal(err)
	}
	fields["nvext"] = encoded
	raw, _ := json.Marshal(fields)
	var out map[string]any
	_ = json.Unmarshal(raw, &out)
	return out
}

func TestTheSplicedSaltDecodesToWhatAReEncodeWouldHaveSent(t *testing.T) {
	for name, body := range map[string]string{
		"no nvext":            `{"model":"chat","messages":[{"role":"user","content":"hi"}]}`,
		"empty object":        `{}`,
		"spaced empty object": " { \n } ",
		"nvext first":         `{"nvext":{"annotations":["x"]},"model":"chat"}`,
		"nvext last":          `{"model":"chat","nvext":{"cache_salt":"mine"}}`,
		"nvext null":          `{"model":"chat","nvext":null}`,
		"escaped key":         `{"model":"chat","nvext":{"cache_salt":"mine"}}`,
		"braces in strings":   `{"model":"c}h{a]t[","messages":[{"content":"\"nvext\":{\"cache_salt\":\"x\"}"}]}`,
		"escaped backslashes": `{"model":"chat","messages":[{"content":"ends in a slash \\\\"}],"nvext":{}}`,
		"literals and spaces": "{ \"model\" : \"chat\" ,\n\t\"stream\" : true , \"n\":1, \"stop\": null , \"x\":-1.5e3 }",
		"nested nvext name":   `{"model":"chat","metadata":{"nvext":{"cache_salt":"not this one"}}}`,
	} {
		t.Run(name, func(t *testing.T) {
			out, written, err := withCacheSalt([]byte(body), spliceScope)
			if err != nil || !written {
				t.Fatalf("written=%v err=%v", written, err)
			}
			var got map[string]any
			if err := json.Unmarshal(out, &got); err != nil {
				t.Fatalf("not JSON: %v: %s", err, out)
			}
			if want := expectedSalted(t, body); !reflect.DeepEqual(got, want) {
				t.Fatalf("got  %s\nwant %v", out, want)
			}
			// A decoded map keeps the last of two copies, so count the top-level copies directly.
			if _, spans, ok := topLevelValues(out, "nvext"); !ok || len(spans) != 1 {
				t.Fatalf("%d top-level nvext fields in %s", len(spans), out)
			}
		})
	}
}

// The prompt is forwarded as the bytes that arrived: a re-encode would rewrite this escape to
// the character it names and sort the keys.
func TestTheBytesOutsideNvextAreForwardedUntouched(t *testing.T) {
	prompt := `"messages":[{"role":"user","content":"café <b>"}]`
	body := `{"model":"chat",` + prompt + `,"nvext":{"annotations":["a"]},"stream":true}`
	out, written, err := withCacheSalt([]byte(body), spliceScope)
	if err != nil || !written {
		t.Fatalf("written=%v err=%v", written, err)
	}
	if !bytes.HasPrefix(out, []byte(`{"model":"chat",`+prompt+`,"nvext":{`)) {
		t.Fatalf("bytes before nvext changed: %s", out)
	}
	if !bytes.HasSuffix(out, []byte(`},"stream":true}`)) {
		t.Fatalf("bytes after nvext changed: %s", out)
	}
}

// A repeated nvext is left to the full re-encode, which collapses it to one copy as it always has.
func TestARepeatedNvextIsStillCollapsedToOneCopy(t *testing.T) {
	body := `{"model":"chat","nvext":{"cache_salt":"first"},"nvext":{"cache_salt":"second"}}`
	out, written, err := withCacheSalt([]byte(body), spliceScope)
	if err != nil || !written {
		t.Fatalf("written=%v err=%v", written, err)
	}
	if n := bytes.Count(out, []byte(`"nvext"`)); n != 1 {
		t.Fatalf("%d nvext fields in %s", n, out)
	}
}

func TestANvextThatIsNotAnObjectIsStillLeftForTheRuntimeToRefuse(t *testing.T) {
	body := `{"model":"chat","nvext":"steer me"}`
	out, written, err := withCacheSalt([]byte(body), spliceScope)
	if err != nil || written || string(out) != body {
		t.Fatalf("written=%v err=%v out=%s", written, err, out)
	}
}

func TestTheScanRefusesWhatItCannotRead(t *testing.T) {
	for _, body := range []string{``, `[]`, `"x"`, `{"a"}`, `{"a":1,}`, `{"a":"unterminated}`, `{"a":[1,2}`, `{"a":1`} {
		if _, _, ok := topLevelValues([]byte(body), "nvext"); ok {
			t.Errorf("%q was read as an object", body)
		}
	}
}
