# Architecture

```text
GitHub webhook / browser (optional public edge)
          |
 Caddy edge (disabled by default; TLS required for public use)
          |
  control API :8787 (loopback)
       |          |
 sequential     cleanup loop
 worker         TTL / PR close
       |
 Docker build + restricted runtime container (generated static apps listen on :2222)
       |
 Caddy route -> 127.0.0.1:ephemeral-port
```

State is stored as atomic JSON under `/opt/gp-cloud/data/deployments`. Logs live
under `/opt/gp-cloud/logs`. Caddy route files and deployment metadata live
under `/opt/gp-cloud/config/caddy/routes` and `/opt/gp-cloud/deployments`.
Application secrets live in Vault KV v2. The control service only stores a
Vault path reference.

The default installation has no public listener: the API remains on loopback
at `127.0.0.1:8787` and operators use an SSH tunnel. Public hosting is an
explicit operator decision that requires DNS, TLS, firewall, and access-policy
configuration at the edge.

Project-specific behavior is supplied through operator-defined profiles in
settings. Generic repositories may use a Dockerfile, uv, Python requirements,
npm lockfiles, or pnpm lockfiles. Generic Python services must provide
`start_command`; guessing an arbitrary process would make health checks
misleading.
