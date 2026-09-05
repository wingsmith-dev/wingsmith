package serving

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"hash"
	"strconv"
)

// saltParts are the content parts that carry media, and the field each one
// carries it in.
//
// Deliberately not `mediaParts`, which is the refusal rule's list and excludes
// `image_url` because the frontend already answers for an image it cannot
// decode. This list is about identity rather than validity, and an image is the
// modality that needs it most.
var saltParts = map[string]string{
	"image_url":   "url",
	"video_url":   "url",
	"audio_url":   "url",
	"input_audio": "data",
}

// mediaIdentity returns a stable hex identity for every media payload a request
// carries, in the order it carries them, and whether it carried any.
//
// Why this exists at all: a disaggregated decode worker is handed its prompt as
// expanded token ids with no multimodal data attached — see
// `dynamo/vllm/multimodal_utils/request_processor.py`, where the P/D handoff for
// any model that is not Qwen-VL is `{"expanded_prompt_token_ids": ...}` and the
// decode branch then sets `has_mm_data = False`. vLLM therefore builds that
// request with no `mm_features`, `_gen_mm_extra_hash_keys` contributes nothing,
// and the prefix-cache block hash is token ids alone. Two different images of
// the same pixel dimensions expand to the same placeholder run, so they hash
// identically and the second request is served the first one's KV — measured on
// `cluster-site-d` as 0 of 12 correct at concurrency 12, against 12 of
// 12 with a salt.
//
// `cache_salt` is the one field that survives that handoff into the block hash,
// so deriving it from the media is how the identity the engine dropped is put
// back. Same media means the same salt and the reuse the cache exists for is
// kept; different media means a different salt and the false match is gone.
//
// The payload is hashed by value when the request carries bytes and by
// reference when it carries a URL. A reference is weaker — a URL whose content
// changes underneath it reuses the wrong blocks — but it is the caller's own
// cache coherency, and the alternative is deriving no salt at all and returning
// silently to the defect this exists to close.
//
// Every part contributes its kind and a length prefix, so an image and a video
// holding identical bytes are different identities and two parts cannot be
// rearranged into the same digest as one.
//
// The text is in the digest for the same reason the media is, and the media
// alone was not enough. Deriving the salt from the media put the image identity
// back and was verified 24 of 24 on 2026-09-12, but it makes every question
// about one image share a salt, and a prompt that differs only after the
// placeholder run still collides in the block hash underneath — measured on
// 2026-09-14 as one answer served to six different questions about one
// document, on both endpoints, and reproduced under a caller's own shared salt,
// so the collision is the engine's and the salt is only the lever we hold. An
// identity the engine cannot contradict is therefore the whole prompt: same
// request means the same salt and a repeat still reuses everything, a different
// question means a different salt and no false match. What it costs is the
// image prefix a second question about one image used to reuse, which is the
// reuse that was returning the first question's answer.
func promptIdentity(fields map[string]json.RawMessage) (string, bool) {
	messages, readable := jsonArray(fields["messages"])
	if !readable {
		return "", false
	}

	digest := sha256.New()
	carried := false
	for _, raw := range messages {
		message, readable := jsonObject(raw)
		if !readable {
			continue
		}
		if role, stated := jsonString(message["role"]); stated {
			writeSegment(digest, "role")
			writeSegment(digest, role)
		}
		if text, isString := jsonString(message["content"]); isString {
			writeSegment(digest, "text")
			writeSegment(digest, text)
			continue
		}
		parts, isArray := jsonArray(message["content"])
		if !isArray {
			continue
		}
		for _, raw := range parts {
			part, readable := jsonObject(raw)
			if !readable {
				continue
			}
			kind, stated := jsonString(part["type"])
			if !stated {
				continue
			}
			field, carriesMedia := saltParts[kind]
			if !carriesMedia {
				// Anything that is not media is still prompt, and a prompt that
				// differs only here is the collision this closes.
				if text, stated := jsonString(part[kind]); stated {
					writeSegment(digest, kind)
					writeSegment(digest, text)
				}
				continue
			}
			payload, stated := jsonString(objectRaw(part[kind], field))
			if !stated || payload == "" {
				continue
			}
			writeSegment(digest, kind)
			writeSegment(digest, mediaPayloadKey(field, payload))
			carried = true
		}
	}
	// A request carrying no media has no identity here: the engine never
	// dropped one it was not given, and the tenant scope is what separates it.
	if !carried {
		return "", false
	}
	return hex.EncodeToString(digest.Sum(nil)), true
}

// mediaPayloadKey is the bytes a part carries, or the reference it carries.
//
// A `data` field is bare base64 and a `url` field is base64 only when the data
// URL says so; both are decoded so that two spellings of one image — a
// re-encoding with different padding or case — are one identity. Anything else
// is a reference and is keyed by the string itself.
func mediaPayloadKey(field, payload string) string {
	if field == "data" {
		if decoded, err := base64.StdEncoding.DecodeString(payload); err == nil {
			return string(decoded)
		}
		return payload
	}
	if encoded, byValue := mediaBase64(field, payload); byValue {
		if decoded, err := base64.StdEncoding.DecodeString(encoded); err == nil {
			return string(decoded)
		}
		return encoded
	}
	return payload
}

