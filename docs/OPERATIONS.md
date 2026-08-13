# Operations runbook

## Health and inspection

```sh
curl --fail http://127.0.0.1:8787/healthz
sudo /opt/gp-cloud/worker/gp-cloud-inspect
sudo systemctl status gp-cloud caddy --no-pager
sudo journalctl -u gp-cloud -f
```

Use an SSH tunnel for the default local dashboard. `GET /metrics`, detailed
deployment status, logs, and the bearer-token API remain loopback-only even
when public Caddy mode is enabled.

The dashboard lists full deployment history. `current=true` identifies the
single deployment addressed by the stable preview URL; `SUPERSEDED` attempts
remain available for audit until purged.

## Deterministic lifecycle rules

- Repeated deploy requests create historical candidates in request order.
- A candidate never changes the current route until it is healthy.
- Successful promotion supersedes and cleans the former current deployment.
- Failed build, health check, or Caddy promotion preserves the former current.
- A stop request sets durable desired state and returns 202. Cleanup completes
  asynchronously in the sole lifecycle worker.
- Repeated stop is idempotent. Stop-before-build cancels without building;
  stop-during-build is observed before promotion and cleans the candidate;
  stop-after-running removes the current route/runtime.
- PR-close and TTL cleanup use the same durable stop path.

`Delete all` first requests active stops. Run it again after work reaches a
terminal state to delete JSON, logs, metadata, and owned runtime artifacts.
Vault data is never deleted by deployment purge.

## Reboot and crash recovery

The service is `Restart=always`. On startup it migrates legacy records, clears
abandoned worker leases, rebuilds preview pointers, and reconstructs durable
operations. Interrupted candidates are retried from the exact SHA and their
deployment-specific identity. Stop requests resume. Valid current runtimes are
preserved and terminal work is never resurrected.

Representative recovery check:

```sh
sudo systemctl restart gp-cloud.service
curl -H "Authorization: Bearer $GP_CLOUD_LOCAL_TOKEN" \
  http://127.0.0.1:8787/v1/deployments
docker ps --filter name=gp-cloud-
sudo find /opt/gp-cloud/data/queue -maxdepth 1 -type f -print
```

After reboot, verify exactly one `current=true` deployment per preview and no
duplicate container/runtime slug. A `BUILDING` record with `recovered_at` was
requeued after interruption. A persistent failure remains `FAILED`; recovery
does not replace a healthy current with a failed candidate.

## Storage and resource pressure

Keep `GP_CLOUD_RETAIN_WORKSPACES=false`. The dashboard reports GP Cloud-owned
storage, Docker container usage, state counts, and queue depth. Defaults bound
the queue, runtime CPU/memory/PIDs/ulimits, local Docker logs, build time, and
preview TTL. Docker build cache/storage still requires host-level quotas and
monitoring on untrusted workloads.

## Monitoring

Monitoring is optional and independent. Use:

```sh
sudo /opt/gp-cloud/worker/gp-cloud-monitoring status
sudo /opt/gp-cloud/worker/gp-cloud-monitoring disable
sudo /opt/gp-cloud/worker/gp-cloud-monitoring uninstall
```

A monitoring failure must not stop deployments or the control service. See
[MONITORING.md](MONITORING.md).

## Incident response

1. Request **Stop all** and watch state converge to `STOPPED`.
2. Remove GP Cloud public ingress with `sudo ./scripts/gp-cloud-install --disable-public-edge`.
3. Inspect redacted inventory, GP Cloud journal, per-deployment logs, queue
   markers, containers, and networks.
4. Rotate API/admin/webhook, GitHub, Vault, and DNS credentials if exposure is
   possible. Remove stale webhook delivery files only after disabling ingress.
5. Patch/reinstall, restart, verify route boundaries and recovery, then restore
   public access.

Never paste application secrets, full env files, private build logs, or Caddy's
DNS token into an issue.
