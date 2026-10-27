# HTTP API reference

Generated from the plane by `scripts/openapi.sh`; do not edit by hand. Every route the plane
serves, grouped by collection. Parameters, request bodies and response schemas are in
[`openapi.json`](openapi.json). What the routes mean, who may call them and the rules every
response follows are in [`API.md`](API.md).

## `/health`

| Method | Path | What |
|---|---|---|
| GET | `/health` | Health |

## `/v1/alert-rules`

| Method | Path | What |
|---|---|---|
| GET | `/v1/alert-rules` | ListRules |

## `/v1/alert-thresholds`

| Method | Path | What |
|---|---|---|
| GET | `/v1/alert-thresholds` | ListThresholds |
| POST | `/v1/alert-thresholds` | CreateThreshold |
| GET | `/v1/alert-thresholds/{ref}` | GetThreshold |
| PATCH | `/v1/alert-thresholds/{ref}` | UpdateThreshold |
| DELETE | `/v1/alert-thresholds/{ref}` | DeleteThreshold |

## `/v1/alerts`

| Method | Path | What |
|---|---|---|
| GET | `/v1/alerts` | ListAlerts |
| GET | `/v1/alerts/{ref}` | GetAlert |
| POST | `/v1/alerts/{ref}/acknowledge` | AcknowledgeAlert |
| POST | `/v1/alerts/{ref}/resolve` | ResolveAlert |

## `/v1/allocations`

| Method | Path | What |
|---|---|---|
| GET | `/v1/allocations` | ListAllocations |
| GET | `/v1/allocations/{ref}` | GetAllocation |

## `/v1/artifacts`

| Method | Path | What |
|---|---|---|
| GET | `/v1/artifacts` | ListArtifacts |
| POST | `/v1/artifacts` | CreateArtifact |
| GET | `/v1/artifacts/resolve` | ResolveArtifact |
| GET | `/v1/artifacts/usage` | ListArtifactUsage |
| GET | `/v1/artifacts/{ref}` | GetArtifact |
| PATCH | `/v1/artifacts/{ref}` | UpdateArtifact |
| DELETE | `/v1/artifacts/{ref}` | DeleteArtifact |
| GET | `/v1/artifacts/{ref}/baselines` | ListArtifactBaselines |
| POST | `/v1/artifacts/{ref}/baselines` | CreateArtifactBaseline |
| PATCH | `/v1/artifacts/{ref}/baselines/{baseline}` | UpdateArtifactBaseline |
| DELETE | `/v1/artifacts/{ref}/baselines/{baseline}` | DeleteArtifactBaseline |
| GET | `/v1/artifacts/{ref}/fitment` | ArtifactFitment |
| POST | `/v1/artifacts/{ref}/refresh` | RefreshArtifact |
| GET | `/v1/artifacts/{ref}/usage` | ArtifactUsage |

## `/v1/auth`

| Method | Path | What |
|---|---|---|
| POST | `/v1/auth/login` | Login |
| POST | `/v1/auth/logout` | Logout |
| POST | `/v1/auth/refresh` | Refresh |

## `/v1/benchmarks`

| Method | Path | What |
|---|---|---|
| GET | `/v1/benchmarks` | ListRuns |
| POST | `/v1/benchmarks` | CreateRun |
| GET | `/v1/benchmarks/harnesses` | ListHarnesses |
| POST | `/v1/benchmarks/requests` | CreateRequest |
| GET | `/v1/benchmarks/requests/{ref}` | GetRequest |
| GET | `/v1/benchmarks/requests/{ref}/bands` | GetRequestBands |
| GET | `/v1/benchmarks/requests/{ref}/curve` | GetRequestCurve |
| GET | `/v1/benchmarks/requests/{ref}/verdict` | GetRequestVerdict |
| GET | `/v1/benchmarks/serving/{digest}` | GetServingSummary |
| GET | `/v1/benchmarks/{ref}` | GetRun |
| GET | `/v1/benchmarks/{ref}/verdict` | GetVerdict |

