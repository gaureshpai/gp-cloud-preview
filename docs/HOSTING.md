# Hosting GP Cloud Preview

Use a dedicated Debian 12 or Ubuntu 24.04 VM with Python 3.11+, Docker Engine
and its Buildx plugin, Git, curl, systemd, and Caddy. Two vCPUs and 4 GB RAM are
a practical minimum for small previews. Do not mix production or unrelated
tenant workloads with untrusted preview builds.

## Local-only installation

```sh
git clone <your-fork-url> gp-cloud-preview
cd gp-cloud-preview
sudo ./scripts/gp-cloud-install
sudoedit /opt/gp-cloud/config/gp-cloud.env
sudo systemctl restart gp-cloud.service
curl --fail http://127.0.0.1:8787/healthz
```

The default does not touch an existing Prometheus configuration or Caddyfile,
does not enable public listeners, and does not invent secrets. Use
`ssh -L 8787:127.0.0.1:8787 user@host` for dashboard access.

## GitHub setup

Set distinct random API/admin/webhook secrets and an explicit repository
allowlist. For private clones, prefer a narrowly installed GitHub App. Enable
`issue_comment` and `pull_request` webhook events and use JSON content.

Public mode webhook URL:

```text
https://webhook.<preview-domain>/webhooks/github
```

Only an exact `/deploy` comment from a trusted repository owner/member/collaborator
is accepted. Edited/deleted comments, arguments, prose, quotes, code blocks,
case variants, untrusted associations, duplicate delivery IDs, non-PR issues,
forks (by default), and non-allowlisted repositories cannot enqueue work.

GitHub Actions use `https://actions.<preview-domain>` and must keep the control
URL in an Actions secret. They send their token in `Authorization` and the
exact repository in `X-GitHub-Repository`. Candidate responses do not publish
a preview URL before promotion.

## Wildcard DNS and HTTPS

Create an A/AAAA wildcard record such as:

```text
*.preview.example.com -> public VM address
```

Install/build Caddy with the `github.com/caddy-dns/cloudflare` module. Create a
Cloudflare API token restricted to DNS edit/read for only this zone. Configure
`gp-cloud.env`:

```dotenv
GP_CLOUD_PREVIEW_DOMAIN=preview.example.com
GP_CLOUD_PUBLIC_SCHEME=https
GP_CLOUD_HTTP_PORT=443
GP_CLOUD_COOKIE_SECURE=true
```

Configure edge-only `/opt/gp-cloud/config/caddy.env`:

```dotenv
GP_CLOUD_PREVIEW_DOMAIN=preview.example.com
GP_CLOUD_CONTROL_PORT=8787
GP_CLOUD_CADDY_ACME_EMAIL=operator@example.com
GP_CLOUD_CLOUDFLARE_API_TOKEN=<zone-limited-token>
```

Rerun the explicit public installer path:

```sh
sudo ./scripts/gp-cloud-install --enable-public-edge
sudo systemctl is-active caddy
sudo journalctl -u caddy -n 100 --no-pager
```

Caddy is started when it is stopped, or reloaded when it is already running, so
the explicit installer command applies the new configuration immediately. Caddy
obtains and renews one DNS-01 wildcard certificate. Port 80 redirects to
HTTPS; port 443 serves the separated webhook, action, dashboard, and preview
host policies. Keep 8787, 9091, and dynamic Docker host ports firewalled to
loopback.

To stop the GP Cloud edge explicitly:

```sh
sudo ./scripts/gp-cloud-install --disable-public-edge
```

The command refuses to stop an unmarked operator-owned Caddy service. For a
GP Cloud-managed edge it stops/disables Caddy and removes GP Cloud's ownership
marker, but retains `/etc/caddy/Caddyfile`, its environment drop-in,
`/opt/gp-cloud/config/caddy.env`, generated routes, and any timestamped backups
for inspection or re-enable. Restore the desired backup and remove GP Cloud's
retained Caddy files manually before repurposing that service for another use.

