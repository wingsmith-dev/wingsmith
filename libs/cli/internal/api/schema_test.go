package api

import (
	"encoding/json"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"
)

// testdata/plane-schemas.json is every component schema in the plane's
// OpenAPI document, reduced to field name and type. scripts/plane-schemas.py
// regenerates it from libs/plane without a database; run it after changing a
// plane schema and commit the result.
//
// wireTypes pairs each client type with the plane schema it mirrors. A read
// type must have exactly the schema's fields, so a field the plane added fails
// here instead of vanishing from -o json. A request type may send fewer fields
// than the plane accepts, but never one it does not.
var wireTypes = []struct {
	value   any
	schema  string
	request bool
}{
	{ClusterSync{}, "ClusterSyncSchema", false},
	{ClusterServing{}, "ClusterServingSchema", false},
	{Cluster{}, "ClusterSchema", false},
	{NodeDeviceRollup{}, "NodeDeviceRollupSchema", false},
	{NodeCondition{}, "NodeConditionSchema", false},
	{Node{}, "NodeSchema", false},
	{DeviceTopology{}, "DeviceTopologySchema", false},
	{Device{}, "DeviceSchema", false},
	{TopologyDomain{}, "TopologyDomainSchema", false},
	{PoolMembership{}, "PoolMembershipSchema", false},
	{DeviceClass{}, "DeviceClassSchema", false},
	{DeviceClassInventory{}, "DeviceClassInventorySchema", false},
	{Pool{}, "PoolSchema", false},
	{ArtifactLocation{}, "ArtifactLocationSchema", false},
	{ArtifactUpstream{}, "ArtifactUpstreamSchema", false},
	{Artifact{}, "ArtifactSchema", false},
	{ServingParallelism{}, "ParallelismSchema", false},
	{ServingCache{}, "CacheSchema", false},
	{ServingShape{}, "ShapeSchema", false},
	{ServingResources{}, "ResourcesSchema", false},
	{ServingReasoning{}, "ReasoningSchema", false},
	{ServingBehaviour{}, "ServingSchema", false},
	{ServingDiffusion{}, "DiffusionSchema", false},
	{ServingCapabilities{}, "CapabilitiesSchema", false},
	{ServingOverlay{}, "OverlaySchema", false},
	{ServingRole{}, "RoleSchema", false},
	{ServingTopology{}, "TopologyViewSchema", false},
	{ServingRouter{}, "RouterSchema", false},
	{ServingSpec{}, "WorkloadServingSchema", false},
	{ServingRequest{}, "WorkloadServingRequest", true},
	{WorkloadScale{}, "WorkloadScaleSchema", false},
	{WorkloadDeployment{}, "WorkloadDeploymentSchema", false},
	{Workload{}, "WorkloadSchema", false},
	{WorkloadCreateRequest{}, "WorkloadCreateRequest", true},
	{WorkloadUpdateRequest{}, "WorkloadUpdateRequest", true},
	{DeploymentReplicaRollup{}, "DeploymentReplicaRollupSchema", false},
	{Deployment{}, "DeploymentSchema", false},
	{DeploymentUpdateRequest{}, "DeploymentUpdateRequest", true},
	{DeploymentRoleObserved{}, "DeploymentRoleObservedSchema", false},
	{DeploymentStatus{}, "DeploymentStatusSchema", false},
	{DiscoveredWorkload{}, "DiscoveredWorkloadSchema", false},
	{DecisionRequest{}, "DecisionRequest", true},
	{PromoteRequest{}, "PromoteRequest", true},
	{EndpointTarget{}, "EndpointTargetSchema", false},
	{Endpoint{}, "EndpointSchema", false},
	{EndpointSurface{}, "EndpointSurfaceSchema", false},
	{EndpointPolicy{}, "endpoints_schemas_PolicySchema", false},
	{PolicyRule{}, "RuleSchema", false},
	{ProjectTenant{}, "ProjectTenantSchema", false},
	{Tenant{}, "TenantSchema", false},
	{Project{}, "ProjectSchema", false},
	{QuotaUsage{}, "QuotaUsageSchema", false},
	{Quota{}, "QuotaSchema", false},
	{ServingQuotaStandard{}, "ServingQuotaStandardSchema", false},
	{ServingQuota{}, "ServingQuotaSchema", false},
	{ResolvedLimits{}, "ResolvedLimitsSchema", false},
	{ResolvedServingQuota{}, "ResolvedServingQuotaSchema", false},
	{ResolvedServingQuotaTable{}, "ResolvedServingQuotaTableSchema", false},
	{ServingQuotaCreate{}, "ServingQuotaCreateRequest", true},
	{ServingQuotaUpdate{}, "ServingQuotaUpdateRequest", true},
	{SLAPercentiles{}, "PercentilesSchema", false},
	{SLAObjective{}, "SlaObjectiveSchema", false},
	{SLAReport{}, "SlaReportSchema", false},
	{PriorityClass{}, "PriorityClassSchema", false},
	{PolicyAttachment{}, "PolicyAttachmentSchema", false},
	{Policy{}, "policies_schemas_PolicySchema", false},
	{AllocationDevice{}, "AllocationDeviceSchema", false},
	{Allocation{}, "AllocationSchema", false},
	{OperationTarget{}, "OperationTargetSchema", false},
	{Operation{}, "OperationSchema", false},
	{OperationRequest{}, "OperationCreateRequest", true},
	{BenchmarkProvenance{}, "BenchmarkProvenanceSchema", false},
	{BenchmarkRun{}, "BenchmarkRunSchema", false},
	{BenchmarkCoverage{}, "BenchmarkCoverageSchema", false},
	{BenchmarkRequest{}, "BenchmarkRequestSchema", false},
	{BenchmarkCheck{}, "BenchmarkCheckSchema", false},
	{BenchmarkVerdict{}, "BenchmarkVerdictSchema", false},
	{BenchmarkBand{}, "BenchmarkBandSchema", false},
	{BenchmarkParameter{}, "BenchmarkParameterSchema", false},
	{BenchmarkHarness{}, "BenchmarkHarnessSchema", false},
	{BenchmarkRunCreate{}, "BenchmarkRunCreateRequest", true},
	{BenchmarkRequestCreate{}, "BenchmarkRequestCreateRequest", true},
	{ComponentStatus{}, "ComponentStatusSchema", false},
	{Component{}, "ComponentSchema", false},
	{AlertSubject{}, "AlertSubjectSchema", false},
	{MetricRule{}, "MetricRuleSchema", false},
	{AlertThreshold{}, "AlertThresholdSchema", false},
	{AlertThresholdCreate{}, "AlertThresholdCreateRequest", true},
	{AlertThresholdUpdate{}, "AlertThresholdUpdateRequest", true},
	{EventSubject{}, "EventSubjectSchema", false},
	{Event{}, "EventSchema", false},
	{LoginRequest{}, "LoginRequest", true},
	{LoginResult{}, "LoginResponse", false},
}

