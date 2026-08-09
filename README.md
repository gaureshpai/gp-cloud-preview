# GP Cloud Preview

Documentation index: [hosting](docs/HOSTING.md) · [architecture](docs/ARCHITECTURE.md)
· [operations](docs/OPERATIONS.md) · [security](docs/SECURITY.md) ·
[local quickstart](docs/LOCAL-QUICKSTART.md) · [contributing](CONTRIBUTING.md)

GP Cloud Preview is a small, self-hostable preview-deployment platform. It
builds an exact Git commit in an isolated Docker container, checks that the
container is healthy, publishes it through Caddy, and removes it when a pull
request closes. It has no application database or AI dependency: the standard-
library API, built-in dashboard, worker, Docker, Caddy, and Prometheus are
enough to run it.

The platform is useful for ordinary web applications and AI-enabled web
applications alike. A non-AI app can be a static Vite/Next export or any
Dockerfile-based HTTP service. An AI app is treated the same way; its model
provider, local model server, vector store, and secrets stay inside its own
container/configuration and are never supplied by the control plane.

## Runtime contract

The harness accepts a local checkout, an exact commit SHA, a deterministic
deployment slug, an application port, and a health-check path. It builds a
Dockerfile image, starts it with bounded resources on the `gp-cloud` network,
waits for an HTTP 200 response, and writes a Caddy route for the preview host.

The control API is bound to `127.0.0.1:8787`. The installer runs in local-only
mode and disables Caddy; use an SSH tunnel for administration. If you
intentionally enable Caddy, it can expose the control API and previews through
the configured domain and HTTP port. Put a TLS proxy or a properly configured
TLS-aware edge in front of it before allowing access from the Internet.

With a secured public edge enabled, open
`https://control.<GP_CLOUD_PREVIEW_DOMAIN>/` in a browser; it redirects to
`/ui/login`. In local-only mode, forward `127.0.0.1:8787` over SSH and open
`http://127.0.0.1:8787/ui` locally.
The console uses a neutral operator interface and does not expose API tokens to
the browser. Set `GP_CLOUD_ADMIN_PASSWORD` for a separate UI password; while
migrating, the API token is accepted as the login password when that value is
blank.

Endpoints:

* `GET /v1/deployments` — browser-readable safe deployment index, reconciled
  with runtime metadata; use `?state=RUNNING` to filter. Authorization adds
  repository, SHA, and diagnostic fields.
* `POST /v1/deployments` — authenticated job creation.
* `GET /v1/deployments/<id>` — status and immutable SHA metadata.
* `GET /v1/deployments/<id>/logs` — retained worker logs.
* `POST /v1/deployments/<id>/stop` — cleanup.
* `POST /webhooks/github` — HMAC-verified `issue_comment` and `pull_request` events.
* `GET /healthz` and `/metrics` — local health and Prometheus metrics.

Preview jobs have a configurable TTL (`GP_CLOUD_DEPLOYMENT_TTL_SECONDS`, one
day by default) and are stopped when their pull request closes. The cleanup
worker runs inside the system service, so it does not depend on an open SSH
session or terminal. Secrets are written to HashiCorp Vault KV v2 and injected
only at container start through a temporary env file that is removed after the
container is launched.

The worker supports GitHub App installation tokens or `GITHUB_TOKEN` for
private clones, but every repository must still be explicitly listed in
`GP_CLOUD_ALLOWED_REPOS`. It performs detached exact-SHA checkout, sequential
builds, health checks, Caddy route registration, and pull-request cleanup.

Repositories do not need a gp-cloud configuration file or deployment-specific
changes. Operators can define project profiles in the dashboard's settings
JSON or the local configuration workflow. Generic repositories are detected
from their Dockerfile/lockfiles; build-only files are materialized only inside
the ephemeral deployment workspace and are never committed back to Git.

## Configuration

Configuration is stored at `/opt/gp-cloud/config/gp-cloud.env`. The dashboard
shows the live path and non-secret variable inventory, but never returns secret
values. Runtime application variables belong in Vault and are injected only
when a deployment starts; paths are restricted to the `gp-cloud/` namespace.
When a secured public edge is enabled, configure the same webhook secret in
GitHub and point the webhook to
`https://control.<your-domain>/webhooks/github`.

## Security baseline

Deployed containers receive no Docker socket, no host filesystem mounts, no
privileged mode, and no access to existing Docker networks. They are limited
to one CPU, 768 MiB, and 256 PIDs by default. The systemd service writes only
GP Cloud state/workspace/log/route folders, while `/root`, `/home`, and other
host folders are inaccessible. The safe deployment index is public while
mutation and diagnostics remain authenticated.

Prometheus and Node Exporter run as host services. Grafana runs as
`gp-cloud-grafana`, bound to loopback port 3000 for SSH-tunnel access; it is
not attached to the deployed-app network.

## Self-hosting: end-to-end setup

