package observe

import (
	"strings"

	corev1 "k8s.io/api/core/v1"

	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// Clouds and distributions this operator can name. Anything it cannot name is reported empty
// rather than guessed: an unknown provider is a fact, and "aws" asserted about a bare-metal
// cluster is worse than saying nothing.
const (
	CloudAWS          = "aws"
	CloudGCP          = "gcp"
	CloudAzure        = "azure"
	CloudDigitalOcean = "digitalocean"
	CloudOracle       = "oci"
	CloudIBM          = "ibm"
	CloudLinode       = "linode"
	CloudScaleway     = "scaleway"
	CloudHetzner      = "hetzner"
	CloudEquinix      = "equinix"
	CloudOpenStack    = "openstack"
	CloudVSphere      = "vsphere"
	CloudNutanix      = "nutanix"
	CloudKind         = "kind"
)

const (
	DistributionEKS       = "eks"
	DistributionGKE       = "gke"
	DistributionAKS       = "aks"
	DistributionDOKS      = "doks"
	DistributionOKE       = "oke"
	DistributionRKE2      = "rke2"
	DistributionK3s       = "k3s"
	DistributionOpenShift = "openshift"
	DistributionTalos     = "talos"
	DistributionMicroK8s  = "microk8s"
	DistributionKind      = "kind"
	DistributionMinikube  = "minikube"
	DistributionVanilla   = "kubernetes"
)

// providerSchemes maps the scheme of a node's provider reference onto a cloud.
//
// The provider reference is the right source because the cloud controller manager sets it from
// the machine's own identity document. Region and instance-type labels are set by the same
// component in practice but are ordinary labels that anything may overwrite, and a cluster with
// hand-edited labels should still report the cloud it is actually on.
var providerSchemes = map[string]string{
	"aws":          CloudAWS,
	"gce":          CloudGCP,
	"azure":        CloudAzure,
	"digitalocean": CloudDigitalOcean,
	"oci":          CloudOracle,
	"ibm":          CloudIBM,
	"linode":       CloudLinode,
	"scaleway":     CloudScaleway,
	"hcloud":       CloudHetzner,
	"equinixmetal": CloudEquinix,
	"packet":       CloudEquinix,
	"openstack":    CloudOpenStack,
	"vsphere":      CloudVSphere,
	"nutanix":      CloudNutanix,
	"kind":         CloudKind,
}

// cloudOf names the cloud a node belongs to from its provider reference.
//
// The reference is `<scheme>://<path>`, and the path shape differs per provider — AWS writes an
// empty host and puts the zone first (`aws:///us-east-2b/i-0abc`), others do not — so only the
// scheme is read. Empty for a node with no provider reference at all, which is what bare metal
// and an unconfigured cloud controller both look like.
func cloudOf(providerID string) string {
	scheme, _, found := strings.Cut(providerID, "://")
	if !found {
		return ""
	}
	return providerSchemes[strings.ToLower(scheme)]
}

// taintsOf converts a node's taints into the neutral form.
//
// Carried because a taint is why a node with free accelerators is not receiving work, and without
// it the plane can only report the symptom. The effect is lowercased into the same snake-case
// dialect every other enum on the wire uses.
func taintsOf(in []corev1.Taint) []Taint {
	if len(in) == 0 {
		return nil
	}
	out := make([]Taint, 0, len(in))
	for _, t := range in {
		out = append(out, Taint{Key: t.Key, Value: t.Value, Effect: snake(string(t.Effect))})
	}
	return out
}

// distributionMarkers are labels that only one distribution sets, checked before the version
// string because a label is unambiguous where a version suffix is a convention.
var distributionMarkers = []struct {
	label        string
	distribution string
}{
	{"eks.amazonaws.com/nodegroup", DistributionEKS},
	{"eks.amazonaws.com/capacityType", DistributionEKS},
	{"cloud.google.com/gke-nodepool", DistributionGKE},
	{"kubernetes.azure.com/cluster", DistributionAKS},
	{"doks.digitalocean.com/node-pool", DistributionDOKS},
	{"oke.oraclecloud.com/node.info.private_subnet", DistributionOKE},
	{"node.openshift.io/os_id", DistributionOpenShift},
	{"node-role.kubernetes.io/microk8s-controlplane", DistributionMicroK8s},
	{"minikube.k8s.io/name", DistributionMinikube},
	{"node.kubernetes.io/instance-type=k3s", DistributionK3s},
}

// versionMarkers are substrings distributions stamp into the kubelet version.
var versionMarkers = []struct {
	fragment     string
	distribution string
}{
	{"-eks-", DistributionEKS},
	{"-gke.", DistributionGKE},
	{"+rke2", DistributionRKE2},
	{"+k3s", DistributionK3s},
	{"-doks", DistributionDOKS},
	{"+microk8s", DistributionMicroK8s},
}

// distributionOf names the Kubernetes distribution a node runs.
//
// Three sources in falling order of certainty: a label only one distribution sets, a fragment
// stamped into the kubelet version, and the OS image for the distributions that are an operating
// system. A node that matches none is reported as plain `kubernetes` rather than empty — the
// question was answered, and the answer is that it is not a recognised distribution.
//
// This is the one judgement in this package that is deliberately about Kubernetes: the plane is
// never told what a distribution is, only handed the string as an observed cluster attribute.
func distributionOf(labels map[string]string, kubeletVersion, osImage string) string {
	for _, marker := range distributionMarkers {
		if key, want, pinned := strings.Cut(marker.label, "="); pinned {
			if labels[key] == want {
				return marker.distribution
			}
			continue
		}
		if _, ok := labels[marker.label]; ok {
			return marker.distribution
		}
	}
	version := strings.ToLower(kubeletVersion)
	for _, marker := range versionMarkers {
		if strings.Contains(version, marker.fragment) {
			return marker.distribution
		}
	}
	if strings.Contains(strings.ToLower(osImage), "talos") {
		return DistributionTalos
	}
	if kubeletVersion == "" {
		return ""
	}
	return DistributionVanilla
}

// taints converts discovered taints into their wire form.
func taints(in []Taint) []wire.NodeTaint {
	out := make([]wire.NodeTaint, 0, len(in))
	for _, t := range in {
		out = append(out, wire.NodeTaint{Key: t.Key, Value: t.Value, Effect: t.Effect})
	}
	return out
}
