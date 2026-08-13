# Local no-VM quickstart

This is the safest way to evaluate GP Cloud Preview on a Linux machine. It
requires systemd, root/sudo, Docker Engine, Git, curl, and Python 3.11+
(native Debian 12 and Ubuntu 24.04 are supported). macOS/Windows need a Linux
VM or WSL2 environment with systemd and Docker.

Caddy is required only for the optional public HTTPS edge described in
[HOSTING.md](HOSTING.md).

## Install and configure

```sh
sudo ./scripts/gp-cloud-install
sudoedit /opt/gp-cloud/config/gp-cloud.env
```

Set at least distinct random credentials and a repository allowlist:

```dotenv
GP_CLOUD_PREVIEW_DOMAIN=preview.example.com
GP_CLOUD_API_TOKEN=<openssl-rand-hex-32>
GP_CLOUD_ADMIN_PASSWORD=<different-random-value>
GP_CLOUD_GITHUB_WEBHOOK_SECRET=<another-random-value>
GP_CLOUD_ALLOWED_REPOS=owner/example-repository
GP_CLOUD_PUBLIC_SCHEME=http
GP_CLOUD_HTTP_PORT=80
GP_CLOUD_COOKIE_SECURE=false
GP_CLOUD_RETAIN_WORKSPACES=false
```

The HTTP/local values are for loopback access only. Public mode requires the
HTTPS/secure-cookie values in [HOSTING.md](HOSTING.md).

```sh
sudo systemctl restart gp-cloud.service
curl --fail http://127.0.0.1:8787/healthz
```

GP Cloud does not configure/enable Caddy, and monitoring remains disabled.
An existing operator-owned Caddy service is left untouched. Open
`http://127.0.0.1:8787/ui` and sign in with the admin password.

## Deploy

The repository must be allowlisted and the SHA must contain all 40 hexadecimal
characters. A committed Dockerfile should listen on the configured application
port (2222 by default) and return HTTP 200 at the health path.

```sh
export GP_CLOUD_LOCAL_TOKEN='<api-token>'
curl -X POST http://127.0.0.1:8787/v1/deployments \
  -H "Authorization: Bearer $GP_CLOUD_LOCAL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_url":"https://github.com/owner/example-repository.git",
    "sha":"<40-character-sha>",
    "project":"example-repository",
    "app_port":2222,
    "health_path":"/"
  }'
```

Poll the returned ID:

```sh
curl -H "Authorization: Bearer $GP_CLOUD_LOCAL_TOKEN" \
  http://127.0.0.1:8787/v1/deployments/<id>
curl -H "Authorization: Bearer $GP_CLOUD_LOCAL_TOKEN" \
  http://127.0.0.1:8787/v1/deployments/<id>/logs
```

The stable preview URL is intentionally unreachable without the GP Cloud edge,
but build, health, history, stop, cleanup, and recovery behavior can be tested.
Repeated deployment to the same project/PR creates unique candidates and one
current pointer; failed candidates do not remove the prior current runtime.

## Stop and reboot checks

Stop requests return before serialized cleanup finishes. Poll until `STOPPED`:

```sh
curl -X POST -H "Authorization: Bearer $GP_CLOUD_LOCAL_TOKEN" \
  http://127.0.0.1:8787/v1/deployments/<id>/stop
sudo systemctl restart gp-cloud.service
docker ps --filter name=gp-cloud-
```

After restart, queued/interrupted work resumes, requested stops converge, and
valid current deployments remain current without duplicates. See
[OPERATIONS.md](OPERATIONS.md) for the state matrix.

## GitHub, Vault, and monitoring

GitHub cannot call loopback. Use the dashboard/API locally, or configure the
secured wildcard edge before adding webhooks. The only supported comment
command is a standalone lowercase `/deploy` from a trusted owner, member, or collaborator.

Vault is optional when an app needs no secrets. If enabled, give the service a
KV v2 policy limited to `gp-cloud/` and reference a project path; never put app
secrets in the repository or host template.

Monitoring is also optional and does not affect deployment health. Follow
[MONITORING.md](MONITORING.md) to enable, inspect, disable, or uninstall it.

## Troubleshooting

```sh
sudo systemctl status gp-cloud.service --no-pager
sudo journalctl -u gp-cloud.service -n 100 --no-pager
sudo /opt/gp-cloud/worker/gp-cloud-inspect
docker ps -a --filter name=gp-cloud-
```

Common failures are a repository absent from the exact allowlist, short SHA,
Dockerfile build error, wrong container port, application bound only to
localhost inside the container, failing health path, exhausted queue/storage,
or missing clone permission. Logs are per deployment attempt and runtime
resources use the deployment-specific slug, so concurrent attempts do not
overwrite one another.