func TestWireTypesMatchThePlaneSchemas(t *testing.T) {
	var schemas map[string]map[string]string
	if err := json.Unmarshal(readFixture(t, "plane-schemas.json"), &schemas); err != nil {
		t.Fatal(err)
	}
	for _, w := range wireTypes {
		typ := reflect.TypeOf(w.value)
		props, ok := schemas[w.schema]
		if !ok {
			t.Errorf("%s: the plane has no schema %s", typ.Name(), w.schema)
			continue
		}
		have := jsonFields(typ)
		var invented, missing []string
		for k := range have {
			if _, ok := props[k]; !ok {
				invented = append(invented, k)
			}
		}
		if !w.request {
			for k := range props {
				if _, ok := have[k]; !ok {
					missing = append(missing, k)
				}
			}
		}
		sort.Strings(invented)
		sort.Strings(missing)
		if len(invented) > 0 {
			t.Errorf("%s has fields %s does not: %s", typ.Name(), w.schema, strings.Join(invented, ", "))
		}
		if len(missing) > 0 {
			t.Errorf("%s lacks fields of %s: %s", typ.Name(), w.schema, strings.Join(missing, ", "))
		}
	}
}

// TestEveryWireTypeIsChecked keeps the table above complete: a struct field
// typed as another wire struct has to be in it too.
func TestEveryWireTypeIsChecked(t *testing.T) {
	listed := map[reflect.Type]bool{}
	for _, w := range wireTypes {
		listed[reflect.TypeOf(w.value)] = true
	}
	for _, w := range wireTypes {
		typ := reflect.TypeOf(w.value)
		for name, ft := range jsonFields(typ) {
			for ft.Kind() == reflect.Pointer || ft.Kind() == reflect.Slice || ft.Kind() == reflect.Map {
				ft = ft.Elem()
			}
			if ft.Kind() != reflect.Struct || ft == reflect.TypeOf(time.Time{}) {
				continue
			}
			if !listed[ft] {
				t.Errorf("%s.%s is a %s, which is not checked against a plane schema", typ.Name(), name, ft.Name())
			}
		}
	}
}

// jsonFields is the wire name and Go type of every field encoding/json writes.
func jsonFields(typ reflect.Type) map[string]reflect.Type {
	out := map[string]reflect.Type{}
	for i := 0; i < typ.NumField(); i++ {
		f := typ.Field(i)
		if !f.IsExported() {
			continue
		}
		tag := f.Tag.Get("json")
		if tag == "-" {
			continue
		}
		name, _, _ := strings.Cut(tag, ",")
		if f.Anonymous && name == "" {
			for k, v := range jsonFields(f.Type) {
				out[k] = v
			}
			continue
		}
		if name == "" {
			name = f.Name
		}
		out[name] = f.Type
	}
	return out
}
