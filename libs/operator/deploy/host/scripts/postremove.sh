#!/bin/sh
# Leaves /etc/wingsmith, /var/lib/wingsmith and the wingsmith user in place: they hold the
# enrolment token and the model cache. docs/operations/linux-host.md lists how to purge them.
set -e
if [ -d /run/systemd/system ]; then
	systemctl daemon-reload || true
fi