// writeSegment length-prefixes every field that goes into the digest, so no two
// different requests can be spelled into one identity by moving a boundary.
func writeSegment(digest hash.Hash, value string) {
	digest.Write([]byte(strconv.Itoa(len(value))))
	digest.Write([]byte(":"))
	digest.Write([]byte(value))
}

// cacheScope is whose prefix cache a request may reuse.
//
// A prefix cache shared between tenants on one deployment is a side channel: how
// fast a prompt's first token comes back says whether somebody else sent the same
// prefix recently, and a system prompt or a document is exactly the prefix worth
// probing for. Dynamo folds `cache_salt` into every block hash (and, since 1.4,
// into the KV router's overlap score), so a salt per tenant gives each tenant its
// own cache on hardware they share, and keeps every hit inside one tenant.
//
// Tenant is the plane's tenant id: the one the public zone resolved from the host,
// or on the internal zone the tenant that owns the endpoint. Caller is the
// internal caller's own narrower scope, from `X-Hangar-Cache-Scope` — an internal
// platform serving many customers through one tenant names the customer there —
// and is honoured only on the internal zone, whose callers this fleet runs.
type cacheScope struct {
	Tenant string
	Caller string
}

func (s cacheScope) empty() bool {
	return s.Tenant == "" && s.Caller == ""
}

// cacheScopeHeader is the internal zone's narrower cache scope. It is read and
// removed by stageCacheSalt and never reaches a cluster.
const cacheScopeHeader = "X-Hangar-Cache-Scope"

// cacheSaltPaths are the generation paths whose engine keeps a prefix cache and
// whose request Dynamo reads `nvext.cache_salt` from. Embeddings, images and
// video are left alone: no prefix is reused across their requests, and a field
// written into a request nobody reads it from buys nothing.
var cacheSaltPaths = map[string]bool{
	"/v1/chat/completions": true,
	"/v1/completions":      true,
	"/v1/responses":        true,
	"/v1/messages":         true,
}

// withCacheSalt returns the body with `nvext.cache_salt` set to the request's
// cache identity, and whether it wrote one.
//
// The identity is the tenant scope, then the prompt identity when the request
// carries media (see promptIdentity), then any salt the caller sent. A request
// with neither a scope nor media is returned as the exact bytes that arrived.
// The `nvext` object is created when absent, which the policy machinery cannot
// do — `containersTo` reports false when the parent object is missing and `fill`
// never creates one.
//
// A caller's own salt is folded in rather than honoured as it stands. Honoured,
// a caller sending one constant salt across different images would reintroduce
// the collision the prompt identity closes, and a caller naming another
// tenant's salt would share that tenant's cache; folded, a caller isolating
// itself still gets that isolation and can never widen it.
//
// It must run after `withoutSteeringHints`, which removes the routing fields a
// caller put in `nvext`. `cache_salt` is deliberately not one of those.
func withCacheSalt(body []byte, scope cacheScope) ([]byte, bool, error) {
	media := hasMediaPart(body)
	if scope.empty() && !media {
		return body, false, nil
	}
	fields, _, ok := topLevelFields(body)
	if !ok {
		return body, false, nil
	}

	var hints map[string]json.RawMessage
	if raw, present := fields["nvext"]; present {
		if err := json.Unmarshal(raw, &hints); err != nil {
			// Not an object: the runtime refuses it as invalid nvext, and a salt
			// written into something the engine will not read is worse than none.
			return body, false, nil
		}
	}

	var prompt string
	var carried bool
	if media {
		prompt, carried = promptIdentity(fields)
	}
	if scope.empty() && !carried {
		return body, false, nil
	}

	digest := sha256.New()
	if scope.Tenant != "" {
		writeSegment(digest, "tenant")
		writeSegment(digest, scope.Tenant)
	}
	if scope.Caller != "" {
		writeSegment(digest, "scope")
		writeSegment(digest, scope.Caller)
	}
	if carried {
		writeSegment(digest, "prompt")
		writeSegment(digest, prompt)
	}
	if raw, present := hints["cache_salt"]; present {
		if callerSalt, _ := jsonString(raw); callerSalt != "" {
			writeSegment(digest, "caller")
			writeSegment(digest, callerSalt)
		}
	}

	encodedSalt, err := json.Marshal(hex.EncodeToString(digest.Sum(nil)))
	if err != nil {
		return nil, false, err
	}
	if hints == nil {
		hints = make(map[string]json.RawMessage, 1)
	}
	hints["cache_salt"] = encodedSalt

	encodedHints, err := json.Marshal(hints)
	if err != nil {
		return nil, false, err
	}
	fields["nvext"] = encodedHints

	rewritten, err := json.Marshal(fields)
	if err != nil {
		return nil, false, err
	}
	return rewritten, true, nil
}

// hasMediaPart is a cheap guard so a text request does not pay a JSON walk.
//
// A false positive costs one walk that finds nothing; a false negative is not
// possible, because a part this rule salts has to name its type in the body.
func hasMediaPart(body []byte) bool {
	for kind := range saltParts {
		if bytes.Contains(body, []byte(`"`+kind+`"`)) {
			return true
		}
	}
	return false
}
