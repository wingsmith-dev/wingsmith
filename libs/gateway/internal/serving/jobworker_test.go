package serving

import (
	"bytes"
	"encoding/base64"
	"io"
	"os"
	"runtime"
	"strings"
	"testing"
)

func spoolOf(t *testing.T, body string) *os.File {
	t.Helper()
	f, err := os.CreateTemp(t.TempDir(), "spool-*")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteString(body); err != nil {
		t.Fatal(err)
	}
	return f
}

func TestOutputOfDecodesTheFirstB64AndKeepsOtherResponses(t *testing.T) {
	cases := []struct{ body, ct, want, wantType string }{
		{`{"created":1,"data":[{"revised_prompt":{"a":[1]},"b64_json":"TVA0\/\/\/\/"},{"b64_json":"QkI="}]}`, "application/json", "MP4\xff\xff\xff", "video/mp4"},
		{`{"data":[{"url":"https://x"}]}`, "application/json", `{"data":[{"url":"https://x"}]}`, "application/json"},
		{"RAW", "audio/mpeg", "RAW", "audio/mpeg"},
	}
	for _, c := range cases {
		out, ct, size, err := outputOf(spoolOf(t, c.body), c.ct, mediaVideo)
		if err != nil {
			t.Fatalf("%s: %v", c.body, err)
		}
		got, _ := io.ReadAll(out)
		if string(got) != c.want || ct != c.wantType || size != int64(len(c.want)) {
			t.Fatalf("%s: got %q %s %d", c.body, got, ct, size)
		}
	}
	for _, bad := range []string{`{"data":[{"b64_json":"!!!!"}]}`, `{"data":[{"b64_json":"QUJD`, `{"data":[{"b64_json":1}]}`} {
		if _, _, _, err := outputOf(spoolOf(t, bad), "application/json", mediaVideo); err == nil {
			t.Fatalf("%s: no error", bad)
		}
	}
}

func TestOutputOfStreamsALargeB64(t *testing.T) {
	const size = 64 << 20
	f, err := os.CreateTemp(t.TempDir(), "spool-*")
	if err != nil {
		t.Fatal(err)
	}
	_, _ = f.WriteString(`{"data":[{"b64_json":"`)
	enc := base64.NewEncoder(base64.StdEncoding, f)
	chunk := bytes.Repeat([]byte{7}, 1<<20)
	for range size >> 20 {
		_, _ = enc.Write(chunk)
	}
	_ = enc.Close()
	_, _ = f.WriteString(`"}]}`)

	var before, after runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&before)
	out, _, n, err := outputOf(f, "application/json", mediaVideo)
	runtime.ReadMemStats(&after)
	if err != nil || n != size {
		t.Fatalf("outputOf = %d %v", n, err)
	}
	if grew := after.TotalAlloc - before.TotalAlloc; grew > 8<<20 {
		t.Fatalf("decoding allocated %d bytes", grew)
	}
	head := make([]byte, 4)
	_, _ = io.ReadFull(out, head)
	if !strings.HasPrefix(string(head), "\x07\x07") {
		t.Fatalf("head %q", head)
	}
	_ = out.Close()
	_ = os.Remove(out.Name())
}
