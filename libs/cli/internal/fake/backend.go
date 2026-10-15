package fake

import (
	"context"
	"encoding/base64"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/hangar-dev/hangar/libs/cli/internal/api"
	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
)

// defaultLimit and maxLimit mirror the plane's pagination, so a listing that
// paginates against the fake paginates the same way against the server.
const (
	defaultLimit = 50
	maxLimit     = 200
)

// Backend implements api.Backend against seeded in-memory state.
type Backend struct {
	state *state
	path  string
	fail  string
	// polls counts `operation get` calls per operation so a drain visibly
	// advances through its phases offline, the way one does in a cluster.
	polls map[string]int
	// minted keeps ids unique within one process even inside a microsecond.
	minted int
	// HasToken is whether the caller's context holds a token. The fake never
	// reads it, but whoami reports it, and claiming one that is not there
	// would make the fake disagree with the config it is running beside.
	HasToken bool
}

// New builds a fake backend from the built-in seed. A path may point at a
// JSON document that replaces the seed, which is how tests pin fixtures.
func New(path string) (*Backend, error) {
	st, err := load(path)
	if err != nil {
		return nil, err
	}
	return &Backend{state: st, path: path, fail: os.Getenv("HANGAR_FAKE_FAIL"), polls: map[string]int{}}, nil
}

func (b *Backend) Simulated() bool { return true }

// injected turns HANGAR_FAKE_FAIL=<CODE> into a real failure, so error paths
// are exercisable without a server that can be made to misbehave.
func (b *Backend) injected() error {
	if b.fail == "" {
		return nil
	}
	code := errs.ParseCode(b.fail)
	if _, ok := errs.Explain(code); !ok {
		code = errs.CodeServerError
	}
	return errs.New(code, "injected failure (HANGAR_FAKE_FAIL=%s)", b.fail)
}

// persist writes the current state when the caller pinned one to a file.
func (b *Backend) persist() error { return save(b.path, b.state) }

// newUID mints an identifier shaped like the plane's: a prefix and a
// 26-character lowercase body.
func (b *Backend) newUID(prefix string) string {
	b.minted++
	return fmt.Sprintf("%s%026d", prefix, time.Now().UTC().UnixMicro()+int64(b.minted))
}

// Identity reports the simulated context.
func (b *Backend) Identity(ctx context.Context) (api.Identity, error) {
	if err := b.injected(); err != nil {
		return api.Identity{}, err
	}
	return api.Identity{Server: "fake://local", HasToken: b.HasToken, Reachable: true, Simulated: true}, nil
}

// Login accepts any non-empty password and hands back a session that works
// only here. Nothing leaves the process: the fake is sealed, and a login
// against it must never reach a real plane.
func (b *Backend) Login(ctx context.Context, req api.LoginRequest) (api.LoginResult, error) {
	if err := b.injected(); err != nil {
		return api.LoginResult{}, err
	}
	if req.Password == "" {
		return api.LoginResult{}, errs.New(errs.CodeAuthRequired, "invalid credentials")
	}
	kind := "admin"
	if req.Slug != "" && req.Slug != "admin" {
		kind = "tenant"
	}
	return api.LoginResult{AccessToken: "fake-access", RefreshToken: "fake-refresh", ExpiresIn: 900, Kind: kind}, nil
}

// Logout has nothing to revoke.
func (b *Backend) Logout(ctx context.Context, refreshToken string) error { return b.injected() }

// --- pagination ------------------------------------------------------------

func encodeCursor(offset int) string {
	return strings.TrimRight(base64.URLEncoding.EncodeToString([]byte(strconv.Itoa(offset))), "=")
}

func decodeCursor(cursor string) (int, error) {
	if cursor == "" {
		return 0, nil
	}
	padded := cursor + strings.Repeat("=", (4-len(cursor)%4)%4)
	raw, err := base64.URLEncoding.DecodeString(padded)
	if err != nil {
		return 0, errs.New(errs.CodeUsage, "cursor is not valid")
	}
	n, err := strconv.Atoi(string(raw))
	if err != nil {
		return 0, errs.New(errs.CodeUsage, "cursor is not valid")
	}
	return n, nil
}