## `/v1/clusters`

| Method | Path | What |
|---|---|---|
| GET | `/v1/clusters` | ListClusters |
| POST | `/v1/clusters` | CreateCluster |
| GET | `/v1/clusters/rollups` | ListRollups |
| POST | `/v1/clusters/tunnel-auth` | VerifyTunnelToken |
| GET | `/v1/clusters/{ref}` | GetCluster |
| PATCH | `/v1/clusters/{ref}` | UpdateCluster |
| DELETE | `/v1/clusters/{ref}` | DeleteCluster |
| POST | `/v1/clusters/{ref}/enrolment-token` | IssueEnrolmentToken |
| GET | `/v1/clusters/{ref}/headroom` | GetHeadroom |
| GET | `/v1/clusters/{ref}/rollup` | GetRollup |
| PUT | `/v1/clusters/{ref}/serving` | SetServing |
| DELETE | `/v1/clusters/{ref}/serving` | ClearServing |
| GET | `/v1/clusters/{ref}/setup` | ClusterSetup |

## `/v1/components`

| Method | Path | What |
|---|---|---|
| GET | `/v1/components` | ListComponents |
| POST | `/v1/components` | CreateComponent |
| GET | `/v1/components/{ref}` | GetComponent |
| PATCH | `/v1/components/{ref}` | UpdateComponent |
| DELETE | `/v1/components/{ref}` | DeleteComponent |

## `/v1/deployments`

| Method | Path | What |
|---|---|---|
| GET | `/v1/deployments` | ListDeployments |
| GET | `/v1/deployments/{ref}` | GetDeployment |
| PATCH | `/v1/deployments/{ref}` | UpdateDeployment |
| POST | `/v1/deployments/{ref}/authority` | SetDeploymentAuthority |

## `/v1/device-classes`

| Method | Path | What |
|---|---|---|
| GET | `/v1/device-classes` | ListDeviceClasses |
| GET | `/v1/device-classes/{name}` | GetDeviceClass |

## `/v1/devices`

| Method | Path | What |
|---|---|---|
| GET | `/v1/devices` | ListDevices |
| GET | `/v1/devices/{ref}` | GetDevice |

## `/v1/discovered`

| Method | Path | What |
|---|---|---|
| GET | `/v1/discovered` | ListDiscovered |
| GET | `/v1/discovered/{ref}` | GetDiscovered |
| DELETE | `/v1/discovered/{ref}` | DismissDiscovered |
| POST | `/v1/discovered/{ref}/ignore` | IgnoreDiscovered |
| POST | `/v1/discovered/{ref}/promote` | PromoteDiscovered |
| POST | `/v1/discovered/{ref}/restore` | RestoreDiscovered |
| GET | `/v1/discovered/{ref}/serving-draft` | DraftServing |
| POST | `/v1/discovered/{ref}/track` | TrackDiscovered |

## `/v1/endpoints`

| Method | Path | What |
|---|---|---|
| GET | `/v1/endpoints` | ListEndpoints |
| POST | `/v1/endpoints` | CreateEndpoint |
| GET | `/v1/endpoints/resolved` | ResolveAll |
| GET | `/v1/endpoints/{ref}` | GetEndpoint |
| PATCH | `/v1/endpoints/{ref}` | UpdateEndpoint |
| DELETE | `/v1/endpoints/{ref}` | DeleteEndpoint |
| GET | `/v1/endpoints/{ref}/resolved` | ResolveOne |

## `/v1/events`

| Method | Path | What |
|---|---|---|
| GET | `/v1/events` | ListEvents |
| GET | `/v1/events/{ref}` | GetEvent |

## `/v1/fleet`

| Method | Path | What |
|---|---|---|
| GET | `/v1/fleet/summary` | FleetSummary |

## `/v1/gateway`

