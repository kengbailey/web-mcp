#!/usr/bin/env bash
# Fetch Web MCP secrets from Secret Manager and (re)start the cloud stack.
# Idempotent and re-runnable. Run on the VM as root (or via `gcloud compute ssh`).
#
#   sudo /opt/webintel-mcp/deploy/scripts/bootstrap.sh
set -euo pipefail

APP_DIR="${1:-/opt/webintel-mcp}"
ENV_FILE="/opt/webintel.env"
META="http://metadata.google.internal/computeMetadata/v1"
SECRET_API="https://secretmanager.googleapis.com/v1"

PROJECT=$(curl -s -H 'Metadata-Flavor: Google' "${META}/project/project-id")
TOKEN=$(curl -s -H 'Metadata-Flavor: Google' "${META}/instance/service-accounts/default/token" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# Empty output = secret missing/unseeded OR fetch error; both are reported in
# the summary below and gated by the fail-closed check.
get_secret() {
  local secret="$1"
  curl -s -H "Authorization: Bearer ${TOKEN}" "${SECRET_API}/projects/${PROJECT}/secrets/${secret}/versions/latest:access" | python3 -c 'import sys,json,base64;d=json.load(sys.stdin);print(base64.b64decode(d["payload"]["data"]).decode())' 2>/dev/null || true
}

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

umask 077
: > "$ENV_FILE"
chmod 600 "$ENV_FILE"

missing=()
tunnel_token=""
authkit_domain=""
base_url=""
for kv in "${pairs[@]}"; do
  var="${kv%%=*}"
  secret="${kv#*=}"
  val="$(get_secret "$secret")"
  [ -z "$val" ] && missing+=("$var")
  case "$var" in
    CLOUDFLARE_TUNNEL_TOKEN) tunnel_token="$val" ;;
    MCP_AUTHKIT_DOMAIN)      authkit_domain="$val" ;;
    MCP_BASE_URL)            base_url="$val" ;;
  esac
  printf '%s=%s\n' "$var" "$val" >> "$ENV_FILE"
done

if [ "${#missing[@]}" -gt 0 ]; then
  echo "bootstrap: no value for: ${missing[*]}" >&2
fi

# Fail closed: never publish an unauthenticated server. If the tunnel token is
# seeded but the OAuth config is not, a seeding or fetch failure would
# otherwise expose the MCP server publicly with auth disabled.
if [ -n "$tunnel_token" ] && { [ -z "$authkit_domain" ] || [ -z "$base_url" ]; }; then
  echo "bootstrap: FATAL: CLOUDFLARE_TUNNEL_TOKEN is seeded but MCP_AUTHKIT_DOMAIN/MCP_BASE_URL are empty." >&2
  echo "bootstrap: refusing to start a publicly tunneled server without auth. Seed the auth secrets (deploy/scripts/seed-secrets.sh) or remove the tunnel token secret." >&2
  exit 1
fi

cd "$APP_DIR"
docker compose -f docker-compose.cloud.yml --env-file "$ENV_FILE" up -d --build
