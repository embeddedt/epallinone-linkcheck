#!/usr/bin/env bash
# Redeploy: pull latest code, resync deps as the linkcheck user, restart the service.
# Run as root (or via sudo) on the app server. Assumes the one-time setup from
# deploy/linkcheck.service's header comments has already been done: linkcheck
# system user, /opt/linkcheck checked out and owned by linkcheck, /var/lib/linkcheck
# created (0755, owned by linkcheck) as its uv HOME.
set -euo pipefail

APP_DIR=/opt/linkcheck
STATE_DIR=/var/lib/linkcheck
UV_BIN=/usr/local/bin/uv

git -C "$APP_DIR" pull --ff-only
chown -R linkcheck:linkcheck "$APP_DIR"
sudo -u linkcheck env HOME="$STATE_DIR" "$UV_BIN" sync --directory "$APP_DIR"
systemctl restart linkcheck.service
