// Command hgr is the Hangar command line.
package main

import (
	"os"

	"github.com/hangar-dev/hangar/libs/cli/internal/cli"
)

func main() {
	os.Exit(cli.Execute())
}
