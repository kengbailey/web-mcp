#!/usr/bin/env bash
# VM first-boot setup: install Docker + git, clone the repo at the pinned ref,
# then run the bootstrap script (fetches secrets, starts the stack).
set -euo pipefail

APP_DIR="/opt/webintel-mcp"
META="http://metadata.google.internal/computeMetadata/v1"
REPO_REF="$(curl -s -H "Metadata-Flavor: Google" "${META}/instance/attributes/repo-ref" || echo feat/cloud-auth)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl gnupg python3 git lsb-release

# --- Docker Engine + Compose plugin ---
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
CODENAME="$(. /etc/os-release && echo "$VERSION_CODENAME")"
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

# --- Clone repo at the pinned ref ---
rm -rf "$APP_DIR"
git clone --depth 1 --branch "$REPO_REF" https://github.com/kengbailey/webintel-mcp.git "$APP_DIR"

# --- Fetch secrets + start the stack (re-runnable) ---
bash "$APP_DIR/deploy/scripts/bootstrap.sh" "$APP_DIR"