// paginate slices a filtered collection into one page and mints the cursor
// for the next, unless the caller asked to follow them all.
func paginate[T any](items []T, opts api.ListOpts) (api.Page[T], error) {
	if opts.All {
		return api.Page[T]{Items: items, Count: len(items)}, nil
	}
	offset, err := decodeCursor(opts.Cursor)
	if err != nil {
		return api.Page[T]{}, err
	}
	limit := opts.Limit
	if limit <= 0 {
		limit = defaultLimit
	}
	if limit > maxLimit {
		limit = maxLimit
	}
	if offset > len(items) {
		offset = len(items)
	}
	end := offset + limit
	next := ""
	if end < len(items) {
		next = encodeCursor(end)
	} else {
		end = len(items)
	}
	page := items[offset:end]
	return api.Page[T]{Items: page, Count: len(page), NextCursor: next}, nil
}

// collect filters a collection with the given predicate and pages the result.
func collect[T any](b *Backend, items []T, opts api.ListOpts, keep func(T) bool) (api.Page[T], error) {
	if err := b.injected(); err != nil {
		return api.Page[T]{}, err
	}
	kept := make([]T, 0, len(items))
	for _, it := range items {
		if keep == nil || keep(it) {
			kept = append(kept, it)
		}
	}
	return paginate(kept, opts)
}

// find resolves a reference to exactly one object by its whole id or name.
// The API does not match prefixes (libs/packages/contract/API.md §3) and
// neither does the fake: prefix resolution is the command layer's, which does
// it for reads and refuses it for verbs that change something. A fake that
// matched prefixes itself would hide that difference from every test.
func find[T any](b *Backend, noun string, items []T, ref string, keys func(T) []string) (T, error) {
	var zero T
	if err := b.injected(); err != nil {
		return zero, err
	}
	if strings.TrimSpace(ref) == "" {
		return zero, errs.New(errs.CodeUsage, "a %s name or id is required", noun)
	}
	for _, it := range items {
		for _, k := range keys(it) {
			if k == ref {
				return it, nil
			}
		}
	}
	return zero, errs.New(errs.CodeNotFound, "no %s named %q", noun, ref).
		WithFix("List them", "hgr "+noun+" ls")
}

// --- predicate helpers -----------------------------------------------------

func eqFold(want, got string) bool {
	return want == "" || strings.EqualFold(want, got)
}

func refEq(want string, candidates ...string) bool {
	if want == "" {
		return true
	}
	for _, c := range candidates {
		if c != "" && strings.EqualFold(c, want) {
			return true
		}
	}
	return false
}

func refEqPtr(want string, id, name *string) bool {
	if want == "" {
		return true
	}
	return refEq(want, deref(id), deref(name))
}

// refEqAny matches a domain whose membership contains the wanted node, by id or
// by name. Membership is a set: a fabric across four machines matches all four.
func refEqAny(want string, ids, names []string) bool {
	if want == "" {
		return true
	}
	for i, id := range ids {
		name := ""
		if i < len(names) {
			name = names[i]
		}
		if refEq(want, id, name) {
			return true
		}
	}
	return false
}

func boolEq(want *bool, got bool) bool {
	return want == nil || *want == got
}

func contains(needle string, haystack ...string) bool {
	if needle == "" {
		return true
	}
	needle = strings.ToLower(needle)
	for _, h := range haystack {
		if strings.Contains(strings.ToLower(h), needle) {
			return true
		}
	}
	return false
}

func labelsMatch(labels map[string]any, want map[string]string) bool {
	for k, v := range want {
		got, ok := labels[k]
		if !ok || toString(got) != v {
			return false
		}
	}
	return true
}

func toString(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	if v == nil {
		return ""
	}
	return fmt.Sprint(v)
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func after(t *time.Time, cutoff *time.Time) bool {
	if cutoff == nil {
		return true
	}
	return t != nil && !t.Before(*cutoff)
}

// Backend must answer everything the CLI can ask, or the fake stops being a
// stand-in and becomes a subset nobody can rely on.
var _ api.Backend = (*Backend)(nil)
