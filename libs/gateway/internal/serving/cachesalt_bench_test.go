package serving

import (
	"strings"
	"testing"
)

// largeTextBody is a chat request whose one message is about n bytes of prose: the shape of a
// long-context prompt, which is every byte of the cost withCacheSalt adds to a text request.
func largeTextBody(n int, nvext string) []byte {
	prose := strings.Repeat("The quick brown fox jumps over the lazy dog. ", n/45+1)[:n]
	body := `{"model":"chat","messages":[{"role":"system","content":"You are terse."},` +
		`{"role":"user","content":"` + prose + `"}],"max_tokens":256,"stream":true`
	if nvext != "" {
		body += `,"nvext":` + nvext
	}
	return []byte(body + `}`)
}

func benchmarkCacheSalt(b *testing.B, body []byte) {
	scope := cacheScope{Tenant: "tnt_0123456789abcdef", Caller: "org_42"}
	b.SetBytes(int64(len(body)))
	b.ReportAllocs()
	for b.Loop() {
		if _, written, err := withCacheSalt(body, scope); err != nil || !written {
			b.Fatalf("written=%v err=%v", written, err)
		}
	}
}

// BenchmarkCacheSaltLargeText is the per-request cost of salting a 4 MB text prompt, roughly a
// million tokens, which every text generation request on a scoped endpoint pays.
func BenchmarkCacheSaltLargeText(b *testing.B) {
	benchmarkCacheSalt(b, largeTextBody(4<<20, ""))
}

func BenchmarkCacheSaltLargeTextWithNvext(b *testing.B) {
	benchmarkCacheSalt(b, largeTextBody(4<<20, `{"annotations":["request_id"],"cache_salt":"mine"}`))
}

func BenchmarkCacheSaltSmallText(b *testing.B) {
	benchmarkCacheSalt(b, largeTextBody(2<<10, ""))
}
