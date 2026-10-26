#!/usr/bin/env bash
# Seed Secret Manager secrets from a local env file (KEY=VALUE per line).
# Run from a machine with gcloud authed (the Mac). Creates a new secret
# version for each known key; secret containers must already exist
# (created by `terraform apply`).
#
# Portable to bash 3.2 (macOS default) — no associative arrays.
#
#   PROJECT=homelab-424902 ./deploy/scripts/seed-secrets.sh .env.cloud
set -euo pipefail
PROJECT="${PROJECT:-homelab-424902}"
ENV_FILE="${1:?usage: seed-secrets.sh <env-file>}"

# env var name -> Secret Manager secret id
pairs=(
  "MCP_AUTHKIT_DOMAIN=webintel-mcp-authkit-domain"
  "MCP_BASE_URL=webintel-mcp-base-url"
  "MCP_RESOURCE_URL=webintel-mcp-resource-url"
  "MCP_MACHINE_SECRET=webintel-mcp-machine-secret"
  "MCP_MACHINE_ISSUER=webintel-mcp-machine-issuer"
  "MCP_REQUIRED_SCOPES=webintel-mcp-required-scopes"
  "REDDIT_CLIENT_ID=webintel-reddit-client-id"
  "REDDIT_CLIENT_SECRET=webintel-reddit-client-secret"
  "REDDIT_USER_AGENT=webintel-reddit-user-agent"
  "YOUTUBE_PROXY_URL=webintel-youtube-proxy-url"
  "STT_ENDPOINT=webintel-stt-endpoint"
  "STT_MODEL=webintel-stt-model"
  "STT_API_KEY=webintel-stt-api-key"
  "EXA_API_KEY=webintel-exa-api-key"
  "YOUTUBE_API_KEY=webintel-youtube-api-key"
  "CLOUDFLARE_TUNNEL_TOKEN=webintel-cloudflare-tunnel-token"
)

secret_for() {
  local key="$1" kv
  for kv in "${pairs[@]}"; do
    if [ "${kv%%=*}" = "$key" ]; then
      printf '%s' "${kv#*=}"
      return 0
    fi
  done
  return 1
}

gcloud services enable secretmanager.googleapis.com --project "$PROJECT" >/dev/null 2>&1 || true

# Split on the FIRST '=' only; `IFS='=' read` would strip trailing '='
# characters (base64 padding) from values.
while IFS= read -r line; do
  case "$line" in ''|\#*) continue ;; esac
  key="${line%%=*}"
  val="${line#*=}"
  secret="$(secret_for "$key" || true)"
  [ -z "$secret" ] && continue
  if [ -z "$val" ]; then
    # gcloud rejects empty payloads; an unseeded secret already reads as
    # empty on the VM (bootstrap.sh), so skipping is equivalent.
    echo "skipped $key (empty value)"
    continue
  fi
  printf '%s' "$val" | gcloud secrets versions add "$secret" --data-file - --project "$PROJECT" >/dev/null
  echo "seeded $key -> $secret"
done < "$ENV_FILE"

echo "done."
