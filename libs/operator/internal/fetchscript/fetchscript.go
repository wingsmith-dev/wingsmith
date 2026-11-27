// Package fetchscript holds the python a weight download runs inside the engine image. It is
// shared by the Kubernetes prefetch Job (render) and the host's prefetch container (host/serve),
// so both lay the cache out the same way, and it must stay free of Kubernetes imports.
package fetchscript

import "fmt"

// HF is the script that downloads a pinned Hugging Face repo into HF_HOME.
func HF(repo, revision string) string { return fmt.Sprintf(hfScript, repo, revision) }

// URL is the script that pulls an http(s) or s3 object into dest.
func URL(uri, dest string) string { return fmt.Sprintf(fetchScript, uri, dest) }

// progressScript prints one line of download progress on a timer, and is prepended to every fetch
// strategy so that a running job says something before it is finished.
//
// A line instead of a bar. HF_HUB_DISABLE_PROGRESS_BARS stays set for the reason it was set (a
// carriage-returned bar renders in `kubectl logs` as thousands of lines that bury the one worth
// reading), but the job that resulted said nothing at all for the forty minutes of a large pull,
// and a silent job is indistinguishable from a stuck one. That question, not the byte count, is
// what somebody tailing the log is actually asking.
//
// Bytes are measured on disk under the directory the fetch writes into, not taken from a
// hook inside the client library: it is the same number the volume will hold at the end, one
// implementation serves both strategies, and it costs a directory walk every interval instead of a
// dependency on huggingface_hub's internals. lstat, not stat, because a HuggingFace snapshot
// is symlinks into a blob store under the same root, and following them would count every file
// twice.
//
// Thirty seconds is slow enough that an hour-long pull is sixty lines of log, and fast enough that
// a reader waiting on a stalled download sees the number stop moving instead of wondering whether
// anything is due. The thread is a daemon so it cannot hold the job open past the fetch.
const progressScript = `import os, threading, time
_start = time.monotonic()
def _size(path):
    total = 0
    for root, _, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total
def _report(path):
    while True:
        time.sleep(30)
        print("prefetch progress bytes=%%d elapsed_seconds=%%d path=%%s" %% (
            _size(path), int(time.monotonic() - _start), path), flush=True)
def _watch(path):
    threading.Thread(target=_report, args=(path,), daemon=True).start()
`

// hfScript downloads a HuggingFace repo into HF_HOME, held as a constant so the rendered job is a
// pure function of its inputs and can be applied repeatedly without churning the object.
//
// max_workers=4 is enough parallelism to saturate the link without tripping HuggingFace rate
// limiting on an unauthenticated pull. It was 8, which also multiplied by four the dirty page
// cache waiting on writeback to the shared NFS volume: memory this job is charged for.
//
// Progress is watched on the repo's own directory under the hub cache and not on HF_HOME, because
// HF_HOME is the shared volume every artifact lands in: two warm-set entries fetch at once, and a
// count over the root would report each job the other's bytes. The path is reconstructed, not
// asked for, since snapshot_download only returns it once there is nothing left to report.
const hfScript = progressScript + `from huggingface_hub import snapshot_download
try:
    from huggingface_hub.constants import HF_HUB_CACHE as hub
except ImportError:
    hub = os.path.join(os.environ.get("HF_HOME", "/models/hf"), "hub")
repo, revision = %q, %q or None
_watch(os.path.join(hub, "models--" + repo.replace("/", "--")))
p = snapshot_download(repo, revision=revision, max_workers=4)
print("cached at", p, flush=True)
`

// fetchScript pulls custom weights into the cache at the path modelRef falls back to.
//
// Written inline in python3 instead of shelling out to a cloud CLI, because the only thing the
// engine runtime image is guaranteed to carry is a python with huggingface_hub in it; an image
// without the aws CLI would fail at runtime with a message about a missing binary, not
// about the weights.
//
// TODO: credentials for s3 and https sources. Only a private artifact's Hugging Face token is
// mounted today (see hfTokenEnvFrom); other sources fetch anonymously.
// TODO: verify ArtifactDesired.Digest once the digest algorithm over a multi-file artifact is
// pinned down; checking the wrong thing is worse than checking nothing.
const fetchScript = progressScript + `import sys, urllib.parse, urllib.request
uri, dest = %q, %q
os.makedirs(dest, exist_ok=True)
_watch(dest)
u = urllib.parse.urlparse(uri)
if u.scheme in ("http", "https"):
    out = os.path.join(dest, os.path.basename(u.path) or "weights")
    urllib.request.urlretrieve(uri, out)
elif u.scheme == "s3":
    import boto3
    boto3.client("s3").download_file(u.netloc, u.path.lstrip("/"), os.path.join(dest, os.path.basename(u.path)))
else:
    sys.exit("unsupported source scheme: " + u.scheme)
print("cached at", dest, flush=True)
`
