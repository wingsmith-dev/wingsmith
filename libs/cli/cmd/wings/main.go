// Command wings is the Wingsmith command line.
package main

import (
	"os"

	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/cli"
)

func main() {
	os.Exit(cli.Execute())
}