| Method | Path | What |
|---|---|---|
| GET | `/v1/gateway/tenants` | ResolveTenants |

## `/v1/metrics`

| Method | Path | What |
|---|---|---|
| GET | `/v1/metrics/clusters/{ref}` | ClusterMetrics |
| GET | `/v1/metrics/clusters/{ref}/device-classes/{name}` | DeviceClassMetrics |
| GET | `/v1/metrics/deployments/{ref}` | DeploymentMetrics |
| GET | `/v1/metrics/devices/{ref}` | DeviceMetrics |
| GET | `/v1/metrics/endpoints/{ref}` | EndpointMetrics |
| GET | `/v1/metrics/endpoints/{ref}/sla` | EndpointSla |
| GET | `/v1/metrics/fleet` | FleetMetrics |
| GET | `/v1/metrics/nodes/{ref}` | NodeMetrics |
| GET | `/v1/metrics/pools/{ref}` | PoolMetrics |
| GET | `/v1/metrics/serving` | ServingMetrics |
| GET | `/v1/metrics/sources` | ListSources |
| GET | `/v1/metrics/tenants/{ref}` | TenantMetrics |
| GET | `/v1/metrics/workloads/{ref}` | WorkloadMetrics |

## `/v1/nodes`

| Method | Path | What |
|---|---|---|
| GET | `/v1/nodes` | ListNodes |
| GET | `/v1/nodes/summary` | NodesSummary |
| GET | `/v1/nodes/{ref}` | GetNode |

## `/v1/notification-channels`

| Method | Path | What |
|---|---|---|
| GET | `/v1/notification-channels` | ListChannels |
| POST | `/v1/notification-channels` | CreateChannel |
| GET | `/v1/notification-channels/{ref}` | GetChannel |
| PATCH | `/v1/notification-channels/{ref}` | UpdateChannel |
| DELETE | `/v1/notification-channels/{ref}` | DeleteChannel |
| POST | `/v1/notification-channels/{ref}/test` | TestChannel |

## `/v1/operations`

| Method | Path | What |
|---|---|---|
| GET | `/v1/operations` | ListOperations |
| POST | `/v1/operations` | CreateOperation |
| GET | `/v1/operations/summary` | OperationsSummary |
| GET | `/v1/operations/{ref}` | GetOperation |
| DELETE | `/v1/operations/{ref}` | CancelOperation |

## `/v1/placement`

| Method | Path | What |
|---|---|---|
| POST | `/v1/placement/expand` | PreviewExpansion |
| POST | `/v1/placement/preview` | PreviewPlacement |
| GET | `/v1/placement/reservations` | ListReservations |

## `/v1/policies`

| Method | Path | What |
|---|---|---|
| GET | `/v1/policies` | ListPolicies |
| POST | `/v1/policies` | CreatePolicy |
| GET | `/v1/policies/{ref}` | GetPolicy |
| PATCH | `/v1/policies/{ref}` | UpdatePolicy |
| DELETE | `/v1/policies/{ref}` | DeletePolicy |

## `/v1/pools`

| Method | Path | What |
|---|---|---|
| GET | `/v1/pools` | ListPools |
| POST | `/v1/pools` | CreatePool |
| GET | `/v1/pools/{ref}` | GetPool |
| PATCH | `/v1/pools/{ref}` | UpdatePool |
| DELETE | `/v1/pools/{ref}` | DeletePool |

## `/v1/priority-classes`

| Method | Path | What |
|---|---|---|
| GET | `/v1/priority-classes` | ListPriorityClasses |
| POST | `/v1/priority-classes` | CreatePriorityClass |
| GET | `/v1/priority-classes/{ref}` | GetPriorityClass |
| PATCH | `/v1/priority-classes/{ref}` | UpdatePriorityClass |
| DELETE | `/v1/priority-classes/{ref}` | DeletePriorityClass |

## `/v1/projects`

