package fake

import (
	"context"

	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/api"
	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/errs"
)

// Manifest answers with identity and phase only. The fake holds the HTTP read
// shapes, not the plane's translation to spec, and inventing a spec here would be
// a second definition of the manifest to drift from the first.
func (b *Backend) Manifest(ctx context.Context, collection, project, name string) (api.Manifest, error) {
	meta := map[string]any{"name": name}
	if project != "" {
		meta["namespace"] = project
	}
	manifest := func(kind, uid string, status map[string]any) api.Manifest {
		meta["uid"] = uid
		return api.Manifest{"apiVersion": api.ManifestAPIVersion, "kind": kind, "metadata": meta, "spec": map[string]any{}, "status": status}
	}
	switch collection {
	case "workloads":
		wl, err := b.Workload(ctx, name)
		if err != nil {
			return nil, err
		}
		return manifest("Workload", wl.ID, map[string]any{"phase": wl.Phase}), nil
	case "endpoints":
		e, err := b.Endpoint(ctx, name)
		if err != nil {
			return nil, err
		}
		return manifest("Endpoint", e.ID, map[string]any{}), nil
	case "artifacts":
		a, err := b.Artifact(ctx, name)
		if err != nil {
			return nil, err
		}
		return manifest("Artifact", a.ID, map[string]any{"verified": a.Verified}), nil
	}
	return nil, errs.New(errs.CodeUsage, "unknown manifest collection %q", collection)
}

// ApplyManifest echoes the manifest back, created when the fake holds no object by that name.
func (b *Backend) ApplyManifest(ctx context.Context, collection, project, name string, m api.Manifest, opts api.ApplyOptions) (api.Manifest, string, error) {
	if collection != "workloads" && collection != "endpoints" {
		return nil, "", errs.New(errs.CodeUsage, "unknown manifest collection %q", collection)
	}
	if _, err := b.Manifest(ctx, collection, project, name); err != nil {
		return m, api.ApplyCreated, nil
	}
	return m, api.ApplyConfigured, nil
}
