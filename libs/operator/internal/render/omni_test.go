package render

import (
	"encoding/json"
	"slices"
	"strings"
	"testing"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

func omniInputs() Inputs {
	in := testInputs()
	in.Deployment.Name = "z-image-turbo"
	in.Deployment.ServedModelName = "tongyi-mai/z-image-turbo"
	in.Deployment.Placement.DevicesPerReplica = 1
	in.Deployment.Serving.Engine = "omni"
	in.Deployment.Serving.OutputModality = OutputImage
	in.Deployment.Serving.Parallelism = wire.Parallelism{Tensor: 1, Pipeline: 1}
	return in
}

func TestAnUnknownEngineIsARenderErrorNotAChatServer(t *testing.T) {
	in := testInputs()
	in.Deployment.Serving.Engine = "diffusers"
	if _, err := DynamoGraphDeployment(in, testOptions()); err == nil || !strings.Contains(err.Error(), "unknown engine") {
		t.Fatalf("err = %v, want an unknown engine refusal", err)
	}
}

func TestEngineModules(t *testing.T) {
	for engine, want := range map[string]string{
		"vllm": "vllm", "VLLM": "vllm", "sglang": "sglang", "trtllm": "trtllm",
		"tensorrt-llm": "trtllm", "omni": "vllm.omni", "vllm-omni": "vllm.omni",
	} {
		got, err := engineModule(engine)
		if err != nil || got != want {
			t.Errorf("engineModule(%q) = %q, %v; want %q", engine, got, err, want)
		}
	}
	if _, err := engineModule(""); err == nil {
		t.Error("an empty engine rendered; it must be refused like any other unknown one")
	}
}

func TestAnOmniDeploymentRendersOneGenericWorker(t *testing.T) {
	obj, err := DynamoGraphDeployment(omniInputs(), testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	c := component(t, obj)
	if c["name"] != OmniComponent || c["type"] != "worker" {
		t.Fatalf("component = %v/%v, want %s/worker", c["name"], c["type"], OmniComponent)
	}
	pod := renderedPod(t, obj)
	main := pod.Containers[0]
	if got := strings.Join(main.Command, " "); got != "python3 -m dynamo.vllm.omni" {
		t.Fatalf("command = %q", got)
	}
	for flag, want := range map[string]string{
		"--output-modalities":   "image",
		"--media-output-fs-url": omniMediaURL,
		"--served-model-name":   "tongyi-mai/z-image-turbo",
	} {
		if got := argValue(main.Args, flag); got != want {
			t.Errorf("%s = %q, want %q", flag, got, want)
		}
	}
	// The omni CLI exits on any of these; they are the chat worker's.
	for _, flag := range []string{"--max-model-len", "--no-enable-prefix-caching", "--kv-events-config",
		"--tensor-parallel-size", "--stage-configs-path", "--revision"} {
		if slices.Contains(main.Args, flag) {
			t.Errorf("omni args carry %s: %v", flag, main.Args)
		}
	}
	for _, init := range pod.InitContainers {
		if init.Name == "omni-stage-config" {
			t.Fatalf("a stage-config writer was rendered with no diffusion block declared")
		}
	}
}

func TestOmniNeedsAnOutputModality(t *testing.T) {
	in := omniInputs()
	in.Deployment.Serving.OutputModality = ""
	if _, err := DynamoGraphDeployment(in, testOptions()); err == nil {
		t.Fatal("an omni worker rendered with no --output-modalities")
	}
}

func TestAChatEngineRefusesAnImageOutput(t *testing.T) {
	in := testInputs()
	in.Deployment.Serving.OutputModality = OutputImage
	if _, err := DynamoGraphDeployment(in, testOptions()); err == nil {
		t.Fatal("a vllm worker rendered for an image output it cannot produce")
	}
}

func TestOmniRefusesDisaggregation(t *testing.T) {
	in := omniInputs()
	in.Deployment.Serving.Topology = "disaggregated"
	in.Deployment.Serving.Roles = []wire.RoleSpec{{Role: "prefill"}, {Role: "decode"}}
	in.Deployment.RoleReplicas = map[string]int{"prefill": 1, "decode": 1}
	if _, err := DynamoGraphDeployment(in, testOptions()); err == nil {
		t.Fatal("a disaggregated omni graph rendered")
	}
}

func TestTheDiffusionBlockBecomesAStageConfigFile(t *testing.T) {
	in := omniInputs()
	in.Deployment.Serving.OutputModality = OutputVideo
	in.Deployment.Serving.Diffusion = &wire.DiffusionSpec{
		ModelClassName: "LTX2Pipeline",
		DefaultSamplingParams: map[string]any{
			"num_inference_steps": 30,
			"guidance_scale":      4.0,
		},
	}
	in.Deployment.Serving.EngineArgs = map[string]string{"vae-use-tiling": ""}
	obj, err := DynamoGraphDeployment(in, testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	pod := renderedPod(t, obj)
	main := pod.Containers[0]
	if got := argValue(main.Args, "--stage-configs-path"); got != omniStagePath {
		t.Fatalf("--stage-configs-path = %q", got)
	}
	if !slices.Contains(main.Args, "--vae-use-tiling") {
		t.Fatalf("a valueless engine arg was dropped: %v", main.Args)
	}
	mounted := false
	for _, m := range main.VolumeMounts {
		if m.Name == omniStageVolume && m.MountPath == omniStageDir {
			mounted = true
		}
	}
	if !mounted {
		t.Fatalf("the worker does not mount the stage config: %+v", main.VolumeMounts)
	}

	var config string
	for _, init := range pod.InitContainers {
		if init.Name == "omni-stage-config" {
			config = containerEnv(init, omniStageEnv)
		}
	}
	if config == "" {
		t.Fatal("no init container writes the stage config")
	}
	var got struct {
		AsyncChunk bool   `json:"async_chunk"`
		Executor   string `json:"distributed_executor_backend"`
		Stages     []struct {
			StageID        int            `json:"stage_id"`
			Devices        string         `json:"devices"`
			MaxNumSeqs     int            `json:"max_num_seqs"`
			ModelClassName string         `json:"model_class_name"`
			Defaults       map[string]any `json:"default_sampling_params"`
		} `json:"stages"`
	}
	if err := json.Unmarshal([]byte(config), &got); err != nil {
		t.Fatalf("stage config is not JSON (and so not YAML): %v\n%s", err, config)
	}
	if got.AsyncChunk || got.Executor != "mp" || len(got.Stages) != 1 {
		t.Fatalf("stage config = %+v", got)
	}
	s := got.Stages[0]
	if s.Devices != "0" || s.MaxNumSeqs != 1 || s.ModelClassName != "LTX2Pipeline" {
		t.Fatalf("stage = %+v", s)
	}
	if s.Defaults["num_inference_steps"] != float64(30) || s.Defaults["guidance_scale"] != 4.0 {
		t.Fatalf("default_sampling_params = %v", s.Defaults)
	}
}

func TestTheStageSpansEveryDeviceThePodHolds(t *testing.T) {
	in := omniInputs()
	in.Deployment.Placement.DevicesPerReplica = 2
	in.Deployment.Serving.Diffusion = &wire.DiffusionSpec{ModelClassName: "WanPipeline"}
	config, err := omniStageConfig(in)
	if err != nil {
		t.Fatalf("stage config: %v", err)
	}
	if !strings.Contains(config, `"devices":"0,1"`) {
		t.Fatalf("stage config = %s", config)
	}
}

func TestAnOmniWorkerIsPinnedToAmd64Nodes(t *testing.T) {
	obj, err := DynamoGraphDeployment(omniInputs(), testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	if got := renderedPod(t, obj).NodeSelector[archLabel]; got != "amd64" {
		t.Fatalf("%s = %q, want amd64", archLabel, got)
	}
	if got := NodeSelector(omniInputs().Deployment, testOptions())[archLabel]; got != "amd64" {
		t.Fatalf("the capacity check's selector does not carry the arch: %q", got)
	}
	chat, err := DynamoGraphDeployment(testInputs(), testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	if _, set := renderedPod(t, chat).NodeSelector[archLabel]; set {
		t.Fatal("a chat worker gained an arch selector; its pod template must not move")
	}
}

func TestAnOmniFrontendRoundRobinsUnlessARouterIsDeclared(t *testing.T) {
	in := omniInputs()
	if got := routerMode(t, in); got != OmniRouterMode {
		t.Fatalf("omni DYN_ROUTER_MODE = %q, want %q", got, OmniRouterMode)
	}
	in.Deployment.Serving.Router = &wire.RouterSpec{Mode: ptr("random")}
	if got := routerMode(t, in); got != "random" {
		t.Fatalf("declared DYN_ROUTER_MODE = %q, want random", got)
	}
}