| Method | Path | What |
|---|---|---|
| GET | `/v1/projects` | ListProjects |
| POST | `/v1/projects` | CreateProject |
| GET | `/v1/projects/{ref}` | GetProject |
| PATCH | `/v1/projects/{ref}` | UpdateProject |
| DELETE | `/v1/projects/{ref}` | DeleteProject |

## `/v1/quotas`

| Method | Path | What |
|---|---|---|
| GET | `/v1/quotas` | ListQuotas |
| POST | `/v1/quotas` | CreateQuota |
| GET | `/v1/quotas/{ref}` | GetQuota |
| PATCH | `/v1/quotas/{ref}` | UpdateQuota |
| DELETE | `/v1/quotas/{ref}` | DeleteQuota |

## `/v1/serving`

| Method | Path | What |
|---|---|---|
| GET | `/v1/serving` | Overview |
| GET | `/v1/serving/models/{ref}` | Model |
| GET | `/v1/serving/models/{ref}/metrics` | ModelMetrics |
| GET | `/v1/serving/models/{ref}/sla` | ModelSla |

## `/v1/serving-quotas`

| Method | Path | What |
|---|---|---|
| GET | `/v1/serving-quotas` | ListServingQuotas |
| POST | `/v1/serving-quotas` | CreateServingQuota |
| GET | `/v1/serving-quotas/resolved` | ResolveServingQuotas |
| GET | `/v1/serving-quotas/{ref}` | GetServingQuota |
| PATCH | `/v1/serving-quotas/{ref}` | UpdateServingQuota |
| DELETE | `/v1/serving-quotas/{ref}` | DeleteServingQuota |

## `/v1/system`

| Method | Path | What |
|---|---|---|
| GET | `/v1/system/config` | Config |

## `/v1/tenants`

| Method | Path | What |
|---|---|---|
| GET | `/v1/tenants` | ListTenants |
| POST | `/v1/tenants` | CreateTenant |
| GET | `/v1/tenants/{ref}` | GetTenant |
| PATCH | `/v1/tenants/{ref}` | UpdateTenant |
| DELETE | `/v1/tenants/{ref}` | DeleteTenant |
| GET | `/v1/tenants/{ref}/keys` | ListKeys |
| POST | `/v1/tenants/{ref}/keys` | MintKey |
| DELETE | `/v1/tenants/{ref}/keys/{key_ref}` | RevokeKey |
| GET | `/v1/tenants/{ref}/tokens` | ListTokens |
| POST | `/v1/tenants/{ref}/tokens` | MintToken |
| DELETE | `/v1/tenants/{ref}/tokens/{token_ref}` | RevokeToken |
| GET | `/v1/tenants/{tenant}/serving` | Overview |
| GET | `/v1/tenants/{tenant}/serving/models/{ref}` | Model |
| GET | `/v1/tenants/{tenant}/serving/models/{ref}/metrics` | ModelMetrics |
| GET | `/v1/tenants/{tenant}/serving/models/{ref}/sla` | ModelSla |

## `/v1/topology-domains`

| Method | Path | What |
|---|---|---|
| GET | `/v1/topology-domains` | ListTopologyDomains |
| GET | `/v1/topology-domains/{ref}` | GetTopologyDomain |

## `/v1/workloads`

| Method | Path | What |
|---|---|---|
| GET | `/v1/workloads` | ListWorkloads |
| POST | `/v1/workloads` | CreateWorkload |
| GET | `/v1/workloads/{ref}` | GetWorkload |
| PATCH | `/v1/workloads/{ref}` | UpdateWorkload |
| DELETE | `/v1/workloads/{ref}` | DeleteWorkload |
| GET | `/v1/workloads/{ref}/placement` | WorkloadPlacement |
| GET | `/v1/workloads/{ref}/revisions` | WorkloadRevisions |
| GET | `/v1/workloads/{ref}/revisions/{revision}` | WorkloadRevision |
| PATCH | `/v1/workloads/{ref}/revisions/{revision}` | AnnotateRevision |
