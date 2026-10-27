package reconcile

import (
	"context"
	"fmt"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// componentGVK is the object Dynamo derives from a graph deployment, one per component.
//
// This operator neither renders nor owns it, and reads it for one reason: it is where the Dynamo
// operator writes down that it could not do what the graph asked. The graph's own Ready condition
// names the child and stops there, so a diagnosis that reads only the graph reports "some
// resources are not ready" (the fact you already had) instead of what went wrong.
var componentGVK = schema.GroupVersionKind{
	Group:   render.GVK.Group,
	Version: render.GVK.Version,
	Kind:    "DynamoComponentDeploymentList",
}

// stallAfter is how long a deployment may hold at zero ready replicas before the silence is
// reported as a failure in its own right.
//
// The window is generous by design. Everything with a name of its own (a rejected spec, an image
// that will not pull, a pod nothing can schedule) is reported the moment it is seen, and this
// catches only what none of those explain: a graph applied cleanly, no pod, no complaint from
// anybody. Weights are already resident before the apply, so what remains is loading a model off
// local disk into device memory, which is minutes, not tens of them.
const stallAfter = 15 * time.Minute

// trouble is a diagnosis: the closed-set reason for it, and the message a person reads.
//
// Separate from `rollout` because it comes from somewhere else. A rollout is counted off the pods;
// this is read off the objects between the apply and those pods. A deployment with no pods and no
// trouble is starting up; a deployment with no pods and trouble is broken, and without this the
// two would report the same `placed · 0 of 1 replicas ready`.
type trouble struct {
	reason  string
	code    string
	message string

	// fatal marks a diagnosis that will not resolve on its own. An unschedulable pod clears
	// when a node joins; a spec the API server refused is refused identically forever.
	fatal bool
}

func (t *trouble) lastError(at string) *wire.LastError {
	if t == nil {
		return nil
	}
	return &wire.LastError{Code: t.code, Message: t.message, At: at}
}

// diagnose reads why a deployment has fewer ready replicas than it asked for.
//
// Called only on the shortfall, which is also the only time anybody wants the answer. The order is
// the order of certainty. A *refusal* by the cluster's own controllers comes first because nothing
// downstream of it will ever change: no pod is coming, so pod evidence is stale or absent. Pod
// evidence comes next. The stall is last, because it is the diagnosis for having found nothing.
//
// A component that is merely not ready is read after the pods and not before them, which is the
// opposite of a refusal and for the same reason: it is a summary of the pods instead of a fact
// about them, so the pod that is pulling an image, unschedulable, or crash-looping is the better
// answer whenever there is one. Taken first, "Deployment is not ready" hid "pulling image" for the
// whole of a nine-minute image pull.
func (r *SyncedResourceReconciler) diagnose(
	ctx context.Context,
	name string,
	pods []corev1.Pod,
	out rollout,
) *trouble {
	if out.desired == 0 || out.ready >= out.desired {
		return nil
	}
	summary := r.rejectedBy(ctx, name)
	if summary != nil && summary.fatal {
		return summary
	}
	if found := troubleFromPods(pods); found != nil {
		return found
	}
	if summary != nil {
		return summary
	}
	return r.stalled(ctx, name, pods, out)
}

// rejectedBy reads the conditions of the objects Dynamo derived from the graph, and of the graph
// itself, for one that is false because something refused it.
//
// The children are read before the parent. A parent's condition summarises ("some resources are
// not ready", naming the child), and the child holds the sentence that says why, which for the
// workload this was written for was `must be no more than 63 bytes`. Reporting the summary would
// have left the same investigation to do, one level further down.
func (r *SyncedResourceReconciler) rejectedBy(ctx context.Context, name string) *trouble {
	var children unstructured.UnstructuredList
	children.SetGroupVersionKind(componentGVK)
	err := r.Client.List(ctx, &children,
		client.InNamespace(r.Render.Namespace),
		client.MatchingLabels{graphDeploymentLabel: name},
	)
	if err != nil {
		// Not a reconcile failure. A cluster whose Dynamo version does not serve this kind is
		// one this operator still reconciles; it only diagnoses it less well.
		r.Log.Debug("could not read the rendered components", "graph", name, "error", err)
	}
	for i := range children.Items {
		if found := troubleFromConditions(&children.Items[i]); found != nil {
			return found
		}
	}

	var graph unstructured.Unstructured
	graph.SetGroupVersionKind(render.GVK)
	key := client.ObjectKey{Namespace: r.Render.Namespace, Name: name}
	if err := r.Client.Get(ctx, key, &graph); err != nil {
		r.Log.Debug("could not read the rendered graph", "graph", name, "error", err)
		return nil
	}
	return troubleFromConditions(&graph)
}

// progressReasons are the condition reasons that mean the object is still working, as opposed to
// having given up. A false condition carrying one of them is not a diagnosis.
//
// Everything else is treated as trouble, by design. A closed list of failures would have to
// enumerate what a controller this operator does not own might one day write, and being silent
// about an unrecognised failure is the exact behaviour being fixed here.
var progressReasons = map[string]bool{
	"Reconciling":                  true,
	"Progressing":                  true,
	"Pending":                      true,
	"Creating":                     true,
	"some_resources_are_not_ready": true,
}

// troubleFromConditions reads one object's status conditions for a failure worth reporting.
//
// A `Reconciling` reason with a message that names an API server rejection is still a rejection:
// the Dynamo operator reports its permanent failures under the reason it retries them with, so
// keying on the reason alone reports nothing. The message is what carries the fact, so a false
// condition whose reason means progress is still read when its message says a write was refused.
func troubleFromConditions(obj *unstructured.Unstructured) *trouble {
	conditions, found, err := unstructured.NestedSlice(obj.Object, "status", "conditions")
	if err != nil || !found {
		return nil
	}
	for _, raw := range conditions {
		cond, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		status, _ := cond["status"].(string)
		if status != "False" {
			continue
		}
		reason, _ := cond["reason"].(string)
		message, _ := cond["message"].(string)
		if message == "" {
			continue
		}
		if progressReasons[reason] && !refusal(message) {
			continue
		}
		if refusal(message) {
			return &trouble{
				reason:  "ClusterRejected",
				code:    "ClusterRejected",
				message: fmt.Sprintf("the cluster refused %s: %s", obj.GetName(), message),
				fatal:   true,
			}
		}
		// Reported, because an unrecognised failure must not be silent, but not as a
		// refusal, because it is not one and saying so is worse than saying nothing. A
		// component that is false with `Deployment is not ready` is what every rollout looks
		// like while its pods pull an image, and calling that "the cluster refused" put a red
		// line on the console reading `deployment.placed · the cluster refused ... is not
		// ready`: a phase that means it worked, a colour that means it broke, and a sentence
		// that means neither. Nothing here says whether to wait or to act.
		return &trouble{
			reason:  "ComponentNotReady",
			code:    "ComponentNotReady",
			message: fmt.Sprintf("%s is not ready yet and reports: %s", obj.GetName(), message),
		}
	}
	return nil
}

// refusal is whether a controller's message is the API server having rejected a write.
//
// A rejection is permanent in a way a retry is not: the same spec is refused identically for ever,
// so nothing about waiting longer changes the outcome. This is a heuristic over free text, which is
// why it only ever escalates a diagnosis that is already being reported and never suppresses one.
func refusal(message string) bool {
	lower := strings.ToLower(message)
	return strings.Contains(lower, "is invalid") ||
		strings.Contains(lower, "is forbidden") ||
		strings.Contains(lower, "already exists")
}

// troubleFromPods reads a pod that exists and is not going to serve.
//
// Waiting reasons, not pod phase: a pod stuck on `ImagePullBackOff` is `Pending`, which is
// the same phase as one that is pulling normally, and the difference between those two is the
// entire question. The first pod with something to say wins: a deployment whose every replica is
// crash-looping has one fault, not four.
func troubleFromPods(pods []corev1.Pod) *trouble {
	for i := range pods {
		pod := &pods[i]
		if !pod.DeletionTimestamp.IsZero() {
			continue
		}
		if found := unschedulable(pod); found != nil {
			return found
		}
		if found := containerTrouble(pod); found != nil {
			return found
		}
	}
	return nil
}

// unschedulable is a pod the scheduler has looked at and cannot place.
//
// Not fatal: the message names what was missing, and a node joining or a workload leaving makes it
// placeable with nothing about the deployment having changed. The stall guard is what turns a
// shortfall nobody ever resolves into a failure.
func unschedulable(pod *corev1.Pod) *trouble {
	for _, cond := range pod.Status.Conditions {
		if cond.Type != corev1.PodScheduled || cond.Status != corev1.ConditionFalse {
			continue
		}
		if cond.Reason != corev1.PodReasonUnschedulable {
			continue
		}
		return &trouble{
			reason:  "PodUnschedulable",
			code:    "PodUnschedulable",
			message: fmt.Sprintf("%s cannot be scheduled: %s", pod.Name, cond.Message),
		}
	}
	return nil
}

// waitingReasons maps the container states worth reporting onto the closed set, and is also what
// decides which of them are worth reporting at all: `ContainerCreating` and `PodInitializing` are
// absent because they are a pod coming up normally.
var waitingReasons = map[string]string{
	"ImagePullBackOff":           "ImagePullFailed",
	"ErrImagePull":               "ImagePullFailed",
	"InvalidImageName":           "ImagePullFailed",
	"CrashLoopBackOff":           "CrashLooping",
	"CreateContainerConfigError": "ContainerConfigInvalid",
	"CreateContainerError":       "ContainerConfigInvalid",
}

// maxLastWords bounds how much of a dead container's output travels.
//
// The message reaches an event row and a status field, and a Python traceback is longer than
// either wants. The tail is the part that matters (argparse writes its usage first and its
// complaint last), so this trims the front and keeps the end.
const maxLastWords = 800

// lastWords is what the container printed before it exited, trimmed to something a timeline can
// hold. Empty when Kubernetes kept none, which is every pod rendered before the termination
// message policy was set, and every container that was killed instead of exiting on its own.
func lastWords(status corev1.ContainerStatus) string {
	terminated := status.LastTerminationState.Terminated
	if terminated == nil {
		terminated = status.State.Terminated
	}
	if terminated == nil {
		return ""
	}
	text := strings.TrimSpace(terminated.Message)
	if text == "" {
		return ""
	}
	if len(text) > maxLastWords {
		text = "…" + text[len(text)-maxLastWords:]
	}
	return text
}

// lastExit describes how a container last terminated: the kubelet's reason and the exit code,
// and what an OOM kill means for the person reading it. Empty when it has not terminated.
func lastExit(status corev1.ContainerStatus) string {
	terminated := status.LastTerminationState.Terminated
	if terminated == nil {
		terminated = status.State.Terminated
	}
	if terminated == nil {
		return ""
	}
	reason := terminated.Reason
	if reason == "" {
		reason = "exited"
	}
	out := fmt.Sprintf("last exit: %s, code %d", reason, terminated.ExitCode)
	if terminated.Reason == "OOMKilled" {
		out += " (killed for exceeding its memory limit)"
	}
	return out
}

func containerTrouble(pod *corev1.Pod) *trouble {
	statuses := append(append([]corev1.ContainerStatus{}, pod.Status.InitContainerStatuses...),
		pod.Status.ContainerStatuses...)
	for _, status := range statuses {
		waiting := status.State.Waiting
		if waiting == nil {
			continue
		}
		reason, known := waitingReasons[waiting.Reason]
		if !known {
			continue
		}
		message := waiting.Message
		if message == "" {
			message = waiting.Reason
		}
		// What the container itself said beats the kubelet's account of it. `CrashLoopBackOff`
		// carries "back-off 40s restarting failed container", which describes the loop and not
		// the fault: every crash-looping container in the world reports the same sentence. The
		// last termination carries the process's own dying output, which for the workload this
		// was written for was `unrecognized arguments: --tool-call-parser glm47`; the whole
		// answer, in one line, on a console instead of in `kubectl logs --previous`.
		//
		// Present only because the renderer sets `terminationMessagePolicy:
		// FallbackToLogsOnError`; a pod from an older render, or one whose container was
		// killed instead of exiting, still has none, and then the kubelet's account is what
		// there is.
		if last := lastWords(status); last != "" {
			message = fmt.Sprintf("%s (%s)", last, message)
		}
		// How it ended, which the output cannot say when the kernel killed it: an OOM kill
		// leaves no last words, and the exit code is the only trace of it.
		if exit := lastExit(status); exit != "" {
			message += "; " + exit
		}
		return &trouble{
			reason:  reason,
			code:    reason,
			message: fmt.Sprintf("%s/%s: %s", pod.Name, status.Name, message),
			fatal:   reason == "ImagePullFailed",
		}
	}
	return nil
}

// stalled is the diagnosis for a deployment that has been applied, has produced no ready replica,
// and has nothing anywhere saying why.
//
// It reports the silence itself: a graph applied cleanly, no complaint from anybody, and nothing
// running. Measured from the rendered graph's creation, because the apply is the moment the
// cluster was asked to run something, or from the oldest pod still not running when that is
// later, because a pod recreated an hour in has only been stuck since it was created.
//
// A pod that is Running and not Ready is not silent: its container is up and loading weights, and
// for a model of hundreds of gigabytes read off network storage that takes longer than the window.
// A process that hangs there is the readiness and startup probes' to kill, and a restart is then
// reported as a crash loop. So Running pods are left out, and the stall is about the rest: one
// replica loading, or serving, must not hide another that never left Pending.
//
// With a replica already serving, the deployment is degraded, not failed, so a stuck pod
// there is reported without failing it: under the `Progressing` reason its phase already implies,
// with the pod named in the message.
func (r *SyncedResourceReconciler) stalled(ctx context.Context, name string, pods []corev1.Pod, out rollout) *trouble {
	var stuck *corev1.Pod
	running := 0
	for i := range pods {
		pod := &pods[i]
		if !pod.DeletionTimestamp.IsZero() {
			continue
		}
		if pod.Status.Phase == corev1.PodRunning {
			running++
			continue
		}
		if stuck == nil || pod.CreationTimestamp.Before(&stuck.CreationTimestamp) {
			stuck = pod
		}
	}
	if stuck == nil && (running > 0 || out.ready > 0) {
		return nil
	}
	if out.ready > 0 {
		age := time.Since(stuck.CreationTimestamp.Time)
		if age < stallAfter {
			return nil
		}
		return &trouble{
			reason: "Progressing",
			code:   "Progressing",
			message: fmt.Sprintf("%s has been %s for %s with nothing reporting why",
				stuck.Name, phaseOf(stuck), age.Round(time.Minute)),
		}
	}

	var graph unstructured.Unstructured
	graph.SetGroupVersionKind(render.GVK)
	key := client.ObjectKey{Namespace: r.Render.Namespace, Name: name}
	if err := r.Client.Get(ctx, key, &graph); err != nil {
		return nil
	}
	since := graph.GetCreationTimestamp()
	if since.IsZero() {
		return nil
	}
	if stuck != nil && since.Before(&stuck.CreationTimestamp) {
		since = stuck.CreationTimestamp
	}
	age := time.Since(since.Time)
	if age < stallAfter {
		return nil
	}
	detail := podCount(len(out.replicas))
	if stuck != nil {
		detail = fmt.Sprintf("%s has been %s for %s", stuck.Name, phaseOf(stuck), age.Round(time.Minute))
	}
	return &trouble{
		reason: "NoReplicasStarted",
		code:   "NoReplicasStarted",
		message: fmt.Sprintf(
			"applied %s ago and %d of %d replicas are ready, with nothing reporting why; %s",
			time.Since(graph.GetCreationTimestamp().Time).Round(time.Minute), out.ready, out.desired, detail),
		fatal: true,
	}
}

func phaseOf(pod *corev1.Pod) string {
	if pod.Status.Phase == "" {
		return "Pending"
	}
	return string(pod.Status.Phase)
}

func podCount(n int) string {
	if n == 0 {
		return "no pod was ever created"
	}
	return fmt.Sprintf("%d pods exist and none is ready", n)
}