Run the read-only smoke test from an external machine after DNS and Caddy are
live:

```sh
./scripts/gp-cloud-tls-smoke preview.example.com
```

Before DNS changes propagate, direct the probes to the intended edge while
preserving HTTP Host and TLS SNI:

```sh
./scripts/gp-cloud-tls-smoke preview.example.com --resolve 203.0.113.10
```

The script requires `curl` and OpenSSL. It verifies the HTTP-to-HTTPS redirect,
the trusted wildcard SAN and at least 14 days of remaining certificate validity,
the three intentionally public ingress paths, and the default-deny matrix for
control, metrics, API, and arbitrary preview hosts. It sends only empty,
unauthenticated webhook/action probes, so it cannot enqueue a deployment. Use
`--min-valid-days DAYS` to change the expiry threshold. Check Caddy renewal logs
regularly because DNS-01 renewal needs continued access to the limited DNS
token. If renewal fails, disable Caddy before expiry rather than serving an
insecure fallback.

## Vault

Use KV v2 and a policy limited to `gp-cloud/`. Put a short-lived token in a
root-owned mode-0600 file under `/opt/gp-cloud/config`; do not use a Vault root
token. Deployment paths cannot escape or imitate the configured prefix.

Project profiles are keyed by canonical `owner/repository`. Legacy project-name
keys apply only to direct deployments; they are not used for pull-request
deployments, preventing same-named repositories from sharing configuration.
PR source is untrusted and receives no Vault values by default. Supplying
preview-only, disposable secrets to a PR requires both
`GP_CLOUD_ALLOW_PR_SECRETS=true` and `allow_pr_secrets: true` in that canonical
repository profile. Keep production credentials out of all preview profiles.

## Build isolation and limits

The host requires `docker buildx`. Each attempt gets a uniquely named
docker-container builder, disables shared build cache reuse, and applies
`GP_CLOUD_BUILD_MEMORY_LIMIT` and `GP_CLOUD_BUILD_CPU_QUOTA`. The builder is
normally removed when the build command exits. Runtime limits remain separately
controlled by `GP_CLOUD_MEMORY_LIMIT` and `GP_CLOUD_CPU_LIMIT`.

Dockerfile `RUN` networking is `none` by default, blocking cloud metadata,
private-network, and internet access during builds. Repositories whose locked
dependencies are not vendored must be explicitly trusted with
`allow_build_network: true` in their canonical profile. PR deployments also
require `GP_CLOUD_ALLOW_PR_BUILD_NETWORK=true`. This double opt-in enables
ordinary Docker egress; leave the host flag off for fork and other hostile PR
source.

Images declaring Docker `VOLUME`s are rejected because anonymous writable
volumes bypass the read-only-root contract and are difficult to bound and
clean reliably. Use the provided bounded `/tmp` tmpfs for transient writes.
These controls reduce collision and resource risk, but the builders still use
the host Docker daemon; use a dedicated VM or stronger rootless/disposable
builder isolation for hostile public contributions.

## Optional monitoring

Prometheus is not a prerequisite. To opt in after installing the `prometheus`
and `promtool` commands and a local `prometheus` user:

```sh
sudo ./scripts/gp-cloud-install --enable-monitoring
sudo /opt/gp-cloud/worker/gp-cloud-monitoring status
```

The dedicated service binds to `127.0.0.1:9091`, avoiding the distribution
service's usual 9090, and owns only its namespaced config/unit/data. It does not
modify `/etc/prometheus/prometheus.yml`. See [MONITORING.md](MONITORING.md).

## Upgrade and rollback

Back up `/opt/gp-cloud/config/gp-cloud.env` and Caddy's limited env, pull with
`--ff-only`, rerun the installer using only the options previously intended,
restart, and run recovery/route checks. State/history/logs/Vault data are
outside the checkout and are retained. The installer backs up a differing
Caddyfile before replacing it in explicit public mode.
