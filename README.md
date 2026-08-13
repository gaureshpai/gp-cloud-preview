# GP Cloud Preview

GP Cloud Preview is a small, self-hosted pull-request preview control plane.
It checks out an exact Git commit, builds and health-checks a deployment
candidate, and publishes the healthy candidate through Caddy. The default
installation is local-only: the API listens on `127.0.0.1:8787`, GP Cloud does
not configure or enable Caddy, and the optional monitoring service is not
enabled.

Documentation: [hosting](docs/HOSTING.md) ·
[local quickstart](docs/LOCAL-QUICKSTART.md) ·
[architecture](docs/ARCHITECTURE.md) · [operations](docs/OPERATIONS.md) ·
[security](docs/SECURITY.md) · [monitoring](docs/MONITORING.md) ·
[contributing](CONTRIBUTING.md)

## Lifecycle model

A **preview** is the stable environment for a repository pull request (or a
direct project deployment). A **deployment** is one immutable build attempt.
A preview retains all deployment history and has at most one current
deployment.

- Candidates use deployment-specific workspaces, logs, image tags,
  containers, and internal Docker networks.
- A redeploy leaves the current deployment and route running while the
  candidate builds and passes its health check.
- Promotion atomically replaces the stable Caddy route. Only then is the old
  deployment marked `SUPERSEDED` and cleaned.
- A failed replacement is `FAILED`; it does not supersede or remove a healthy
  current deployment.
- Deploy, stop, cleanup, TTL, and pull-request-close requests are serialized by
  the durable worker. HTTP handlers only enqueue requested transitions.

Deployment transitions are:

```text
QUEUED -> BUILDING -> RUNNING -> SUPERSEDED
   |         |           |
   +---------+-----------+-> STOPPED
             +--------------> FAILED
```

State is atomic JSON under `/opt/gp-cloud/data/deployments` and
`/opt/gp-cloud/data/previews`. Durable operation markers are under
`/opt/gp-cloud/data/queue`.

## GitHub command contract

Only a standalone, lowercase `/deploy` comment is accepted. Spaces or tabs
around the command are allowed. Arguments, multiline commands, prose,
case variants, quotes, inline code, fenced code, and strings such as
`/deployment` never deploy. An otherwise standalone `/deploy` with arguments
returns a usage error.

The webhook must be an `issue_comment` `created` event from an author whose
signed `author_association` is one of `OWNER`, `MEMBER`, or `COLLABORATOR` by
default. Every request requires HMAC SHA-256 and a unique
`X-GitHub-Delivery`; delivery IDs are durably deduplicated. Pull-request close
events request cleanup.

## Endpoints and access boundaries

The service binds only to loopback. Locally available endpoints include:

- `GET /healthz` and `GET /metrics`
- `GET /v1/deployments` (safe fields without authentication; full history and
  diagnostics with the bearer token)
- `POST /v1/deployments`, deployment status/logs, and stop requests (bearer
  token required)
- `/ui` and `/ui/api/*` (operator session required; mutation requests require
  JSON and same-origin browser requests)
- `POST /webhooks/github` (GitHub HMAC, event, actor, and replay checks)
- `/actions/gp-cloud-deploy*` (repository-scoped GitHub Action token)

Public Caddy mode uses separate host policies:

- `webhook.<domain>` exposes only `POST /webhooks/github`;
- `actions.<domain>` exposes only the GitHub Action endpoints;
- `control.<domain>` exposes only `/` and `/ui*`;
- preview hostnames expose only their application;
- `/v1`, `/metrics`, `/healthz`, and internal/admin paths remain loopback-only.

No permissive CORS headers are returned.

## Install

The supported host is Debian 12 or Ubuntu 24.04 with Python 3.11 or newer,
Docker Engine and its Buildx plugin, Git, curl, Caddy, and systemd.

```sh
sudo ./scripts/gp-cloud-install
sudoedit /opt/gp-cloud/config/gp-cloud.env
sudo systemctl restart gp-cloud.service
curl http://127.0.0.1:8787/healthz
```

The base installer does not generate credentials, alter Prometheus, or touch
an operator-owned Caddy service. Access the dashboard locally or through an
SSH tunnel:

```sh
ssh -L 8787:127.0.0.1:8787 user@host
```

Then open `http://127.0.0.1:8787/ui`.

## Optional wildcard HTTPS

