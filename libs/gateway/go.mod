module github.com/hangar-dev/hangar/libs/gateway

go 1.25.0

require (
	github.com/hangar-dev/hangar/libs/packages/tunnel v0.0.0
	github.com/prometheus/client_golang v1.23.2
	github.com/prometheus/client_model v0.6.2
)

// The wire lives in this repository; there is no published version to resolve.
replace github.com/hangar-dev/hangar/libs/packages/tunnel => ../packages/tunnel

require (
	github.com/beorn7/perks v1.0.1 // indirect
	github.com/cespare/xxhash/v2 v2.3.0 // indirect
	github.com/kr/text v0.2.0 // indirect
	github.com/munnerz/goautoneg v0.0.0-20191010083416-a7dc8b61c822 // indirect
	github.com/prometheus/common v0.66.1 // indirect
	github.com/prometheus/procfs v0.16.1 // indirect
	github.com/rogpeppe/go-internal v1.12.0 // indirect
	go.yaml.in/yaml/v2 v2.4.2 // indirect
	golang.org/x/net v0.58.0 // indirect
	golang.org/x/sys v0.47.0 // indirect
	golang.org/x/text v0.41.0 // indirect
	google.golang.org/protobuf v1.36.8 // indirect
)
