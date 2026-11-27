#!/bin/sh
# Stops the agent before its files go. Containers it started keep running; they are Docker's.
set -e
# deb passes "remove"/"upgrade", rpm passes the count of versions left (0 on erase).
case "$1" in
remove | 0)
	if [ -d /run/systemd/system ]; then
		systemctl disable --now wingsmith-host.service >/dev/null 2>&1 || true
	fi
	;;
esac
