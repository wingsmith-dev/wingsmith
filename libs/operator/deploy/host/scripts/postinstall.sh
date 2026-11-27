#!/bin/sh
# Creates the service user and its directories, then reloads systemd. It never enables or starts
# the unit: a host serves only after its operator enrols it and turns the service on. On an upgrade
# it stops a running agent around the ownership fix and starts it again; replica containers keep
# running and the agent adopts them.
set -e

# deb passes "configure <old-version>" (old version empty on a fresh install), rpm the count of
# installed versions (2 on an upgrade).
upgrade=false
case "$1" in
configure) [ -n "$2" ] && upgrade=true ;;
2) upgrade=true ;;
esac
restart=false
if $upgrade && [ -d /run/systemd/system ] && systemctl is-active --quiet wingsmith-host.service; then
	systemctl stop wingsmith-host.service
	restart=true
fi

if ! getent group wingsmith >/dev/null; then
	groupadd --system wingsmith
fi
if ! getent passwd wingsmith >/dev/null; then
	useradd --system --gid wingsmith --home-dir /var/lib/wingsmith --no-create-home \
		--shell /usr/sbin/nologin --comment "Wingsmith host agent" wingsmith
fi
# The docker group is root-equivalent; the agent needs it to reach the Engine API socket.
if getent group docker >/dev/null; then
	usermod -a -G docker wingsmith
else
	echo "wingsmith-host: no docker group yet; install Docker, then run: usermod -aG docker wingsmith" >&2
fi

install -d -m 0750 -o root -g wingsmith /etc/wingsmith
install -d -m 0700 -o wingsmith -g wingsmith /etc/wingsmith/credentials
install -d -m 0750 -o wingsmith -g wingsmith /var/lib/wingsmith
# An install that ran as root before the service user existed left root-owned state and token.
chown -R wingsmith:wingsmith /var/lib/wingsmith
if [ -f /etc/wingsmith/token ]; then
	chown wingsmith:wingsmith /etc/wingsmith/token
	chmod 0600 /etc/wingsmith/token
fi
chown root:wingsmith /etc/wingsmith/host.json 2>/dev/null || true

if [ -d /run/systemd/system ]; then
	systemctl daemon-reload || true
fi
if $restart; then
	systemctl start wingsmith-host.service
fi