Public mode uses a DNS-01 wildcard certificate managed and renewed by Caddy.
The bundled configuration expects a Caddy build with
`github.com/caddy-dns/cloudflare` and a Cloudflare token limited to DNS edits
for the preview zone. Configure wildcard DNS (`*.preview.example.com`) and set
these values in `gp-cloud.env`:

```dotenv
GP_CLOUD_PREVIEW_DOMAIN=preview.example.com
GP_CLOUD_PUBLIC_SCHEME=https
GP_CLOUD_HTTP_PORT=443
GP_CLOUD_COOKIE_SECURE=true
```

Set edge-only values in `/opt/gp-cloud/config/caddy.env`:

```dotenv
GP_CLOUD_PREVIEW_DOMAIN=preview.example.com
GP_CLOUD_CONTROL_PORT=8787
GP_CLOUD_CADDY_ACME_EMAIL=operator@example.com
GP_CLOUD_CLOUDFLARE_API_TOKEN=<zone-limited-token>
```

Then rerun:

```sh
sudo ./scripts/gp-cloud-install --enable-public-edge
sudo systemctl is-active caddy
```

The installer starts Caddy when it is stopped and reloads it when it is
already running, so the new public-edge configuration is applied immediately.

To stop a GP Cloud-managed public edge without deleting its retained
configuration and backups, run
`sudo ./scripts/gp-cloud-install --disable-public-edge`. The installer refuses
to disable an unmarked operator-owned Caddy service.

HTTP wildcard requests redirect to HTTPS. Validate certificate issuance,
renewal, hostname routing, and the public route matrix with the commands in
[HOSTING.md](docs/HOSTING.md). Do not put the DNS token in the general
application environment or repository.

## Optional monitoring

Monitoring is off by default and is never required by the control plane or
worker. The dedicated service uses `127.0.0.1:9091` so it can coexist with a
distribution Prometheus on its usual 9090. To install it:

```sh
sudo ./scripts/gp-cloud-install --enable-monitoring
# or later:
sudo /opt/gp-cloud/worker/gp-cloud-monitoring enable
```

See [MONITORING.md](docs/MONITORING.md) for health checks, failure semantics,
disable, and uninstall behavior.

## Isolation and security

Runtime containers have no Docker socket or host mounts. They use unique
internal networks, loopback-only published ports, read-only root filesystems,
all capabilities dropped, `no-new-privileges`, CPU/memory/PID/ulimit bounds,
and bounded local Docker logs. Build and runtime artifacts cannot collide
between deployment attempts.

Builds require Docker Buildx and use a unique ephemeral docker-container
builder with configurable memory/CPU bounds (`GP_CLOUD_BUILD_MEMORY_LIMIT` and
`GP_CLOUD_BUILD_CPU_QUOTA`), no shared cache reuse, and build-step networking
disabled by default. A trusted direct deployment may use a canonical repository
profile with `allow_build_network: true`. PR builds additionally require the
host-wide `GP_CLOUD_ALLOW_PR_BUILD_NETWORK=true`; keep it false for hostile PR
source. This double opt-in permits normal Docker egress. Images declaring
`VOLUME` are rejected so anonymous writable volumes cannot bypass runtime
storage bounds.

Untrusted Dockerfile builds still execute through the host Docker daemon. This
is not a hostile multi-tenant sandbox. Use a dedicated VM and move builds to a
rootless, isolated builder or per-job VM before accepting untrusted public
contributors. Read [SECURITY.md](docs/SECURITY.md) before enabling a public
edge.

Application runtime secrets are read from an operator-limited Vault KV v2 path
below `gp-cloud/`, written to a temporary mode-0600 env file, and deleted after
container start. Pull-request deployments receive none by default. PR secrets
require a canonical `owner/repository` profile with `allow_pr_secrets: true`
and the host-wide `GP_CLOUD_ALLOW_PR_SECRETS=true`; use only disposable preview
credentials. Docker retains container environment values for the runtime
lifetime, so host-root/Docker-daemon access remains trusted.

## Development

Run the complete local quality contract with:

```sh
./scripts/check
```

It runs unit/integration tests, compilation, Ruff lint/format, shell syntax and
ShellCheck, Prometheus validation when available, Caddy formatting, and Git
whitespace checks. CI pins Python 3.11 and installs Caddy, Prometheus tools,
ShellCheck, and pinned Ruff before running the same entrypoint with
least-privilege workflow permissions.
