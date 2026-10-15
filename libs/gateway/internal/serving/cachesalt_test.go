package serving

import (
	"encoding/base64"
	"encoding/json"
	"strings"
	"testing"
)

// imageBody is a chat request carrying one inline image of the given bytes.
func imageBody(t *testing.T, pixels string) string {
	t.Helper()
	url := "data:image/png;base64," + base64.StdEncoding.EncodeToString([]byte(pixels))
	return `{"model":"chat","messages":[{"role":"user","content":[` +
		`{"type":"image_url","image_url":{"url":"` + url + `"}},` +
		`{"type":"text","text":"what is this?"}]}]}`
}

// imageQuestionBody is a chat request carrying one inline image and one
// question about it, so two bodies can differ in the question alone.
func imageQuestionBody(t *testing.T, pixels, question string) string {
	t.Helper()
	url := "data:image/png;base64," + base64.StdEncoding.EncodeToString([]byte(pixels))
	return `{"model":"chat","messages":[{"role":"user","content":[` +
		`{"type":"image_url","image_url":{"url":"` + url + `"}},` +
		`{"type":"text","text":` + mustJSON(t, question) + `}]}]}`
}

// mustJSON quotes a string the way the wire carries it.
func mustJSON(t *testing.T, value string) string {
	t.Helper()
	encoded, err := json.Marshal(value)
	if err != nil {
		t.Fatalf("marshal %q: %v", value, err)
	}
	return string(encoded)
}

// saltOf returns the cache_salt the gateway wrote, and whether it wrote one.
func saltOf(t *testing.T, body string) (string, bool) {
	t.Helper()
	out, written, err := withCacheSalt([]byte(body), cacheScope{})
	if err != nil {
		t.Fatalf("withCacheSalt: %v", err)
	}
	if !written {
		return "", false
	}
	var fields struct {
		NvExt struct {
			CacheSalt string `json:"cache_salt"`
		} `json:"nvext"`
	}
	if err := json.Unmarshal(out, &fields); err != nil {
		t.Fatalf("salted body is not JSON: %v: %s", err, out)
	}
	if fields.NvExt.CacheSalt == "" {
		t.Fatalf("reported a salt but wrote none: %s", out)
	}
	return fields.NvExt.CacheSalt, true
}

// TestTwoDifferentImagesNeverShareACacheIdentity is the defect this exists to
// close. The prompts are identical in every byte but the image, which is exactly
// the pair that expands to one placeholder run and one block hash on a
// disaggregated decode worker.
func TestTwoDifferentImagesNeverShareACacheIdentity(t *testing.T) {
	first, ok := saltOf(t, imageBody(t, "the first image's pixels"))
	if !ok {
		t.Fatal("no salt written for a request carrying an image")
	}
	second, ok := saltOf(t, imageBody(t, "the second image's pixels"))
	if !ok {
		t.Fatal("no salt written for a request carrying an image")
	}
	if first == second {
		t.Errorf("two different images share the salt %q", first)
	}
}

// TestTheSameImageKeepsTheReuseTheCacheExistsFor is the other half of the claim:
// the salt must not be a per-request nonce, or every repeat re-prefills.
func TestTheSameImageKeepsTheReuseTheCacheExistsFor(t *testing.T) {
	first, _ := saltOf(t, imageBody(t, "identical pixels"))
	second, _ := saltOf(t, imageBody(t, "identical pixels"))
	if first != second {
		t.Errorf("one image hashed two ways: %q and %q", first, second)
	}
}

// TestATextRequestIsForwardedAsTheBytesThatArrived pins that the text path pays
// nothing and grows no nvext it did not have.
func TestATextRequestIsForwardedAsTheBytesThatArrived(t *testing.T) {
	body := `{"model":"chat","messages":[{"role":"user","content":"no media here"}]}`
	out, written, err := withCacheSalt([]byte(body), cacheScope{})
	if err != nil {
		t.Fatalf("withCacheSalt: %v", err)
	}
	if written {
		t.Errorf("wrote a salt for a request carrying no media: %s", out)
	}
	if string(out) != body {
		t.Errorf("body was rewritten:\n got %s\nwant %s", out, body)
	}
	if hasMediaPart([]byte(body)) {
		t.Error("the cheap guard claimed media in a text request")
	}
}

