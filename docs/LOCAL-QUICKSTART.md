# Run GP Cloud Preview without a VM

This guide is for someone who has no hosted VM and wants to run GP Cloud
Preview on a Linux laptop, desktop, home server, or physical machine.

The simplest and safest setup is local-only: the control API listens only on
`127.0.0.1:8787`, Caddy is disabled, and nothing is exposed to the Internet.
You can deploy and inspect projects from the same computer. Public GitHub
webhooks and public preview URLs require a separately secured HTTPS tunnel or
edge; that is an optional advanced setup described at the end.

## Supported local host

The installer expects a Debian/Ubuntu-style Linux system with:

- systemd and root/sudo access;
- Docker Engine and a working Docker daemon;
- Git, curl, Caddy, and Python 3.12 or newer;
- enough disk space for Docker images and temporary build checkouts.

No public IP address, DNS record, TLS certificate, or cloud account is needed
for the local-only workflow.

macOS and Windows are not direct installer targets because the service uses
Linux systemd units, `/opt/gp-cloud`, Linux permissions, and the Docker Engine
API. Run the supported Linux workflow on native Linux, or provide a Linux
environment such as WSL2 with systemd and Docker configured. Docker Desktop by
itself does not provide the systemd host expected by the installer.

## 1. Install prerequisites

On Ubuntu or Debian, install the basic packages and Docker. Use the official
Docker and Caddy installation instructions if those packages are not available
from your distribution:

```sh
sudo apt update
sudo apt install -y git curl python3 docker.io
sudo systemctl enable --now docker
```

Confirm that the daemon is usable before continuing:

```sh
docker info
python3 --version
git --version
curl --version
```

Install Caddy and confirm it is available as a command:

```sh
caddy version
```

The GP Cloud installer validates the Caddyfile even though it disables the
Caddy service in local-only mode. This catches configuration errors before an
operator later enables a public edge.

## 2. Download and install the project

Clone this repository, or use your fork:

```sh
git clone <repository-url> gp-cloud-preview
cd gp-cloud-preview
sudo ./scripts/gp-cloud-install
```

The installer creates the live installation outside the checkout:

```text
/opt/gp-cloud/
├── config/gp-cloud.env       # local host configuration, mode 0600
├── control/                  # installed control service
├── data/                     # deployment state and queue markers
├── deployments/              # temporary build/runtime metadata
├── logs/                     # retained deployment logs
└── worker/                   # installed shell harnesses
```

It also installs and enables `gp-cloud.service`, creates the internal Docker
network, validates Caddy, and disables `caddy.service`. It does not generate
credentials and does not make the machine public.

## 3. Configure local-only mode

Edit the live configuration, not the example file in Git:

```sh
sudoedit /opt/gp-cloud/config/gp-cloud.env
```

Set at least these values:

```dotenv
GP_CLOUD_PREVIEW_DOMAIN=preview.example.com
GP_CLOUD_API_TOKEN=<random-long-token>
GP_CLOUD_ADMIN_PASSWORD=<different-local-admin-password>
GP_CLOUD_ALLOWED_REPOS=owner/example-repository
GP_CLOUD_HTTP_PORT=80
GP_CLOUD_PUBLIC_SCHEME=http
GP_CLOUD_COOKIE_SECURE=false
GP_CLOUD_CONTROL_PORT=8787
GP_CLOUD_DEFAULT_APP_PORT=2222
GP_CLOUD_RETAIN_WORKSPACES=false
```

Generate credentials in a separate terminal and paste them into the file:

```sh
openssl rand -hex 32
```

`GP_CLOUD_ALLOWED_REPOS` is fail-closed. An empty value denies deployments.
List every repository explicitly, separated by commas:

```dotenv
GP_CLOUD_ALLOWED_REPOS=owner/site-one,owner/site-two
```

The preview domain is only a name used in generated preview URLs while Caddy
is disabled. It does not need to resolve in local-only mode.

Do not put application secrets in this file when they belong to a deployment.
Those values should be stored in Vault and referenced with a path below
`gp-cloud/`. You can run deployments without Vault when the application needs
no runtime secrets.

Restart the service and verify it:

```sh
sudo systemctl restart gp-cloud.service
curl http://127.0.0.1:8787/healthz
sudo systemctl status gp-cloud.service --no-pager
```

The health response should contain `"ok": true`.

## 4. Open the dashboard

On the same computer, open:

```text
http://127.0.0.1:8787/ui
```

Sign in with `GP_CLOUD_ADMIN_PASSWORD`. If it is blank, the API token is used
as the temporary login password, but setting a separate admin password is
strongly recommended.

The dashboard can:

- create a deployment from a full commit SHA;
- show safe deployment state and logs;
- stop one deployment or stop all active deployments;
- delete deployment records, routes, logs, and runtime artifacts;
- show GP Cloud-scoped storage and container metrics;
- edit non-secret host settings;
- write application environment values to Vault.

The dashboard never displays API tokens, webhook secrets, Vault tokens, GitHub
credentials, or the admin password.

## 5. Prepare an application

The repository must be allowlisted and the requested commit must be a full
40-character SHA. The application should contain a Dockerfile and listen on
the requested container port. The default is `2222`:

```dockerfile
FROM busybox:1.36
RUN mkdir -p /www && printf 'hello from gp-cloud\n' > /www/index.html
EXPOSE 2222
CMD ["httpd", "-f", "-p", "2222", "-h", "/www"]
```

