module github.com/wingsmith-dev/wingsmith/libs/gateway

go 1.26.0

require (
	github.com/prometheus/client_golang v1.25.0
	github.com/prometheus/client_model v0.6.3
	github.com/wingsmith-dev/wingsmith/libs/packages/tunnel v0.0.0
)

// The wire lives in this repository; there is no published version to resolve.
replace github.com/wingsmith-dev/wingsmith/libs/packages/tunnel => ../packages/tunnel

require (
	github.com/beorn7/perks v1.0.1 // indirect
	github.com/cespare/xxhash/v2 v2.3.0 // indirect
	github.com/munnerz/goautoneg v0.0.0-20191010083416-a7dc8b61c822 // indirect
	github.com/prometheus/common v0.72.0 // indirect
	github.com/prometheus/procfs v0.22.0 // indirect
	golang.org/x/net v0.61.0 // indirect
	golang.org/x/sys v0.49.0 // indirect
	golang.org/x/text v0.43.0 // indirect
	google.golang.org/protobuf v1.36.12 // indirect
)
