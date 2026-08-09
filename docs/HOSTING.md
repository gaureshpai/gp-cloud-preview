# Hosting GP Cloud Preview for yourself or the community

GP Cloud Preview is a host-level preview service, not a SaaS control plane. A
single Linux VM runs the API, worker, Caddy, and optional monitoring. Anyone
can fork the repository and host an isolated instance for their own projects.

## Minimum host

- Debian 12 or Ubuntu 24.04
- A dedicated VM; a public IPv4 address is needed only for public hosting
- Docker Engine, Git, curl, Caddy, and systemd
- 2 vCPU / 4 GB RAM for small static sites; size larger hosts for heavier builds
- Local-only mode needs no public application port; 8787, 2222, 3000, and
  deployment host ports remain loopback-only

## Install

```sh
git clone <your-fork-url> gp-cloud-preview
cd gp-cloud-preview
sudo ./scripts/gp-cloud-install
sudoedit /opt/gp-cloud/config/gp-cloud.env
sudo systemctl restart gp-cloud.service
```

For public hosting, set a preview domain and DNS wildcard, configure TLS at an
edge proxy, generate API/webhook credentials, and explicitly allowlist
repositories. Use a dedicated GitHub App installation or a narrowly scoped
token for private repositories; credentials never replace the allowlist.

To host this for other people, first configure a secured public edge, then give
them the HTTPS control URL (`https://control.<domain>`) and a separate UI
password. Keep 8787, 9090, and application host ports loopback-only; public
traffic should enter through the edge. The dashboard provides Stop all and
Delete all controls for emergency shutdown and cleanup.

The installer defaults to local-only mode and disables Caddy. The bundled
Caddyfile listens for plain HTTP only, so public operators must place TLS at a
trusted proxy or replace the edge configuration before exposing it. To inspect the
dashboard remotely without exposing it, use `ssh -L 8787:127.0.0.1:8787
user@host` and open `http://127.0.0.1:8787/ui` locally.

## Vault setup

Use Vault KV v2. Create a policy that permits the control plane to read/write
only the project prefix it owns, then provide a short-lived token through a
root-owned token file:

```sh
sudo install -o root -g root -m 0600 /dev/null /opt/gp-cloud/config/vault.token
sudoedit /opt/gp-cloud/config/vault.token
```

```dotenv
GP_CLOUD_VAULT_ADDR=https://vault.example.com
GP_CLOUD_VAULT_TOKEN_FILE=/opt/gp-cloud/config/vault.token
GP_CLOUD_VAULT_MOUNT=secret
```

Restart the service. The dashboard should report Vault as configured. Values
entered in the Vault panel are never written to deployment JSON or logs.
Secret values are intentionally not displayed in the dashboard. The Host
configuration card shows availability and variable names; Vault injects the
actual values into the selected application at startup. Deployment Vault paths
must remain below `gp-cloud/`.

## Application port

Generated static profiles listen on container port `2222`. When the public edge
is enabled, Caddy routes each preview hostname to a dynamically allocated
loopback host port. Use HTTPS for public hosting. The control dashboard is not
directly exposed by the control service; it remains on loopback unless the
operator deliberately publishes it through the edge.
Custom Dockerfiles may use another port when `app_port` is provided, but the
application must listen on that port.

## GitHub integration

Configure a webhook at `/webhooks/github` with the exact HMAC secret and enable
`issue_comment` and `pull_request`. The repository must be in
`GP_CLOUD_ALLOWED_REPOS`; GitHub App credentials only authorize cloning and
installation identity. `/deploy` on an allowed pull request starts a preview.
Closing or merging the pull request stops it. The TTL cleanup loop also stops
jobs that outlive their configured lifetime.

For a GitHub Actions workflow, store the secured control URL as an Actions
secret and expose it to the step as an environment variable. Do not hardcode a
private tunnel URL or credential in the workflow:

```yaml
env:
  GP_CLOUD_CONTROL_URL: ${{ secrets.GP_CLOUD_CONTROL_URL }}
```

The workflow must send `POST /actions/gp-cloud-deploy` with the GitHub token in
the `Authorization: Bearer` header and `X-GitHub-Repository` set to the exact
`owner/repository`. The control URL must be reachable by GitHub-hosted runners
over HTTPS; a loopback address or an expired temporary tunnel cannot work.

## Production use

Use GP Cloud Preview for staging, review apps, demos, and open-source examples.
For a production site, put a separate production deployment behind its own
backup, database, observability, and rollback process. The preview worker is
single-concurrency by design and is not a replacement for a multi-node
orchestrator.

## Upgrade and rollback

```sh
sudo cp -a /opt/gp-cloud/config/gp-cloud.env /opt/gp-cloud/config/gp-cloud.env.backup
git pull --ff-only
sudo ./scripts/gp-cloud-install
sudo systemctl restart gp-cloud.service
sudo journalctl -u gp-cloud.service -n 100 --no-pager
```

The installer backs up an existing Caddyfile. Deployment JSON, logs, and
Vault data are kept outside the source checkout.
