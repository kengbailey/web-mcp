> Historical record of the v2 cutover on Oak (2026-09-22). Names here (webintel-mcp
> container, WEBINTEL_* variables) predate the v3 rename to web-mcp.

# V2 release and Oak cutover

## What merge does

The main-branch workflow builds and publishes amd64/arm64 images to GHCR. It does
not contain an Oak deployment step. PR builds validate without publishing.
The default Docker command now starts `src.server.delivery_server`, not the legacy
server. Refresh client tool catalogs when switching to v2.

## Artifact and configuration

Deploy the **tested image ID/digest**, not a fresh build or an automatically updated
`latest` tag. Python base/OS/transitive packages are not fully locked; a rebuild
from the same commit can resolve differently. yt-dlp and Deno are pinned.

Compose requires support for `env_file.format: raw` (Compose 2.30+), preventing
interpolation of literal credential values. Keep `runtime.env` owner-only and
outside version control. Do not print `docker inspect` environment values or a
fully expanded `docker compose config` into logs; use `config --quiet`.

Oak's staged directory is `/home/syran/sandbox/webintel-release-20260922`:

- `source/`: release checkout/build context, with no credentials.
- `runtime.env`: existing Reddit/STT/proxy settings plus the saved Exa/YouTube keys.
- `rollback/production-inspect.json`: owner-only original container configuration.
- `rollback/compose-*.yml`: owner-only original Compose configuration files.
- `baseline.json`: nonsensitive original image/container/network identifiers.
- `candidate.override.yml`: separate candidate name, existing external network,
  automatic Watchtower updates disabled.
- `oak.override.yml`: same network/update policy for the eventual production v2.

Existing production authentication is disabled/LAN-only. This release preserves
that setting; no new exposure or auth migration is part of the cutover. Existing
OAuth/machine-JWT support remains. Browser OAuth login is not a release acceptance
claim for an Oak deployment that does not enable it.

## Validation before approval

1. Build a separately tagged candidate without overwriting `latest`.
2. Run the regression suite against `/app/src` inside that exact image, mounting
   tests/scripts only, not replacement application source.
3. Start a separate Compose project/container on loopback 13091, attached to the
   existing `webintel-mcp_default` network. Use a different container name and
   disable automatic image updates for the candidate.
4. Verify health, 12-tool catalog, actual HTTP tool calls, bounded continuation,
   provider access, default captions and explicit STT. Test SSE compatibility.
5. Keep the original image tagged `webintel-mcp:rollback-v1-20260922`; preserve its
   full configuration privately. Record the candidate image ID and baseline ID.
6. Confirm production image/start time unchanged and review the final PR.

## Approved cutover (not performed by preparation)

Use a **new Compose project `webintel-v2`**. Reusing the old project after renaming
its container can cause Compose to recreate/delete the retained rollback
container based on its old project/service labels.

On Oak, after merge/review approval and final image-ID verification:

```sh
cd /home/syran/sandbox/webintel-release-20260922
# Set this to the verified candidate image tag; verify its recorded image ID.
export WEBINTEL_IMAGE=webintel-mcp:candidate-v2-20260922
export WEBINTEL_ENV_FILE="$PWD/runtime.env"
export MCP_HOST_PORT=3090
export MCP_BIND_ADDRESS=0.0.0.0
python3 - <<'PYVERIFY'
import json, os, subprocess
expected = json.load(open("release-manifest.json"))["candidate_image"]
actual = subprocess.check_output(
    ["docker", "image", "inspect", "--format", "{{.Id}}", os.environ["WEBINTEL_IMAGE"]],
    text=True,
).strip()
assert actual == expected, "Image differs from the validated release; stop here"
PYVERIFY

docker stop webintel-mcp
docker rename webintel-mcp webintel-mcp-rollback-20260922
docker compose -p webintel-v2 -f source/docker-compose.yml -f oak.override.yml \
  up -d --no-build --no-deps webintel-mcp
```

Verify health and actual MCP initialization/tool calls on 3090. Refresh client
catalogs; allow 300 seconds for STT-enabled transcript calls. Keep old image and
stopped rollback container until acceptance. V2 process-local cursors do not
survive a container restart; clients must start a new fetch if a cursor expires.

After acceptance, the two standalone SearxNG containers can be stopped separately
by their verified names. Preserve their configuration during the rollback window.
**Do not stop Gluetun or the YouTube proxy** if current routing still uses them.
Do not run `compose down` or `--remove-orphans` against the old stack.

## Rollback

If the new server fails acceptance, stop it and retain it for diagnostics before
restoring the old container with its original configuration:

```sh
docker stop webintel-mcp
docker rename webintel-mcp webintel-v2-failed-20260922
docker rename webintel-mcp-rollback-20260922 webintel-mcp
docker start webintel-mcp
```

If the new container failed to be created, skip the first two commands. If
SearxNG was stopped after acceptance, restore its required old containers before
checking legacy search. Restore clients' legacy tool catalogs when rolling back.
No OpenClaw Gateway restart is required for either cutover or rollback.
