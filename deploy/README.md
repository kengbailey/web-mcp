# Cloud Deployment (GCP)

Provisions a single GCE VM running Web MCP behind a
Cloudflare Tunnel, with OAuth 2.1 (WorkOS AuthKit) + HMAC-JWT auth. See
`doc/cloud-auth.md` for the auth model.

## One-time prerequisites

1. gcloud authed locally (`gcloud auth login`), default project set
   (`gcloud config set project homelab-424902`), and Application Default
   Credentials for Terraform (`gcloud auth application-default login`).
2. Terraform installed (`brew install hashicorp/tap/terraform` on macOS —
   the homebrew-core `terraform` formula is disabled).
3. APIs enabled (Terraform does this, but you can pre-enable):
   `gcloud services enable compute.googleapis.com secretmanager.googleapis.com`.

## Provision

```bash
cd deploy/terraform
terraform init
terraform apply                 # creates VPC, static IP, firewall, VM, secret containers, SA
```

On first boot the VM installs Docker, clones the repo at `var.repo_ref`
(default `feat/cloud-auth`), and runs `deploy/scripts/bootstrap.sh`, which
fetches secrets and starts the stack with `docker-compose.cloud.yml`. With no
secrets seeded yet, the server starts **unauthenticated** (auth off) — safe to
validate locally before exposing publicly.

## Seed secrets

Create a local `.env.cloud` (gitignored, never committed) with the values:

```dotenv
MCP_AUTHKIT_DOMAIN=https://<project>.authkit.app
MCP_BASE_URL=https://mcp.example.com
MCP_RESOURCE_URL=https://mcp.example.com/mcp
MCP_MACHINE_SECRET=<python -c 'import secrets;print(secrets.token_urlsafe(48))'>
MCP_MACHINE_ISSUER=webintel-internal
MCP_REQUIRED_SCOPES=web-mcp:read
REDDIT_CLIENT_ID=...
REDDIT_CLIENT_SECRET=...
REDDIT_USER_AGENT=python:web-mcp:v3 (by /u/you)
EXA_API_KEY=...
YOUTUBE_API_KEY=...
STT_ENDPOINT=https://openrouter.ai/api/v1
STT_MODEL=nvidia/parakeet-tdt-0.6b-v3
STT_API_KEY=<OpenRouter key>
CLOUDFLARE_TUNNEL_TOKEN=...
```

`EXA_API_KEY` and `YOUTUBE_API_KEY` need the `webintel-exa-api-key` /
`webintel-youtube-api-key` secret containers: run `terraform apply` once after
upgrading to v3 (additive; nothing is replaced).

**Do not edit `deploy/terraform/scripts/vm-startup.sh` casually:** it is the instance's
`metadata_startup_script`, which Terraform cannot update in place, so any change recreates
the VM on the next `terraform apply`. (Its clone URL still says `webintel-mcp`; GitHub
redirects it to `web-mcp`.)

**Naming:** the product was renamed to Web MCP (`web-mcp`), but live infrastructure
keeps its original `webintel` identifiers on purpose: GCP resource names (VM
`webintel-mcp`, `webintel-ip`, `webintel-net`, `webintel-vm`), Secret Manager
`webintel-*` secrets, `/opt/webintel-mcp`, the `webintel.kengbailey.com` hostname and
the `webintel-internal` machine-JWT issuer. Renaming them would recreate the VM,
release the static IP, drop secret values and invalidate issued tokens.

Seed them (run from a machine with gcloud):

```bash
PROJECT=homelab-424902 ./deploy/scripts/seed-secrets.sh .env.cloud
```

Then re-run the bootstrap on the VM so it picks up the new values:

```bash
gcloud compute ssh webintel-mcp --zone=us-central1-a --tunnel-through-iap \
  -- sudo /opt/webintel-mcp/deploy/scripts/bootstrap.sh
```

## Cloudflare tunnel

Create a tunnel in the Cloudflare Zero Trust dashboard, point its public
hostname (e.g. `https://mcp.example.com`) at `http://webintel-mcp:3090` (a network
alias of the `web-mcp` service; `http://web-mcp:3090` also works), and
put the tunnel token in the `webintel-cloudflare-tunnel-token` secret. The
`cloudflared` container in `docker-compose.cloud.yml` runs the tunnel.

## Validate

```bash
# from the Mac, via IAP (no public MCP port)
gcloud compute ssh webintel-mcp --zone=us-central1-a --tunnel-through-iap \
  -- curl -s http://localhost:3090/mcp -o /dev/null -w '%{http_code}\n'
```

With auth on, unauthenticated MCP requests get HTTP 401; a request with a
machine JWT (`scripts/mint-machine-jwt.py`) succeeds.

## Teardown

```bash
cd deploy/terraform && terraform destroy
```
