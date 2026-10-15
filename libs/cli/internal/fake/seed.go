package fake

import (
	"encoding/json"
	"fmt"
	"time"

	"github.com/hangar-dev/hangar/libs/cli/internal/api"
	"strings"
)

// seed builds the offline control plane. It is deterministic in shape and
// relative in time: ids are prefixed lowercase 30-character strings like the
// plane's, and ages are offsets from now so no fixture ever has to be
// regenerated to stay plausible.
//
// The corpus is deliberately awkward — an unreachable cluster, a cordoned
// node, an unhealthy device, a throttled one, a deployment that never got
// admitted, a quota over its limit — because a seed where everything is fine
// tests nothing that matters.
func seed() *state {
	now := time.Now().UTC()
	st := &state{}

	counter := 0
	uid := func(prefix string) string {
		counter++
		return fmt.Sprintf("%s01m0dj%020d", prefix, counter)
	}

	type clusterSpec struct {
		name, region, zone, phase, residency string
		reachable                            bool
		syncState                            string
		agent                                string
		lastError                            string
		nodes                                int
		class                                string
		perNode                              int
	}
	specs := []clusterSpec{
		{"iad-1", "us-east-1", "us-east-1b", api.PhaseActive, "us", true, "connected", "0.4.2", "", 3, "h100-80", 8},
		{"sjc-1", "us-west-2", "", api.PhaseActive, "us", false, "disconnected", "0.4.1", "stream closed by peer", 2, "l40s-48", 4},
		{"ams-1", "eu-west-1", "euw1-az1", api.PhaseDraining, "eu", true, "connected", "0.4.2", "", 1, "a100-80", 8},
		{"sin-1", "ap-southeast-1", "", api.PhasePending, "apac", false, "never_connected", "", "", 0, "", 0},
	}

	for _, spec := range specs {
		clusterID := uid("clu_")
		display := spec.name + " region"
		enrolled := now.Add(-time.Duration(210*24) * time.Hour)
		sync := api.ClusterSync{State: spec.syncState}
		if spec.syncState != "never_connected" {
			sync.LastSyncAt = ptr(now.Add(-12 * time.Second))
			sync.LastSeenAt = ptr(now.Add(-8 * time.Second))
			sync.OperatorVersion = strptr(spec.agent)
			sync.LagMS = intptr(340)
		}
		if spec.lastError != "" {
			sync.LastError = strptr(spec.lastError)
			sync.LastSyncAt = ptr(now.Add(-11 * time.Minute))
		}
		cluster := api.Cluster{
			ID:           clusterID,
			Name:         spec.name,
			DisplayName:  strptr(display),
			Region:       spec.region,
			Zone:         nilIfEmpty(spec.zone),
			Residency:    nilIfEmpty(spec.residency),
			Phase:        spec.phase,
			Labels:       map[string]any{"tier": "prod", "fabric": "nvswitch"},
			Capabilities: map[string]any{"nvlink": true, "rdma": true, "mig": spec.class == "h100-80"},
			EnrolledAt:   ptr(enrolled),
			CreatedAt:    enrolled,
			Revision:     revision(enrolled),
			Reachable:    spec.reachable,
			Sync:         sync,
			MetricsLabel: strptr(spec.name),
		}
		st.Clusters = append(st.Clusters, cluster)
		if spec.nodes == 0 {
			continue
		}

		poolID := uid("pol_")
		poolName := spec.class + "-prod"
		membership := api.PoolMembership{}

		for i := 1; i <= spec.nodes; i++ {
			nodeID := uid("nod_")
			nodeName := fmt.Sprintf("%s-n%02d", spec.name, i)
			cordoned := spec.phase == api.PhaseDraining && i == 1
			condition := api.NodeReady
			ready := true
			if spec.name == "sjc-1" && i == 2 {
				condition = api.NodeNotReady
				ready = false
			}
			byHealth := map[string]int{api.HealthHealthy: spec.perNode}
			if i == 1 {
				byHealth[api.HealthHealthy] = spec.perNode - 1
				byHealth[api.HealthUnhealthy] = 1
			}

			domainID := uid("tpd_")
			domainKind := "nvswitch"
			if spec.class == "l40s-48" {
				domainKind = "pcie"
			}
			st.TopologyDomains = append(st.TopologyDomains, api.TopologyDomain{
				ID:                domainID,
				Cluster:           clusterID,
				ClusterName:       spec.name,
				Nodes:             []string{nodeID},
				NodeNames:         []string{nodeName},
				Kind:              domainKind,
				Identifier:        nodeName + "/" + domainKind + "-0",
				LinkGeneration:    strptr("4"),
				LinkBandwidthGbps: intptr(900),
				DeviceCount:       spec.perNode,
				Attributes:        map[string]any{"switches": 4},
				ObservedAt:        ptr(now.Add(-20 * time.Second)),
				CreatedAt:         enrolled,
			})

			st.Nodes = append(st.Nodes, api.Node{
				ID:              nodeID,
				Name:            nodeName,
				Cluster:         clusterID,
				ClusterName:     spec.name,
				Pool:            strptr(poolID),
				PoolName:        strptr(poolName),
				Condition:       condition,
				Cordoned:        cordoned,
				Ready:           ready,
				CPUCores:        intptr(192),
				MemoryMiB:       intptr(2097152),
				CPUArch:         strptr("amd64"),
				OSImage:         strptr("Ubuntu 22.04.4 LTS"),
				KernelVersion:   strptr("5.15.0-119-generic"),
				DriverVersion:   strptr("550.90.07"),
				OperatorVersion: strptr(spec.agent),
				Labels:          map[string]any{"tier": "prod", "fabric": domainKind},
				Taints:          []any{},
				Conditions:      []api.NodeCondition{},
				Devices:         api.NodeDeviceRollup{Total: spec.perNode, ByHealth: byHealth, DeviceClass: strptr(spec.class)},
				JoinedAt:        ptr(enrolled.Add(48 * time.Hour)),
				ObservedAt:      ptr(now.Add(-20 * time.Second)),
				CreatedAt:       enrolled,
			})

			membership.NodesTotal++
			if ready {
				membership.NodesReady++
			}
			if cordoned {
				membership.NodesCordoned++
			}

			for index := 0; index < spec.perNode; index++ {
				health := api.HealthHealthy
				var reason *string
				allocatable := true
				if i == 1 && index == spec.perNode-1 {
					health = api.HealthUnhealthy
					reason = strptr("Xid 79: GPU has fallen off the bus")
					allocatable = false
				}
				throttled := i == 2 && index == 0
				var throttleReasons []string
				if throttled {
					throttleReasons = []string{"sw_power_cap"}
				}
				st.Devices = append(st.Devices, api.Device{
					ID:                 uid("dev_"),
					Cluster:            clusterID,
					ClusterName:        spec.name,
					Node:               nodeID,
					NodeName:           nodeName,
					DeviceUUID:         fmt.Sprintf("GPU-%s-%02d", nodeName, index),
					DeviceIndex:        index,
					DeviceClass:        spec.class,
					Vendor:             "nvidia",
					ModelName:          strptr("NVIDIA " + spec.class),
					PCIAddress:         strptr(fmt.Sprintf("0000:%02x:00.0", 16+index)),
					MemoryMiB:          81920,
					ComputeCapability:  strptr("9.0"),
					Health:             health,
					HealthReason:       reason,
					ECCEnabled:         true,
					ECCVolatileErrors:  0,
					ECCAggregateErrors: 12,
					Throttled:          throttled,
					ThrottleReasons:    throttleReasons,
					Allocatable:        allocatable,
					Topology:           &api.DeviceTopology{ID: domainID, Kind: domainKind, Identifier: nodeName + "/" + domainKind + "-0"},
					ObservedAt:         ptr(now.Add(-20 * time.Second)),
					CreatedAt:          enrolled,
				})
				membership.DevicesTotal++
				if health == api.HealthHealthy {
					membership.DevicesHealthy++
				}
				if allocatable {
					membership.DevicesAllocatable++
				}
			}
		}

		st.Pools = append(st.Pools, api.Pool{
			ID:                  poolID,
			ClusterID:           clusterID,
			Name:                poolName,
			DeviceClass:         spec.class,
			DevicesPerNode:      spec.perNode,
			NodeShape:           map[string]any{"sku": fmt.Sprintf("%dx%s", spec.perNode, spec.class), "cpu": 192, "memory_gib": 2048},
			MinNodes:            1,
			MaxNodes:            16,
			Labels:              map[string]any{"tier": "prod"},
			Taints:              []any{},
			Enabled:             spec.phase != api.PhaseDisabled,
			CreatedAt:           enrolled,
			Revision:            revision(enrolled),
			Membership:          membership,
			WithinDeclaredBound: membership.NodesTotal >= 1,
		})

		st.Components = append(st.Components, api.Component{
			ID:             uid("cmp_"),
			ClusterID:      clusterID,
			Name:           "scheduler",
			Kind:           "scheduler",
			DesiredVersion: "1.4.0",
			Config:         map[string]any{},
			Enabled:        true,
			CreatedAt:      enrolled,
			Revision:       revision(enrolled),
			Status: api.ComponentStatus{
				ObservedVersion: strptr("1.3.2"),
				RolloutPhase:    "rolling",
				Healthy:         spec.reachable,
				ObservedAt:      ptr(now.Add(-time.Minute)),
			},
			Behind: true,
		})
	}

	tenant := api.ProjectTenant{ID: uid("ten_"), Slug: "acme", Name: "Acme", Enabled: true}
	projectID := uid("prj_")
	created := now.Add(-90 * 24 * time.Hour)
	st.Tenants = append(st.Tenants, api.Tenant{
		ID:           tenant.ID,
		Slug:         tenant.Slug,
		Name:         tenant.Name,
		Enabled:      tenant.Enabled,
		ProjectCount: 1,
		CreatedAt:    now.Add(-90 * 24 * time.Hour),
		Revision:     revision(now.Add(-90 * 24 * time.Hour)),
	})
	st.Projects = append(st.Projects, api.Project{
		Tenant:               tenant,
		ID:                   projectID,
		Slug:                 "core",
		Name:                 "Core",
		DefaultPriorityClass: strptr("production"),
		Enabled:              true,
		EffectiveEnabled:     true,
		CreatedAt:            created,
		Revision:             revision(created),
	})

	st.PriorityClasses = append(st.PriorityClasses,
		api.PriorityClass{ID: uid("prc_"), Name: "production", Value: 100, Preemptible: false, Preempts: true, IsDefault: true, CreatedAt: created, Revision: revision(created)},
		api.PriorityClass{ID: uid("prc_"), Name: "batch", Value: 10, Preemptible: true, Preempts: false, CreatedAt: created, Revision: revision(created)},
	)

	st.Policies = append(st.Policies, api.Policy{
		ID:                   uid("plc_"),
		Name:                 "eu-only",
		Description:          strptr("keep eu tenants in eu regions"),
		AllowedRegions:       []string{"eu-west-1"},
		AllowedResidencies:   []string{"eu"},
		AllowedDeviceClasses: []string{},
		AllowedClusters:      []string{},
		DeniedClusters:       []string{},
		Rules:                map[string]any{},
		Enabled:              true,
		CreatedAt:            created,
		Revision:             revision(created),
		Attachments: []api.PolicyAttachment{{
			SubjectType: "project",
			SubjectID:   "1f0d1f34-1a2b-4c3d-8e9f-0a1b2c3d4e5f",
			Subject:     strptr(projectID),
			SubjectName: strptr("core"),
		}},
	})

	artifactID := uid("art_")
	st.Artifacts = append(st.Artifacts, api.Artifact{
		ID:             artifactID,
		Digest:         "sha256:7b1f0c0a2e4d6f8091a2b3c4d5e6f70819a2b3c4d5e6f70819a2b3c4d5e6f708",
		Name:           "llama-3.1-70b-instruct",
		Slug:           "meta-llama/llama-3.1-70b-instruct",
		Kind:           "weights",
		Format:         strptr("safetensors"),
		SizeBytes:      141_000_000_000,
		Licence:        strptr("llama-3.1"),
		ParameterCount: int64ptr(70_000_000_000),
		Metadata:       map[string]any{"family": "llama"},
		CreatedAt:      created,
		Revision:       revision(created),
		Locations: []api.ArtifactLocation{
			{Scheme: "s3", URI: "s3://hangar-weights/llama-3.1-70b", Region: strptr("us-east-1"), Preferred: true},
		},
		Upstream: api.ArtifactUpstream{
			Provider: strptr("meta-llama"), ProviderModelID: strptr("Llama-3.1-70B-Instruct"),
			Licence: strptr("llama-3.1"), ContextLength: intptr(131072),
			InputModalities: []string{"text"}, StructuredOutputModes: []string{"text", "json_schema"},
			ToolCalling: boolptr(true),
		},
	})

	// The serving configuration is the workload's own. There is no object to
	// point at and nothing to share: two workloads configured this way carry
	// the same serving_digest, and that is all sharing ever meant.
	serving := normalizeServing(api.ServingSpec{
		ArtifactID:           artifactID,
		Engine:               "vllm",
		EngineRuntimeVersion: "0.6.3",
		EngineArgs:           map[string]any{},
		ImportedArgs:         map[string]any{"--max-num-seqs": "256"},
		Dtype:                strptr("bfloat16"),
		Parallelism:          api.ServingParallelism{Tensor: 8, Pipeline: 1, Expert: 1},
		ContextLength:        131072,
		MaxBatchTokens:       intptr(65536),
		Cache: api.ServingCache{
			KVCacheDtype:       strptr("fp8"),
			KVCacheFraction:    floatptr(0.9),
			PrefixCacheEnabled: true,
			PrefixCacheConfig:  map[string]any{},
		},
		Shape: api.ServingShape{
			DeviceClass:                  "h100-80",
			DeviceCount:                  8,
			DeviceMemoryMiBMin:           81920,
			RequiresSingleTopologyDomain: true,
			TopologyDomainKind:           strptr("nvswitch"),
		},
		Resources: api.ServingResources{CPUCores: intptr(32), MemoryMiB: intptr(262144), DiskGiB: intptr(512)},
		Capabilities: api.ServingCapabilities{
			StreamingSupported:    true,
			StructuredOutputModes: []string{"text", "json_schema"},
			ToolCallingSupported:  true,
			ToolChoiceModes:       []string{"auto", "none"},
			InputModalities:       []string{"text"},
			PromptCacheMode:       "off",
		},
	})
	digest := servingDigest(serving)

	iad := st.Clusters[0].ID
	sjc := st.Clusters[1].ID
	depRunning := uid("dep_")
	depPending := uid("dep_")
	// One workload across two clusters, which is the shape the whole model exists
	// for: a total of 5 replicas that placement split 4/1, so the console and the
	// CLI both have something whose scale is a sum rather than a copy.
	workloadID := uid("wkl_")
	st.Workloads = append(st.Workloads, api.Workload{
		ID: workloadID, Name: "chat", Kind: "service", ProjectID: projectID,
		PriorityClass: "production", GangSize: 1,
		ServingDigest:      digest,
		Serving:            serving,
		AdapterArtifactIDs: []string{}, Overrides: map[string]any{},
		Labels:        map[string]any{"tier": "prod"},
		Strategy:      "spread",
		ClusterFloors: map[string]any{},
		Scale: api.WorkloadScale{
			Unit: "replicas", Min: 5, Max: 5, Desired: 5,
		},
		StartedAt: ptr(created), CreatedAt: created, Revision: revision(created),
		Deployments: []api.WorkloadDeployment{},
	})

	st.Deployments = append(st.Deployments,
		api.Deployment{
			ID: depRunning, Name: "chat", WorkloadID: workloadID, WorkloadName: "chat", ClusterID: iad,
			MinReplicas: 4, MaxReplicas: 4, DesiredReplicas: 4,
			AdmissionPhase: "running",
			QueuedAt:       ptr(created), AdmittedAt: ptr(created), PlacedAt: ptr(created),
			Generation: 7, Enabled: true, Authority: "managed",
			CreatedAt: created, Revision: revision(created),
			Replicas: api.DeploymentReplicaRollup{Total: 4, Ready: 3, Running: 4, AtCurrentGeneration: 3},
			Status: &api.DeploymentStatus{
				ObservedGeneration: 7, Phase: "running", DesiredReplicas: 4, ReadyReplicas: 3,
				UpdatedReplicas: 3, UnavailableReplicas: 1, ObservedAt: ptr(now.Add(-30 * time.Second)),
			},
		},
		api.Deployment{
			ID: depPending, Name: "chat", WorkloadID: workloadID, WorkloadName: "chat", ClusterID: sjc,
			MinReplicas: 1, MaxReplicas: 1, DesiredReplicas: 1,
			AdmissionPhase:  "pending",
			AdmissionReason: strptr("no device class in this cluster satisfies the workload's shape"),
			QueuedAt:        ptr(now.Add(-2 * time.Hour)),
			Generation:      1, Enabled: true, Authority: "managed",
			CreatedAt: now.Add(-2 * time.Hour), Revision: revision(now.Add(-2 * time.Hour)),
			Replicas: api.DeploymentReplicaRollup{},
			// The state a deployment is actually in for most of the time somebody is watching it:
			// declared, reached its cluster, and waiting on an ordering that is nobody's fault.
			Status: &api.DeploymentStatus{
				ObservedGeneration: 1, Phase: "pending",
				Reason:          strptr("WeightsNotResident"),
				Message:         strptr("waiting for art_01j9x2aaaaaaaaaaaaaaaaaaaa to finish downloading"),
				DesiredReplicas: 1, UnavailableReplicas: 1,
				ObservedAt: ptr(now.Add(-45 * time.Second)),
			},
		},
	)

	chatEndpoint := uid("end_")
	st.Endpoints = append(st.Endpoints, api.Endpoint{
		ID: chatEndpoint, Name: "chat", Slug: "meta-llama/llama-3.3-70b-instruct", ProjectID: projectID,
		Visibility: "private", RoutingStrategy: "weighted", Enabled: true,
		Aliases: []string{"llama-3.3-70b"},
		Targets: []api.EndpointTarget{
			{DeploymentID: depRunning, Weight: 90, Role: "primary", Enabled: true},
			{DeploymentID: depPending, Weight: 10, Role: "canary", Enabled: false},
		},
		FailoverOrder: []string{depRunning, depPending},
		Surface: api.EndpointSurface{
			Protocols:            []string{"openai.chat"},
			AuthMode:             "api_key",
			ErrorShape:           "passthrough",
			StreamingTransport:   "sse",
			OutputLengthParams:   []string{"max_tokens"},
			CORSEnabled:          &surfaceCORS,
			MaxRequestBytes:      &surfaceMaxBytes,
			RequestTimeoutSecond: &surfaceTimeout,
			Policy: &api.EndpointPolicy{
				Version: 2,
				Rules: []api.PolicyRule{
					{Route: "/v1/chat/completions", Key: "temperature", Enum: []any{1}},
				},
			},
		},
		Labels:    map[string]any{},
		CreatedAt: created,
		Revision:  revision(created),
	})

	// Five models across two clusters, none of them declared here. This is what a plane looks
	// like the day it is pointed at clusters that were already serving.
	found := now.Add(-30 * 24 * time.Hour)
	st.Discovered = append(st.Discovered,
		discovered(uid("dsw_"), iad, "iad-1", "serving", "llama-70b-vllm", "meta-llama/Llama-3.1-70B-Instruct",
			"ghcr.io/acme/vllm:0.9.1", 2, 2, 4, found, "new", nil),
		discovered(uid("dsw_"), iad, "iad-1", "serving", "mixtral-8x7b", "mistralai/Mixtral-8x7B-Instruct-v0.1",
			"ghcr.io/acme/vllm:0.9.1", 1, 1, 2, found, "tracked", strptr(projectID)),
		discovered(uid("dsw_"), iad, "iad-1", "research", "sd3-batch", "",
			"ghcr.io/acme/comfy:1.4.0", 1, 0, 1, now.Add(-3*24*time.Hour), "new", nil),
		discovered(uid("dsw_"), sjc, "sjc-1", "serving", "qwen2-72b", "Qwen/Qwen2-72B-Instruct",
			"ghcr.io/acme/sglang:0.4.2", 2, 2, 4, found, "new", nil),
		discovered(uid("dsw_"), sjc, "sjc-1", "serving", "embed-bge-m3", "BAAI/bge-m3",
			"ghcr.io/acme/tei:1.5.0", 3, 3, 1, found, "ignored", nil),
	)

	st.Quotas = append(st.Quotas,
		api.Quota{
			ID: uid("qta_"), Project: projectID, ProjectSlug: "core",
			Cluster: strptr(iad), ClusterName: strptr("iad-1"), DeviceClass: "h100-80",
			GuaranteedDevices: 16, LimitDevices: intptr(24), OverQuotaWeight: 1.0, OverQuotaEnabled: true,
			CreatedAt: created, Revision: revision(created),
			Usage:           api.QuotaUsage{AllocatedDevices: 32, LiveAllocations: 4, OverQuotaDevices: 16},
			HeadroomDevices: intptr(-8), OverLimit: true,
		},
		api.Quota{
			ID: uid("qta_"), Project: projectID, ProjectSlug: "core",
			DeviceClass: "l40s-48", GuaranteedDevices: 4, OverQuotaWeight: 0.5, OverQuotaEnabled: false,
			CreatedAt: created, Revision: revision(created),
			Usage: api.QuotaUsage{AllocatedDevices: 2, LiveAllocations: 1},
		},
	)

	servingQuota := api.ServingQuota{
		ID: uid("svq_"), Endpoint: chatEndpoint, EndpointName: "chat", Project: projectID,
		CommittedRPM: intptr(1_000), CommittedTPM: int64ptr(10_000_000),
		MaxConcurrentRequests: intptr(256), MaxOutputTokens: intptr(4_096),
		BurstRPM: intptr(1_500), RampUpSeconds: 30, OnExceed: api.OnExceedReject,
		LimitScope: api.ScopePerReplica, SuccessRateObjective: floatptr(0.999),
		AvailabilityObjective: defaultAvailabilityObjective, ObjectiveWindowSeconds: 600,
		Enforced: true, CreatedAt: created, Revision: revision(created),
	}
	servingQuota.Standard = standardOf(servingQuota)
	st.ServingQuotas = append(st.ServingQuotas, servingQuota)

	st.AlertThresholds = append(st.AlertThresholds, api.AlertThreshold{
		ID: uid("alt_"), Rule: "deployment.output_speed_below_floor",
		Metric: "inference.output_tokens_per_stream", Comparison: "below",
		Subject: api.AlertSubject{Type: "deployment", ID: strptr(depRunning)},
		Value:   30, Severity: strptr("warning"), Lookback: strptr("5m"),
		ForSeconds: intptr(900), Enabled: true,
		Reason:    strptr("acceptance floor for a served model"),
		CreatedAt: created,
	})

	firstNode := st.Nodes[0]
	held := make([]api.AllocationDevice, 0, 4)
	for _, d := range st.Devices {
		if d.Node == firstNode.ID && len(held) < 4 {
			held = append(held, api.AllocationDevice{Device: d.ID, DeviceUUID: d.DeviceUUID, Node: d.Node, Live: true})
		}
	}
	st.Allocations = append(st.Allocations,
		api.Allocation{
			ID: uid("alc_"), Cluster: iad, ClusterName: "iad-1", Project: projectID, ProjectSlug: "core",
			Workload: uid("wkl_"), DeviceClass: "h100-80", DeviceCount: 8, Phase: "held", Live: true,
			GangComplete: false, AcquiredAt: ptr(now.Add(-6 * time.Hour)), ObservedAt: ptr(now.Add(-time.Minute)),
			CreatedAt: now.Add(-6 * time.Hour), Devices: held,
		},
		api.Allocation{
			ID: uid("alc_"), Cluster: sjc, ClusterName: "sjc-1", Project: projectID, ProjectSlug: "core",
			Workload: uid("wkl_"), DeviceClass: "l40s-48", DeviceCount: 2, OverQuota: true, Phase: "released",
			Live: false, GangComplete: true, AcquiredAt: ptr(now.Add(-30 * time.Hour)), ReleasedAt: ptr(now.Add(-2 * time.Hour)),
			CreatedAt: now.Add(-30 * time.Hour), Devices: []api.AllocationDevice{},
		},
	)

	drainTarget := st.Nodes[len(st.Nodes)-1]
	cordonTarget, failedDrain := st.Nodes[0], st.Nodes[0]
	for i := len(st.Nodes) - 1; i >= 0; i-- {
		switch n := st.Nodes[i]; {
		case n.Cluster == iad && n.Cordoned:
			cordonTarget = n
		case n.Cluster == sjc:
			failedDrain = n
		}
	}
	st.Operations = append(st.Operations,
		api.Operation{
			ID: uid("opr_"), ClusterID: strptr(drainTarget.Cluster), Type: "node.drain",
			Target: api.OperationTarget{Type: "node", ID: drainTarget.ID}, Reason: strptr("firmware"),
			RequestedBy: "operator", Parameters: map[string]any{"grace_seconds": 600},
			CreatedAt: now.Add(-4 * time.Minute), Phase: api.OpRunning, Attempts: 1,
			StartedAt: ptr(now.Add(-3 * time.Minute)),
		},
		api.Operation{
			ID: uid("opr_"), ClusterID: strptr(iad), Type: "node.cordon",
			Target: api.OperationTarget{Type: "node", ID: cordonTarget.ID}, Reason: strptr("ecc"),
			RequestedBy: "operator", Parameters: map[string]any{"cordon": "true"}, CreatedAt: now.Add(-3 * time.Hour),
			Phase: api.OpSucceeded, Attempts: 1, StartedAt: ptr(now.Add(-3 * time.Hour)),
			FinishedAt: ptr(now.Add(-3 * time.Hour)), Result: map[string]any{"cordoned": true},
		},
		api.Operation{
			ID: uid("opr_"), ClusterID: strptr(sjc), Type: "node.drain",
			Target: api.OperationTarget{Type: "node", ID: failedDrain.ID},
			Reason: strptr("kernel upgrade"), RequestedBy: "operator", Parameters: map[string]any{"grace_seconds": 600},
			CreatedAt: now.Add(-26 * time.Hour), Phase: api.OpFailed, Attempts: 3,
			Error: strptr("eviction blocked by a pod disruption budget"), StartedAt: ptr(now.Add(-26 * time.Hour)),
			FinishedAt: ptr(now.Add(-25 * time.Hour)),
		},
	)

	// Two runs against one deployment at different concurrencies, so that the
	// comparison the table exists for is visible offline, and one failure, so
	// that the empty-result path is too. The summaries are AIPerf's own shape
	// rather than anything tidier: a fixture with an invented schema would make
	// the generic renderer look better than it is against a real harness.
	benchDeployment := st.Deployments[0]
	benchDeploymentID, benchDeploymentCluster := benchDeployment.ID, benchDeployment.ClusterID
	st.Benchmarks = append(st.Benchmarks,
		api.BenchmarkRun{
			ID: uid("bmk_"), DeploymentID: &benchDeploymentID, ClusterID: &benchDeploymentCluster,
			Harness: "aiperf", HarnessVersion: strptr("0.4.1"), Image: fakeAiperfImage, RequestedBy: "operator",
			Reason: strptr("concurrency sweep, step 3"),
			Parameters: map[string]any{
				"concurrency": 64, "request_count": 500, "input_tokens_mean": 2000,
				"output_tokens_mean": 256, "endpoint_type": "chat", "streaming": true,
			},
			Provenance: api.BenchmarkProvenance{
				Model: benchDeployment.Name, DeploymentGeneration: int64(benchDeployment.Generation),
				ServingDigest: &digest,
				EngineArgs:    map[string]any{"--max-num-seqs": 256},
			},
			Phase: api.OpSucceeded, CreatedAt: now.Add(-4 * time.Hour),
			StartedAt: ptr(now.Add(-4 * time.Hour)), FinishedAt: ptr(now.Add(-3*time.Hour - 50*time.Minute)),
			Summary: json.RawMessage(`{"time_to_first_token":{"unit":"ms","avg":412.7,"min":188.4,"max":1904.2,"p50":388.1,"p90":671.5,"p99":1402.6},"inter_token_latency":{"unit":"ms","avg":18.4,"p50":17.6,"p90":24.1,"p99":41.3},"output_token_throughput":{"unit":"tokens/sec","avg":3208.1},"request_count":500,"error_count":0}`),
		},
		api.BenchmarkRun{
			ID: uid("bmk_"), DeploymentID: &benchDeploymentID, ClusterID: &benchDeploymentCluster,
			Harness: "aiperf", HarnessVersion: strptr("0.4.1"), Image: fakeAiperfImage, RequestedBy: "operator",
			Reason: strptr("concurrency sweep, step 1"),
			Parameters: map[string]any{
				"concurrency": 16, "request_count": 500, "input_tokens_mean": 2000,
				"output_tokens_mean": 256, "endpoint_type": "chat", "streaming": true,
			},
			Provenance: api.BenchmarkProvenance{
				Model: benchDeployment.Name, DeploymentGeneration: int64(benchDeployment.Generation),
				ServingDigest: &digest,
				EngineArgs:    map[string]any{"--max-num-seqs": 256},
			},
			Phase: api.OpSucceeded, CreatedAt: now.Add(-5 * time.Hour),
			StartedAt: ptr(now.Add(-5 * time.Hour)), FinishedAt: ptr(now.Add(-4*time.Hour - 52*time.Minute)),
			Summary: json.RawMessage(`{"time_to_first_token":{"unit":"ms","avg":174.2,"min":92.8,"max":611.4,"p50":166.9,"p90":248.3,"p99":448.1},"inter_token_latency":{"unit":"ms","avg":11.1,"p50":10.8,"p90":13.9,"p99":21.7},"output_token_throughput":{"unit":"tokens/sec","avg":1412.6},"request_count":500,"error_count":0}`),
		},
		api.BenchmarkRun{
			ID: uid("bmk_"), DeploymentID: &benchDeploymentID, ClusterID: &benchDeploymentCluster,
			Harness: "aiperf", Image: fakeAiperfImage, RequestedBy: "operator", Reason: strptr("long-context saturation"),
			Parameters: map[string]any{
				"concurrency": 128, "request_count": 1000, "input_tokens_mean": 8000,
				"output_tokens_mean": 512, "endpoint_type": "chat", "streaming": true,
			},
			Provenance: api.BenchmarkProvenance{
				Model: benchDeployment.Name, DeploymentGeneration: int64(benchDeployment.Generation),
				ServingDigest: &digest,
				EngineArgs:    map[string]any{},
			},
			Phase: api.OpFailed, CreatedAt: now.Add(-90 * time.Minute),
			StartedAt: ptr(now.Add(-89 * time.Minute)), FinishedAt: ptr(now.Add(-58 * time.Minute)),
			Error: strptr("the benchmark job hit its deadline before reporting a result"),
		},
	)

	st.Events = append(st.Events,
		api.Event{
			ID: "evt_01j9x2e0000000000000000001", Kind: "plane.started", Severity: "info",
			Subject: api.EventSubject{Type: "plane"}, Message: "control plane started",
			Details: map[string]any{"version": "0.1.0"}, OccurredAt: now.Add(-5 * time.Hour), RecordedAt: now.Add(-5 * time.Hour),
		},
		api.Event{
			ID: "evt_01j9x2e0000000000000000002", ClusterID: strptr(iad), Kind: "device.unhealthy",
			Severity: "error", Subject: api.EventSubject{Type: "device", ID: strptr("809bd430-0c48-40eb-be06-913bbbb181c8")},
			Message: "Xid 79 on " + firstNode.Name, Details: map[string]any{"xid": 79},
			OccurredAt: now.Add(-42 * time.Minute), RecordedAt: now.Add(-42 * time.Minute),
		},
		api.Event{
			ID: "evt_01j9x2e0000000000000000003", ClusterID: strptr(sjc), Kind: "cluster.disconnected",
			Severity: "warning", Subject: api.EventSubject{Type: "cluster", ID: strptr("fb690d40-2fe5-448a-b92c-7a33d14552c9")},
			Message: "sjc-1 stream closed by peer", Details: map[string]any{},
			OccurredAt: now.Add(-11 * time.Minute), RecordedAt: now.Add(-11 * time.Minute),
		},
	)

	// One deployment's whole timeline, so `hgr deployment events` has something to
	// show offline. Seeded as the awkward case — weights that took most of an hour,
	// then a rollout that filled in one replica at a time — because a deployment
	// that came up instantly would not exercise what the command exists to reveal.
	st.Events = append(st.Events,
		api.Event{
			ID: "evt_01j9x2e0000000000000000010", ClusterID: strptr(iad), Kind: "deployment.created",
			Severity: "info", Subject: api.EventSubject{Type: "deployment", ID: strptr(depRunning)},
			Message: "chat was created on iad-1 from artifact llama-3.1-70b-instruct",
			Details: map[string]any{
				"deployment_id": depRunning, "name": "chat",
				"cluster": "iad-1", "desired_replicas": "4",
			},
			OccurredAt: now.Add(-100 * time.Minute), RecordedAt: now.Add(-100 * time.Minute),
		},
		api.Event{
			ID: "evt_01j9x2e0000000000000000011", ClusterID: strptr(iad), Kind: "deployment.dispatched",
			Severity: "info", Subject: api.EventSubject{Type: "deployment", ID: strptr(depRunning)},
			Message:    "chat was sent to iad-1",
			Details:    map[string]any{"deployment_id": depRunning, "name": "chat", "frames": "4"},
			OccurredAt: now.Add(-100 * time.Minute), RecordedAt: now.Add(-100 * time.Minute),
		},
		api.Event{
			ID: "evt_01j9x2e0000000000000000012", ClusterID: strptr(iad), Kind: "deployment.pending",
			Severity: "info", Subject: api.EventSubject{Type: "deployment", ID: strptr(depRunning)},
			Message: "waiting for art_01j9x2eeeeeeeeeeeeeeeeeeee to finish downloading",
			Details: map[string]any{
				"deployment_id": depRunning, "name": "chat", "reason": "WeightsNotResident",
				"ready_replicas": "0", "desired_replicas": "4",
			},
			OccurredAt: now.Add(-99 * time.Minute), RecordedAt: now.Add(-99 * time.Minute),
		},
		api.Event{
			ID: "evt_01j9x2e0000000000000000013", ClusterID: strptr(iad), Kind: "deployment.placed",
			Severity: "info", Subject: api.EventSubject{Type: "deployment", ID: strptr(depRunning)},
			Message: "serving llama-70b at revision 3",
			Details: map[string]any{
				"deployment_id": depRunning, "name": "chat", "reason": "Applied",
				"ready_replicas": "0", "desired_replicas": "4",
			},
			OccurredAt: now.Add(-52 * time.Minute), RecordedAt: now.Add(-52 * time.Minute),
		},
		api.Event{
			ID: "evt_01j9x2e0000000000000000014", ClusterID: strptr(iad), Kind: "deployment.running",
			Severity: "info", Subject: api.EventSubject{Type: "deployment", ID: strptr(depRunning)},
			Message: "serving llama-70b at revision 3",
			Details: map[string]any{
				"deployment_id": depRunning, "name": "chat", "reason": "Applied",
				"ready_replicas": "4", "desired_replicas": "4",
			},
			OccurredAt: now.Add(-30 * time.Minute), RecordedAt: now.Add(-30 * time.Minute),
		},
	)

	// An image model, for the omni engine. It is seeded last so the ids above
	// stay what the fixtures expect.
	st.Artifacts = append(st.Artifacts, api.Artifact{
		ID:             uid("art_"),
		Digest:         "sha256:3c9e1d2f4a5b6c7d8e9f0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f",
		Name:           "flux.1-schnell",
		Slug:           "black-forest-labs/flux.1-schnell",
		Kind:           "weights",
		Format:         strptr("safetensors"),
		SizeBytes:      23_800_000_000,
		Licence:        strptr("apache-2.0"),
		ParameterCount: int64ptr(12_000_000_000),
		Metadata:       map[string]any{"family": "flux"},
		CreatedAt:      created,
		Revision:       revision(created),
		Locations: []api.ArtifactLocation{
			{Scheme: "s3", URI: "s3://hangar-weights/flux.1-schnell", Region: strptr("us-east-1"), Preferred: true},
		},
		Upstream: api.ArtifactUpstream{
			Provider: strptr("black-forest-labs"), ProviderModelID: strptr("FLUX.1-schnell"),
			Licence: strptr("apache-2.0"), InputModalities: []string{"text"},
		},
	})

	return st
}

