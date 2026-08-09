# Security model and threat boundaries

## Trust boundaries

Pull-request repositories are untrusted input. The host control service is
trusted. If enabled, Caddy is the only public edge. The default installation
has no public edge and the control API binds to loopback.
Public preview URLs intentionally expose the deployed application.

## Protections

- GitHub repositories are allowlisted (an empty allowlist denies all requests)
  and exact 40-character SHAs are checked. GitHub App credentials do not
  bypass the allowlist.
- Webhooks require HMAC SHA-256 and fork deployments are disabled by default.
- Control mutations require a bearer token or the single-user HttpOnly UI
  session; the public index contains only safe status fields.
- Runtime containers have no Docker socket, host mounts, privilege, or existing
  Docker network access. They receive CPU, memory, PID, read-only-root,
  `no-new-privileges`, and a private internal network.
- Vault values are injected through a temporary `0600` env file and are never
  written to state JSON or returned by the UI.
- The systemd unit uses a strict read/write path boundary and hides host user
  directories from the root-owned control process.
- TTL and pull-request cleanup bound the lifetime of preview workloads.
- The dashboard never returns secret environment values. Vault paths are
  restricted to `gp-cloud/`, and temporary runtime env files are mode 0600 and
  deleted after the container starts.
- The systemd service writes only GP Cloud state, workspace, log, and generated
  route directories. Build source is checked out only below its deployments
  directory.

## Residual risks

Docker builds execute attacker-controlled Dockerfiles through the host Docker
daemon. This is a fundamental risk of a Docker-on-one-host preview runner.
For hostile public multi-tenant use, move builds to isolated VMs or a sandboxed
builder and use a queue with per-tenant quotas. Do not grant this service a
general-purpose root Vault token or unrestricted GitHub token.

## Disclosure checklist

Before opening a public deployment service, verify DNS/TLS, rotate all sample
credentials, set an admin password, configure Vault, allowlist repositories,
set a short TTL, keep workspaces disabled, and test Stop all under load.