// TestACallersConstantSaltCannotReintroduceTheCollision is the reason a caller's
// own salt is folded in rather than honoured as it stands. A caller sending one
// salt across a corpus would otherwise put every image back on one identity.
func TestACallersConstantSaltCannotReintroduceTheCollision(t *testing.T) {
	withSalt := func(pixels string) string {
		body := imageBody(t, pixels)
		injected := strings.TrimSuffix(body, "}") + `,"nvext":{"cache_salt":"one-constant"}}`
		salt, ok := saltOf(t, injected)
		if !ok {
			t.Fatal("no salt written for a request carrying an image")
		}
		return salt
	}
	if first, second := withSalt("first image"), withSalt("second image"); first == second {
		t.Errorf("a constant caller salt collapsed two images onto %q", first)
	}
}

// TestACallersOwnIsolationSurvivesTheFold is the same field read the other way:
// two callers sending one image and different salts stay apart.
func TestACallersOwnIsolationSurvivesTheFold(t *testing.T) {
	body := imageBody(t, "one image")
	mine := strings.TrimSuffix(body, "}") + `,"nvext":{"cache_salt":"acme"}}`
	theirs := strings.TrimSuffix(body, "}") + `,"nvext":{"cache_salt":"globex"}}`
	if a, b := mustSalt(t, mine), mustSalt(t, theirs); a == b {
		t.Errorf("two callers' salts folded to one identity %q", a)
	}
}

func mustSalt(t *testing.T, body string) string {
	t.Helper()
	salt, ok := saltOf(t, body)
	if !ok {
		t.Fatal("no salt written for a request carrying an image")
	}
	return salt
}

// TestTheRestOfNvextSurvivesTheSalt pins that writing into nvext is additive,
// including for a request that had no nvext at all.
func TestTheRestOfNvextSurvivesTheSalt(t *testing.T) {
	body := strings.TrimSuffix(imageBody(t, "pixels"), "}") +
		`,"nvext":{"max_thinking_tokens":512}}`
	out, written, err := withCacheSalt([]byte(body), cacheScope{})
	if err != nil {
		t.Fatalf("withCacheSalt: %v", err)
	}
	if !written {
		t.Fatal("no salt written for a request carrying an image")
	}
	var fields struct {
		Model string `json:"model"`
		NvExt struct {
			CacheSalt string `json:"cache_salt"`
			MaxThink  int    `json:"max_thinking_tokens"`
		} `json:"nvext"`
	}
	if err := json.Unmarshal(out, &fields); err != nil {
		t.Fatalf("salted body is not JSON: %v: %s", err, out)
	}
	if fields.Model != "chat" {
		t.Errorf("model = %q, want it untouched", fields.Model)
	}
	if fields.NvExt.MaxThink != 512 {
		t.Errorf("max_thinking_tokens = %d, want 512 kept", fields.NvExt.MaxThink)
	}
	if fields.NvExt.CacheSalt == "" {
		t.Error("cache_salt was not written")
	}
}

// TestMediaOfOneKindIsNotMediaOfAnother pins the part kind into the identity, so
// a video and an image carrying the same bytes are two things.
func TestMediaOfOneKindIsNotMediaOfAnother(t *testing.T) {
	payload := base64.StdEncoding.EncodeToString([]byte("identical bytes"))
	asImage := `{"model":"chat","messages":[{"role":"user","content":[` +
		`{"type":"image_url","image_url":{"url":"data:image/png;base64,` + payload + `"}}]}]}`
	asVideo := `{"model":"chat","messages":[{"role":"user","content":[` +
		`{"type":"video_url","video_url":{"url":"data:video/mp4;base64,` + payload + `"}}]}]}`
	if a, b := mustSalt(t, asImage), mustSalt(t, asVideo); a == b {
		t.Errorf("an image and a video share the identity %q", a)
	}
}

// TestAReferencedImageIsStillGivenAnIdentity is the case that would otherwise
// fail silently: no bytes at this hop, so the reference is the identity. A
// request that derives no salt is a request back on the defect.
func TestAReferencedImageIsStillGivenAnIdentity(t *testing.T) {
	remote := func(url string) string {
		return `{"model":"chat","messages":[{"role":"user","content":[` +
			`{"type":"image_url","image_url":{"url":"` + url + `"}}]}]}`
	}
	first := mustSalt(t, remote("https://example.test/first.png"))
	second := mustSalt(t, remote("https://example.test/second.png"))
	if first == second {
		t.Errorf("two referenced images share the identity %q", first)
	}
	if again := mustSalt(t, remote("https://example.test/first.png")); again != first {
		t.Errorf("one reference hashed two ways: %q and %q", first, again)
	}
}

