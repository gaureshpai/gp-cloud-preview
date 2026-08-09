# GP Cloud Preview documentation

- [Hosting for a new operator](HOSTING.md)
- [Local no-VM quickstart](LOCAL-QUICKSTART.md)
- [Architecture and runtime contracts](ARCHITECTURE.md)
- [Operations runbook](OPERATIONS.md)
- [Security model and threat boundaries](SECURITY.md)
- [Contributing and validation](../CONTRIBUTING.md)
- [Configuration and live-file visibility](../config/README.md)

Start with [HOSTING.md](HOSTING.md) for a fresh VM. Use [OPERATIONS.md](OPERATIONS.md)
for the dashboard, cleanup, metrics, and incident commands. Read
[SECURITY.md](SECURITY.md) before exposing a new instance to public GitHub
repositories.

Generated preview containers use port 2222 by default. The installer runs in
local-only mode: Caddy is disabled and the control API listens on loopback.
Use an SSH tunnel for the UI. The Host configuration card edits non-secret
control settings; application secrets and the admin password remain local-only
and are never rendered in the browser.
