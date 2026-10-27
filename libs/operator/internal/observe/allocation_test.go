package observe

import "testing"

// directScrape is the shape the operator actually gets. It scrapes each exporter pod itself, so
// nothing collides with the exporter's own identity, nothing is renamed, and the pod holding the
// card is in the plain `pod` label.
const directScrape = `
DCGM_FI_DEV_GPU_UTIL{gpu="0",UUID="GPU-aaaa",modelName="NVIDIA RTX PRO 6000",Hostname="node-a",container="main",namespace="dynamo",pod="gemma-decode-0"} 0
DCGM_FI_DEV_FB_USED{gpu="0",UUID="GPU-aaaa",Hostname="node-a",namespace="dynamo",pod="gemma-decode-0"} 4096
DCGM_FI_DEV_GPU_UTIL{gpu="1",UUID="GPU-bbbb",modelName="NVIDIA RTX PRO 6000",Hostname="node-a",container="main",namespace="dynamo",pod="gemma-decode-0"} 97
DCGM_FI_DEV_GPU_UTIL{gpu="2",UUID="GPU-cccc",modelName="NVIDIA RTX PRO 6000",Hostname="node-a"} 0
`

// TestParseDCGMReadsAPlainPodLabelWhenNothingWasRenamed: reading only `exported_pod` is why the
// binding was empty on every real cluster: that spelling appears only when a scraper has already
// claimed the plain name, which is the exception instead of the rule.
func TestParseDCGMReadsAPlainPodLabelWhenNothingWasRenamed(t *testing.T) {
	got := ParseDCGM(directScrape).forNode("node-a", nil)
	if len(got) != 3 {
		t.Fatalf("node has %d devices, want 3", len(got))
	}
	if got[0].Pod != "gemma-decode-0" || got[0].Namespace != "dynamo" {
		t.Fatalf("binding is %q/%q, want dynamo/gemma-decode-0", got[0].Namespace, got[0].Pod)
	}
	if got[2].Pod != "" {
		t.Fatalf("an unbound card claims pod %q", got[2].Pod)
	}
}

// TestPodDevicesGroupsTheCardsOnePodHolds: two cards on one pod is one allocation of two, not two
// allocations. The plane counts devices per allocation to decide what is free.
func TestPodDevicesGroupsTheCardsOnePodHolds(t *testing.T) {
	got := podDevicesFrom(ParseDCGM(directScrape))
	held := got[PodKey("dynamo", "gemma-decode-0")]
	if len(held) != 2 {
		t.Fatalf("pod holds %v, want two cards", held)
	}
	if held[0] != "GPU-aaaa" || held[1] != "GPU-bbbb" {
		t.Fatalf("cards are %v, want them sorted and de-duplicated", held)
	}
}

// TestPodDevicesLeavesIdleCardsUnattributed: grouping the unbound cards under the empty pod would
// read as one workload holding every free accelerator in the cluster.
func TestPodDevicesLeavesIdleCardsUnattributed(t *testing.T) {
	got := podDevicesFrom(ParseDCGM(directScrape))
	if _, found := got[PodKey("", "")]; found {
		t.Fatal("idle cards were grouped under an empty pod")
	}
	for key, held := range got {
		for _, uuid := range held {
			if uuid == "GPU-cccc" {
				t.Fatalf("idle card is attributed to %q", key)
			}
		}
	}
}

// TestDeviceIndexAnswersNothingUntilItIsPublished: an unpublished index must not be mistaken for a
// cluster in which nothing holds anything.
func TestDeviceIndexAnswersNothingUntilItIsPublished(t *testing.T) {
	var index DeviceIndex
	if got := index.DevicesForPod("dynamo", "gemma-decode-0"); got != nil {
		t.Fatalf("unpublished index answered %v", got)
	}
	index.Publish(podDevicesFrom(ParseDCGM(directScrape)))
	if got := index.DevicesForPod("dynamo", "gemma-decode-0"); len(got) != 2 {
		t.Fatalf("published index answered %v, want two cards", got)
	}
	if got := index.DevicesForPod("dynamo", "nobody"); got != nil {
		t.Fatalf("unknown pod answered %v", got)
	}
}

// TestDeviceIndexIsSafeForAConcurrentReader: one loop publishes on its tick while another reads on
// every reconcile.
func TestDeviceIndexIsSafeForAConcurrentReader(t *testing.T) {
	var index DeviceIndex
	done := make(chan struct{})
	go func() {
		for i := 0; i < 200; i++ {
			index.Publish(podDevicesFrom(ParseDCGM(directScrape)))
		}
		close(done)
	}()
	for i := 0; i < 200; i++ {
		index.DevicesForPod("dynamo", "gemma-decode-0")
	}
	<-done
}
