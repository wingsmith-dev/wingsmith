package render

import (
	"fmt"
	"sort"
	"strings"
)

// imageAllowed reports whether an image reference is under one of the allowed prefixes. An empty
// list allows every image. A prefix matches at a path, tag or digest boundary, so
// "ghcr.io/acme" allows "ghcr.io/acme/vllm:1.0" and not "ghcr.io/acme-fork/vllm:1.0"; a prefix
// ending in "/" matches anything below it.
func imageAllowed(image string, prefixes []string) bool {
	if len(prefixes) == 0 {
		return true
	}
	for _, prefix := range prefixes {
		prefix = strings.TrimSpace(prefix)
		if prefix == "" {
			continue
		}
		if image == prefix {
			return true
		}
		if !strings.HasPrefix(image, prefix) {
			continue
		}
		if strings.HasSuffix(prefix, "/") {
			return true
		}
		switch image[len(prefix)] {
		case '/', ':', '@':
			return true
		}
	}
	return false
}

// checkImages refuses a rendered object that runs an image outside opts.ImagePrefixes.
//
// It walks the whole object instead of the containers this package wrote, because a deployment's
// pod and Dynamo overlays are merged in afterwards and can name an image of their own.
func checkImages(object any, opts Options) error {
	if len(opts.ImagePrefixes) == 0 {
		return nil
	}
	var refused []string
	seen := map[string]bool{}
	var walk func(v any)
	walk = func(v any) {
		switch v := v.(type) {
		case map[string]any:
			for key, child := range v {
				if image, ok := child.(string); ok && key == "image" {
					if !imageAllowed(image, opts.ImagePrefixes) && !seen[image] {
						seen[image] = true
						refused = append(refused, image)
					}
					continue
				}
				walk(child)
			}
		case []any:
			for _, child := range v {
				walk(child)
			}
		}
	}
	walk(object)
	if len(refused) == 0 {
		return nil
	}
	sort.Strings(refused)
	return fmt.Errorf("image %s is not under an allowed prefix (HANGAR_ALLOWED_IMAGE_PREFIXES: %s)",
		strings.Join(refused, ", "), strings.Join(opts.ImagePrefixes, ", "))
}
