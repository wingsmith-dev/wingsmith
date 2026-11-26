# 0020: Deployments render under a workload-scoped object name

**Status:** accepted · 2026-11-26

## Context

A deployment took its workload's name and was unique on `(cluster_id, name)`, because that name is
the object the operator renders into the cluster. Workload names are unique per project, so two
projects (or tenants) with a `qwen` could not share a cluster, and placement's refusal told one
that the other existed. A namespaced declarative API cannot live with that.

## Decision

Split the two jobs. `Deployment.name` stays the workload's name, for people and references, and is
no longer unique. `Deployment.object_name` is what the deployment renders as, unique per cluster:
the workload name reduced to an RFC 1123 label and cut to 23 characters, a dash, and the last eight
characters of the workload uid (random ULID bits). At most 32 characters, which is the budget the
renderer already gives a workload name, so the operator and the wire are unchanged: the plane sends
`object_name` in `DeploymentDesired.name`.

Derived from the workload uid, not the project, so a rename of a project or a move between tenants
cannot change it, and all deployments of one workload share it across clusters.

Existing rows keep their name as their object name, and adopted deployments use the name of the
object already running: renaming either would make the operator render a second object.

## Consequences

`kubectl get` shows `qwen-7k2m9x4p`, not `qwen`, for new deployments. The placement
name-taken check now reads `object_name` and only trips against a deployment from before this.
The name is a cluster object name, not a Dynamo concept; a runtime without Kubernetes objects can
use it as its process or unit name.