The supported target is a dedicated Debian 12 or Ubuntu 24.04 VM with a public
IPv4 address. Use a separate host for production workloads; preview builds are
untrusted application code even though their runtime containers are restricted.

1. Install Docker Engine, Git, curl, Caddy, and systemd-managed Prometheus and
   Node Exporter. For the default local-only mode, do not open application or
   dashboard ports publicly. For intentional public hosting, point a domain
   you control at the secured edge; wildcard DNS is recommended so
   `*.your-domain` resolves to the same edge.

2. Copy this directory to the VM and run the installer as root:

   ```sh
   sudo ./scripts/gp-cloud-install
   ```

   The installer creates `/opt/gp-cloud`, the private Docker network, the
   system unit, and the Caddy environment drop-in. It does not invent secrets.

3. Edit `/opt/gp-cloud/config/gp-cloud.env`:

   ```dotenv
   GP_CLOUD_PREVIEW_DOMAIN=preview.example.com
   GP_CLOUD_API_TOKEN=<openssl rand -hex 32>
   GP_CLOUD_GITHUB_WEBHOOK_SECRET=<openssl rand -hex 32>
   GP_CLOUD_ALLOWED_REPOS=owner/non-ai-site,owner/ai-site
   ```

   For private repositories, set `GITHUB_TOKEN`, or configure the GitHub App
   ID, installation ID, and private-key file. These credentials do not replace
   the repository allowlist. Keep this file mode `0600`.

4. Start and verify the local service:

   ```sh
   sudo systemctl restart gp-cloud.service
   curl http://127.0.0.1:8787/healthz
   sudo journalctl -u gp-cloud.service -f
   ```

   Caddy is installed and validated but disabled by default. The bundled
   Caddyfile is HTTP-only; only enable it after configuring DNS, TLS termination
   at an edge proxy, firewall rules, and an explicit public-edge policy.

5. Configure a GitHub webhook for each allowlisted repository. Use
   `https://control.<your-domain>/webhooks/github`, content type
   `application/json`, the exact `GP_CLOUD_GITHUB_WEBHOOK_SECRET`, and enable
   `issue_comment` and `pull_request`. Comment `/deploy` on a pull request to
   create a preview; closing the pull request removes its container and route.

6. For a direct API deployment, use a full 40-character commit SHA:

   ```sh
   set -a; . /opt/gp-cloud/config/gp-cloud.env; set +a
   curl -X POST https://control.<your-domain>/v1/deployments \
     -H "Authorization: Bearer $GP_CLOUD_API_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"repo_url":"https://github.com/owner/site.git","sha":"<full-sha>","project":"site","app_port":2222,"health_path":"/","vault_path":"gp-cloud/projects/site"}'
   ```

   Poll the returned `id` with `GET /v1/deployments/<id>` and inspect
   `GET /v1/deployments/<id>/logs` when a build fails. Stop it with
   `POST /v1/deployments/<id>/stop`.

7. Validate monitoring at `http://127.0.0.1:8787/metrics` and
   `http://127.0.0.1:9090`. Keep those ports loopback-only. Add the supplied
   Prometheus files under `/etc/prometheus` if Prometheus is enabled, then
   restart Prometheus. Grafana can be reached safely with an SSH tunnel.

## Build profiles

For a project, either commit a Dockerfile that listens on the requested
port or use a detected `uv`, Python `requirements.txt`, npm lockfile, or pnpm
lockfile project with a configured `start_command`. The generated files exist
only in the ephemeral source workspace and are never pushed to the repository.

## Security and operating notes

The API binds to loopback. When enabled, Caddy is the public edge; otherwise
there is no public GP Cloud listener. The safe deployment index and health
endpoint are public at the edge, while deployment mutation, detailed status,
logs, and Vault writes require authentication. GitHub webhooks require HMAC
SHA-256. Runtime
containers have no Docker socket, host mounts, privileged mode, or access to
existing Docker networks, and receive CPU, memory, PID, read-only-root, and
`no-new-privileges` limits. The worker also removes a failed container and
route, preventing orphaned workloads after a build or health-check failure.

The service is intentionally sequential. This makes host capacity predictable;
increase it only after adding queue limits, disk cleanup, and per-tenant
resource accounting.
The live configuration and runtime files are intentionally outside this
checkout so credentials do not appear in VS Code, Git, or pull requests. Open
`/opt/gp-cloud` as a separate folder in VS Code on the deployment VM to inspect
metadata, routes, logs, and retained workspaces. Run `scripts/gp-cloud-inspect`
for a redacted inventory without printing secrets. Build source is deleted by
default after deployment; enable `GP_CLOUD_RETAIN_WORKSPACES=true` temporarily
when debugging a build.

The system unit uses `ProtectSystem=strict`, `ProtectHome=true`, and explicit
write paths limited to GP Cloud state folders plus `/tmp`. The worker never passes
the host filesystem or Docker socket into a deployed app. Keep GitHub private
keys and Vault token files under `/opt/gp-cloud` because `/root`, `/home`, and
other host user directories are intentionally inaccessible to the service.