// TestTwoImagesAreNotOneImageTwice pins the length prefixing: no rearrangement
// of parts may spell another request's digest.
func TestTwoImagesAreNotOneImageTwice(t *testing.T) {
	part := func(pixels string) string {
		return `{"type":"image_url","image_url":{"url":"data:image/png;base64,` +
			base64.StdEncoding.EncodeToString([]byte(pixels)) + `"}}`
	}
	pair := `{"model":"chat","messages":[{"role":"user","content":[` +
		part("ab") + `,` + part("cd") + `]}]}`
	joined := `{"model":"chat","messages":[{"role":"user","content":[` +
		part("abcd") + `]}]}`
	if a, b := mustSalt(t, pair), mustSalt(t, joined); a == b {
		t.Errorf("two images and one concatenation share the identity %q", a)
	}
}

// TestTheSaltIsNotTakenBackOutAsACallersHint guards the ordering this depends
// on: the steering scrub runs first, and `cache_salt` is not a steering hint, so
// a salt written after it reaches the cluster.
func TestTheSaltIsNotTakenBackOutAsACallersHint(t *testing.T) {
	for _, hint := range steeringHints {
		if hint == "cache_salt" {
			t.Fatal("cache_salt is a steering hint; the salt would be scrubbed before it is sent")
		}
	}
	salted, written, err := withCacheSalt([]byte(imageBody(t, "pixels")), cacheScope{})
	if err != nil || !written {
		t.Fatalf("withCacheSalt: %v, written=%v", err, written)
	}
	scrubbed, removed, err := withoutSteeringHints(salted)
	if err != nil {
		t.Fatalf("withoutSteeringHints: %v", err)
	}
	for _, name := range removed {
		if name == "nvext.cache_salt" {
			t.Error("the scrub removed the salt")
		}
	}
	if !strings.Contains(string(scrubbed), "cache_salt") {
		t.Errorf("the salt did not survive the scrub: %s", scrubbed)
	}
}

// TestTwoQuestionsAboutOneImageNeverShareACacheIdentity guards a measured
// regression: the media-only identity gave every question about one document
// the same salt, and the block hash underneath then served the first question's
// answer to all six of them.
func TestTwoQuestionsAboutOneImageNeverShareACacheIdentity(t *testing.T) {
	first, ok := saltOf(t, imageQuestionBody(t, "identical pixels", "what is the value for 'TO:'?"))
	if !ok {
		t.Fatal("no salt written for a request carrying an image")
	}
	second, ok := saltOf(t, imageQuestionBody(t, "identical pixels", "what is the value for 'MANUFACTURER'?"))
	if !ok {
		t.Fatal("no salt written for a request carrying an image")
	}
	if first == second {
		t.Errorf("two questions about one image share the salt %q", first)
	}
}

// TestOneQuestionAboutOneImageKeepsItsReuse is the other half: the salt must
// still not be a per-request nonce, or an identical repeat re-prefills.
func TestOneQuestionAboutOneImageKeepsItsReuse(t *testing.T) {
	question := "what is the value for 'TO:'?"
	first, _ := saltOf(t, imageQuestionBody(t, "identical pixels", question))
	second, _ := saltOf(t, imageQuestionBody(t, "identical pixels", question))
	if first != second {
		t.Errorf("one request hashed two ways: %q and %q", first, second)
	}
}

// TestAPriorTurnIsInsideTheIdentity pins that the collision is closed for a
// conversation too, where the turn that differs is not the one carrying media.
func TestAPriorTurnIsInsideTheIdentity(t *testing.T) {
	body := func(answer string) string {
		return `{"model":"chat","messages":[` +
			`{"role":"user","content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,aGVsbG8="}},{"type":"text","text":"what is this?"}]},` +
			`{"role":"assistant","content":"` + answer + `"},` +
			`{"role":"user","content":[{"type":"text","text":"and the date?"}]}]}`
	}
	first, ok := saltOf(t, body("a receipt"))
	if !ok {
		t.Fatal("no salt written for a conversation carrying an image")
	}
	second, _ := saltOf(t, body("an invoice"))
	if first == second {
		t.Errorf("two conversations share the salt %q", first)
	}
}
