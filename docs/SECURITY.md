# Security model and threat boundaries

Pull-request source, webhook bodies, dashboard input, and deployed applications
are untrusted. The dedicated VM, root control service, Docker daemon, Caddy,
operator, GitHub installation, Vault policy, and DNS account are trusted.

## Ingress boundaries

The API listens on loopback. Public Caddy mode has no arbitrary-host catch-all:

- `webhook.<domain>` permits only the GitHub webhook POST;
- `actions.<domain>` permits only Action deploy/status paths;
- `control.<domain>` permits only the authenticated UI;
- preview hosts route only to their current application;
- control API, health, metrics, logs, and admin paths remain private.

Wildcard TLS uses Caddy DNS-01 with a zone-limited token stored only in
`/opt/gp-cloud/config/caddy.env` (`root:caddy`, mode 0640). HTTP redirects to
HTTPS. Public mode refuses installation unless secure cookies and the HTTPS
public scheme are configured.

GitHub webhooks require a configured HMAC SHA-256 secret, supported event,
exact action, allowlisted repository, configured installation identity when
used, trusted commenter association, and a unique persisted delivery ID.
HMAC credentials cannot authenticate dashboard/API requests, and dashboard
credentials cannot bypass webhook verification.

Dashboard mutation requests require an authenticated HttpOnly, SameSite=Strict
session, JSON content type, and a matching Origin/Host when browsers send an
Origin. This blocks same-site attacks from compromised preview subdomains. The
edge sends no permissive CORS policy. Keep the dashboard behind an SSH tunnel,
VPN, IP policy, or identity-aware proxy when practical.

## Deployment isolation

Every deployment attempt has a unique workspace, log path, image tag,
container, metadata directory, and internal Docker network. The stable preview
hostname is separate. This prevents concurrent or repeated builds from
overwriting each other's artifacts and ensures a failed replacement cannot
remove the healthy current runtime.

Runtime containers receive:

- no Docker socket, host bind mount, privileged mode, or shared app network;
- a loopback-only dynamically published application port;
- all capabilities dropped and `no-new-privileges`;
- read-only root filesystem and bounded noexec temporary storage;
- CPU, memory/swap, PID, open-file/process, and Docker log limits.

The systemd unit hides host home/media paths and allows writes only to GP Cloud
state, workspace, logs, configuration, routes, and temporary paths.

## Secrets

Repositories are deny-by-default through `GP_CLOUD_ALLOWED_REPOS`; fork PRs are
disabled by default. Full 40-character SHAs are required and verified after
checkout. Vault paths must equal the configured namespace root or begin with
the namespace plus `/`; near-prefix and dot-segment escapes are rejected.

Vault values are never written to deployment JSON or browser responses. A
temporary mode-0600 runtime env file is deleted after container start. Values
remain visible to trusted host root/Docker-daemon access through container
configuration for the runtime's lifetime. Do not use a general Vault root
token or broad GitHub token.

## Residual build risk

Dockerfile build steps have networking disabled by default. This prevents
direct access to cloud metadata, private networks, and internet targets. An
operator can set `allow_build_network: true` only in a canonical repository
profile; PR builds additionally require `GP_CLOUD_ALLOW_PR_BUILD_NETWORK=true`.
That double trust decision restores normal Docker egress.

Builds still run through the host Docker daemon. Unique names, per-job BuildKit
containers and cache, resource limits, bounded wall-clock execution, cleanup,
and a dedicated host reduce operational impact,
but they do not make the build daemon safe for mutually hostile tenants. A
malicious build may consume daemon storage/cache, exploit a daemon/kernel bug,
or access data exposed by the builder.

For public untrusted contribution workloads, use rootless BuildKit with
per-job cache/storage/cgroup/network policy, or disposable build VMs, and pass
only signed artifacts to this runtime host. Do not host unrelated production
workloads on the same VM.

## Pre-public checklist

1. Use a dedicated patched VM and configure firewall ports 80/443 only.
2. Verify wildcard DNS, certificate SAN/expiry/renewal, and HTTP redirects.
3. Set distinct long API, admin, and webhook credentials; limit GitHub/Vault/DNS
   permissions and keep their files mode 0600/0640.
4. Confirm Caddy returns 404 for `/v1`, `/metrics`, `/healthz`, and arbitrary
   hosts, while the webhook host accepts only its exact POST path.
5. Keep fork deployments off unless an isolated builder is available.
6. Set a short TTL, queue/resource limits, and test stop/redeploy/reboot paths.
7. Enable optional monitoring only on loopback if desired.

Report vulnerabilities through the private process in
[`../.github/SECURITY.md`](../.github/SECURITY.md), never a public issue with
credentials or private logs.