// revision mirrors the plane's revision token: microseconds since the epoch,
// opaque to the client and only ever compared or echoed back in `If-Match`.
func revision(t time.Time) string {
	return fmt.Sprintf("%d", t.UnixMicro())
}

// The declared halves of the seeded endpoint's surface, addressable because a surface field that
// nobody declared is nil and not a zero — three states, and the fake has to be able to express all
// three the way the plane does.
var (
	surfaceCORS     = false
	surfaceMaxBytes = 1048576
	surfaceTimeout  = 120
)

func ptr(t time.Time) *time.Time { return &t }

func boolptr(b bool) *bool { return &b }

func strptr(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

func nilIfEmpty(s string) *string { return strptr(s) }

func intptr(v int) *int { return &v }

func int64ptr(v int64) *int64 { return &v }

func floatptr(v float64) *float64 { return &v }

// discovered builds one sighting. Every field it fills is observed except the
// decision, which is the split the two tables behind this endpoint exist to keep.
func discovered(
	id, cluster, clusterName, namespace, name, model, image string,
	desired, ready, perReplica int,
	first time.Time,
	status string,
	project *string,
) api.DiscoveredWorkload {
	seen := time.Now().UTC().Add(-90 * time.Second)
	digest := "sha256:" + strings.Repeat(name+"0", 4)
	if len(digest) > 71 {
		digest = digest[:71]
	}
	w := api.DiscoveredWorkload{
		ID: id, ClusterID: cluster, ClusterName: clusterName,
		Namespace: namespace, Name: name, Kind: "deployment",
		Images:          []string{image},
		ReplicasDesired: desired, ReplicasReady: ready,
		DevicesPerReplica: perReplica, DevicesTotal: perReplica * ready,
		NodeIDs:         []string{},
		Labels:          map[string]any{"name": name},
		Status:          status,
		ProjectID:       project,
		SpecFingerprint: &digest,
		FirstSeenAt:     ptr(first),
		ObservedAt:      ptr(seen),
	}
	if model != "" {
		w.Model = strptr(model)
	}
	if status != "new" {
		decided := first.Add(time.Hour)
		who := "admin"
		w.DecidedAt, w.DecidedBy = ptr(decided), &who
	}
	return w
}
