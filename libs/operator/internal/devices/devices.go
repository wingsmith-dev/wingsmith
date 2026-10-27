// Package devices resolves a vendor's product string into a hangar device class.
//
// The dictionary itself is libs/packages/contract/devices.json, read by both the operator and the plane, because
// the two dialects a card is named in, the GPU Feature Discovery node label and the DCGM
// exporter's modelName, never agree with each other and neither is ours. table.json is a copy of
// the contract file so the binary carries it; a test fails if the two drift.
package devices

import (
	_ "embed"
	"encoding/json"
	"log/slog"
	"strings"
	stdsync "sync"
	"unicode"
)

//go:embed table.json
var tableJSON []byte

// Dialects the table indexes aliases by.
const (
	DialectGFD  = "gfd"
	DialectDCGM = "dcgm"
)

// Class is one card variant. Memory and form factor are part of the identity: an h100-80gb-sxm
// and an h100-80gb-pcie are different things to place against.
type Class struct {
	Class string `json:"class"`
	// Label is the human name for the class and the only one to print to a person; Class is the
	// identifier and the only one that goes on the wire.
	Label        string              `json:"label"`
	Architecture string              `json:"architecture"`
	Vendor       string              `json:"vendor"`
	MemoryMiB    int64               `json:"memory_mib"`
	FormFactor   string              `json:"form_factor"`
	Aliases      map[string][]string `json:"aliases"`
}

// Table is the loaded dictionary, indexed by dialect and normalized alias.
type Table struct {
	Version int     `json:"version"`
	Classes []Class `json:"classes"`

	index map[string]map[string]Class

	mu   stdsync.Mutex
	seen map[string]struct{}
}

var (
	once   stdsync.Once
	loaded *Table
)

// Load returns the embedded dictionary. It is parsed once and shared.
func Load() *Table {
	once.Do(func() {
		table, err := Parse(tableJSON)
		if err != nil {
			slog.Default().Error("device class table is unreadable, every class will be slugified", "error", err)
			table = &Table{index: map[string]map[string]Class{}, seen: map[string]struct{}{}}
		}
		loaded = table
	})
	return loaded
}

// Parse builds a table from the contract document.
func Parse(raw []byte) (*Table, error) {
	var table Table
	if err := json.Unmarshal(raw, &table); err != nil {
		return nil, err
	}
	table.index = map[string]map[string]Class{}
	table.seen = map[string]struct{}{}
	for _, class := range table.Classes {
		for dialect, aliases := range class.Aliases {
			byAlias, ok := table.index[dialect]
			if !ok {
				byAlias = map[string]Class{}
				table.index[dialect] = byAlias
			}
			for _, alias := range aliases {
				byAlias[Normalize(alias)] = class
			}
		}
	}
	return &table, nil
}

// ByClass finds a class by its own identifier, not by a vendor alias.
//
// The alias index answers "what is this card called", which is the question discovery asks. This
// answers "what is a card of this class", which is what a node that states its class outright,
// through the device-class label, leaves to be filled in.
func (t *Table) ByClass(class string) (Class, bool) {
	if t == nil || class == "" {
		return Class{}, false
	}
	for _, candidate := range t.Classes {
		if candidate.Class == class {
			return candidate, true
		}
	}
	return Class{}, false
}

// Lookup finds the class a vendor string names in one dialect.
func (t *Table) Lookup(dialect, product string) (Class, bool) {
	if t == nil || product == "" {
		return Class{}, false
	}
	class, ok := t.index[dialect][Normalize(product)]
	return class, ok
}

// Resolve maps a vendor string onto a hangar class, falling back to the slug of the string itself
// so unknown hardware still gets a stable, deterministic class. A fallthrough is logged once per
// distinct string, because discovery runs on an interval and the table is grown from these lines.
func (t *Table) Resolve(dialect, product string) string {
	if product == "" {
		return ""
	}
	if class, ok := t.Lookup(dialect, product); ok {
		return class.Class
	}
	t.warn(dialect, product)
	return Slug(product)
}

func (t *Table) warn(dialect, product string) {
	key := dialect + "\x00" + product
	t.mu.Lock()
	_, already := t.seen[key]
	if !already {
		t.seen[key] = struct{}{}
	}
	t.mu.Unlock()
	if already {
		return
	}
	slog.Default().Warn(
		"device is not in the device class dictionary, falling back to its slug",
		"dialect", dialect,
		"product", product,
		"class", Slug(product),
	)
}

// Normalize is the matching form of a vendor string: lowercase, no vendor prefix, every run of
// non-alphanumeric characters collapsed to one hyphen. It is identical to the Python side and
// must stay that way: spaces and hyphens are the whole difference between the two dialects.
func Normalize(s string) string {
	return Slug(s)
}

// Slug is the fallback class for hardware the table does not name.
func Slug(product string) string {
	s := strings.ToLower(product)
	s = strings.Map(func(r rune) rune {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			return r
		}
		return '-'
	}, s)
	for strings.Contains(s, "--") {
		s = strings.ReplaceAll(s, "--", "-")
	}
	s = strings.Trim(s, "-")
	return strings.TrimPrefix(s, "nvidia-")
}