The health path must return HTTP 200. For the example above, `/` is sufficient.
The host port is allocated dynamically on loopback; applications are not
published directly to the LAN or Internet.

The repository includes additional Docker fixtures under `fixtures/` for
testing. `fixtures/smoke-app` should return HTTP 200 on port `2222`;
`fixtures/failing-health-app` intentionally exits so you can test failure
cleanup. See [fixtures/README.md](../fixtures/README.md).

Repositories using supported lockfiles can use an operator-defined runtime
profile, but a Dockerfile is the clearest first test. Build-only files are
created in the temporary deployment workspace and are not committed upstream.

## 6. Create the first local deployment

Find the full commit SHA in the application repository:

```sh
git rev-parse HEAD
```

Create a deployment through the dashboard, or use the local API. The API
example below keeps the token in the current shell only:

```sh
export GP_CLOUD_LOCAL_TOKEN='<the-value-of-GP_CLOUD_API_TOKEN>'
curl -X POST http://127.0.0.1:8787/v1/deployments \
  -H "Authorization: Bearer $GP_CLOUD_LOCAL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_url": "https://github.com/owner/example-repository.git",
    "sha": "<40-character-commit-sha>",
    "project": "example-repository",
    "app_port": 2222,
    "health_path": "/"
  }'
```

The response contains a deployment ID and `QUEUED` state. Poll it locally:

```sh
curl -H "Authorization: Bearer $GP_CLOUD_LOCAL_TOKEN" \
  http://127.0.0.1:8787/v1/deployments/<deployment-id>

curl -H "Authorization: Bearer $GP_CLOUD_LOCAL_TOKEN" \
  http://127.0.0.1:8787/v1/deployments/<deployment-id>/logs
```

A successful local deployment has a generated preview URL, but that URL is
not reachable while Caddy is disabled. The deployment container is still
running on an internal Docker network and can be stopped from the dashboard.
This local-only mode is intended to validate builds, health checks, cleanup,
configuration, and resource accounting without publishing the application.

## 7. Configure application environment values with Vault

Vault is optional for a first smoke test. If an application needs secrets:

1. Run HashiCorp Vault separately and create a KV v2 mount.
2. Give GP Cloud a policy limited to the `gp-cloud/` project prefix.
3. Store the token in a root-owned mode-0600 file.
4. Set `GP_CLOUD_VAULT_ADDR`, `GP_CLOUD_VAULT_TOKEN_FILE`, and
   `GP_CLOUD_VAULT_MOUNT` in `/opt/gp-cloud/config/gp-cloud.env`.
5. Restart `gp-cloud.service`.
6. Use the dashboard Vault panel to write values and set the deployment's
   `vault_path`, such as `gp-cloud/projects/example-repository`.

Vault values are injected only when the runtime container starts. They are not
stored in deployment JSON, logs, or browser responses.

## 8. Use GitHub automation locally

GitHub cannot call `127.0.0.1` on your computer. Therefore the GitHub webhook
workflow is not available in local-only mode unless you provide a temporary,
secured HTTPS tunnel or another public edge.

You can still deploy locally by:

- using the dashboard;
- calling the authenticated local API as shown above; or
- running a GitHub Action against a secured external endpoint that forwards to
  your local service.

Never commit a tunnel URL or token to a workflow. Put the control URL in the
Actions secret `GP_CLOUD_CONTROL_URL`, and ensure the endpoint is HTTPS and
reachable from GitHub-hosted runners.

## 9. Stop, delete, and inspect

Emergency controls are available in the dashboard. From the terminal:

```sh
sudo /opt/gp-cloud/worker/gp-cloud-inspect
sudo journalctl -u gp-cloud.service -f
curl http://127.0.0.1:8787/metrics
docker ps --filter name=gp-cloud-
```

Use **Stop all** to stop active workloads while retaining records. Use
**Delete all** to remove GP Cloud deployment records, logs, generated Caddy
routes, and runtime images. Vault secrets are intentionally not removed by
that action.

## 10. Troubleshooting

### The dashboard returns connection refused

Check the service and its logs:

```sh
sudo systemctl status gp-cloud.service --no-pager
sudo journalctl -u gp-cloud.service -n 100 --no-pager
```

Confirm that the API token is configured and that nothing else is using port
8787.

### A deployment is rejected

Check that the repository exactly matches an entry in
`GP_CLOUD_ALLOWED_REPOS`, the SHA has 40 hexadecimal characters, the Dockerfile
exists, and the application listens on the requested port.

### A deployment is queued but does not become running

Inspect the deployment log and Docker state:

```sh
curl -H "Authorization: Bearer $GP_CLOUD_LOCAL_TOKEN" \
  http://127.0.0.1:8787/v1/deployments/<deployment-id>/logs
docker ps -a --filter name=gp-cloud-
```

Common causes are a failed Docker build, an application that listens on
`localhost` instead of `0.0.0.0` inside the container, a wrong `app_port`, or a
health path that does not return HTTP 200.

### I need public previews

Do not simply open Docker or control ports. Read [HOSTING.md](HOSTING.md) and
[SECURITY.md](SECURITY.md), configure DNS and TLS at a trusted edge, explicitly
enable Caddy, and test authentication and Stop all before sharing the URL.
