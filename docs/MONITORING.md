# Optional monitoring

Monitoring is disabled by default. The control API, worker, deploy/stop paths,
reboot recovery, and Caddy do not import, call, or depend on Prometheus. A
Prometheus outage affects dashboards/alerts only.

## What it provides

The optional service scrapes loopback GP Cloud metrics and Prometheus itself
every 15 seconds. It records deployment counts by state, queue depth, GP Cloud
storage/log/artifact bytes, host filesystem totals exposed by the control
service, and running GP Cloud container count. Deployment failures are also
recorded as a durable monotonic counter. Alerts cover low disk, more than
three failure events in one hour, and an unavailable control target.
Historical failed records alone do not keep the alert firing.

No monitoring credential or public endpoint is required. The service binds to
`127.0.0.1:9091`; Caddy never proxies `/metrics` or Prometheus. Port 9091 is
deliberately separate from the distribution Prometheus service's usual 9090.
Grafana and Node Exporter are not installed or claimed by GP Cloud. Operators
may add them separately with loopback bindings and their own lifecycle.

## Enable

Install distribution packages that provide `prometheus`, `promtool`, and the
`prometheus` system user. A distribution-owned Prometheus may continue using
9090; GP Cloud does not modify, stop, or reuse it. Confirm that loopback port
9091 is available, then run either:

```sh
sudo ./scripts/gp-cloud-install --enable-monitoring
```

or from an installed host:

```sh
sudo /opt/gp-cloud/worker/gp-cloud-monitoring enable
```

The command validates config/rules before enabling
`gp-cloud-prometheus.service`. Owned paths are:

```text
/etc/gp-cloud/monitoring/prometheus.yml
/etc/gp-cloud/monitoring/alerts.yml
/etc/systemd/system/gp-cloud-prometheus.service
/var/lib/gp-cloud-prometheus/
```

The base installer also retains the manager and authoritative templates used by
later enable/upgrade operations:

```text
/opt/gp-cloud/worker/gp-cloud-monitoring
/opt/gp-cloud/monitoring/prometheus.yml
/opt/gp-cloud/monitoring/alerts.yml
/opt/gp-cloud/monitoring/gp-cloud-prometheus.service
```

It never overwrites `/etc/prometheus/prometheus.yml`, distribution defaults,
or another Prometheus instance. Re-running enable refreshes only GP Cloud-owned
configuration and is idempotent.

## Health

```sh
sudo /opt/gp-cloud/worker/gp-cloud-monitoring status
curl --fail http://127.0.0.1:8787/metrics
curl --fail http://127.0.0.1:9091/-/ready
sudo journalctl -u gp-cloud-prometheus -f
```

`status` checks the core service, its health endpoint, the optional service,
and Prometheus readiness. A nonzero status diagnoses monitoring; it must not be
used as a reason to stop the core control service.

## Disable and uninstall

```sh
sudo /opt/gp-cloud/worker/gp-cloud-monitoring disable
sudo /opt/gp-cloud/worker/gp-cloud-monitoring uninstall
```

Disable stops/disables only the GP Cloud Prometheus unit and retains config and
time-series data. Uninstall removes only the GP Cloud-owned unit/config and
retains `/var/lib/gp-cloud-prometheus` for recovery. Remove that data or the
Prometheus package separately only after an explicit retention decision. The
manager and templates under `/opt/gp-cloud` are part of the base installation
and remain available for a later re-enable or upgrade.

After either operation, confirm `gp-cloud.service` and deployments still work:

```sh
curl --fail http://127.0.0.1:8787/healthz
sudo systemctl is-active gp-cloud.service
```
