# Operations runbook

## Dashboard

With a secured public edge enabled, open `https://control.<domain>/`. For the
default local-only installation, use an SSH tunnel and open
`http://127.0.0.1:8787/ui`. Sign in with `GP_CLOUD_ADMIN_PASSWORD` (the
API token is a temporary fallback when no UI password is configured). The UI
can create previews, stop one deployment, stop all active deployments, purge
all deployment records/artifacts, edit runtime profiles, and write application
environment values to Vault.

For local-only operation, open the dashboard through an SSH tunnel to
`127.0.0.1:8787`; Caddy is disabled by default and has no public listener.

`Delete all` is irreversible for local JSON, logs, Caddy routes, and runtime
images. Vault secrets are not deleted by that button because they are managed
outside the deployment lifecycle.

## Inspect without leaking secrets

```sh
sudo /opt/gp-cloud/worker/gp-cloud-inspect
sudo systemctl status gp-cloud caddy --no-pager
sudo journalctl -u gp-cloud -f
curl http://127.0.0.1:8787/metrics
```

The inspector redacts every env value. Open `/opt/gp-cloud` separately in VS
Code when host-level inspection is required; do not add that folder to Git.

## Storage and cleanup

The dashboard reports GP Cloud storage, deployment storage, logs, free space,
queue depth, lifecycle states, runtime limits, and per-container CPU, memory,
network, block-I/O, and PID readings. Prometheus exposes aggregate core values.
Keep `GP_CLOUD_RETAIN_WORKSPACES=false` on small VMs. A TTL of 86400
seconds is the default; set it lower for high-volume pull requests.

## Incident response

1. Click **Stop all** if previews are consuming resources. This includes
   queued, building, and running records visible to the control plane.
2. If disk pressure remains, click **Delete all** and inspect Docker images.
3. Check `journalctl -u gp-cloud` and the deployment log.
4. Rotate the API token, webhook secret, GitHub token, and Vault token if any
   credential may have appeared in logs or a shell transcript.
5. Restore the last known-good source and restart the service.
