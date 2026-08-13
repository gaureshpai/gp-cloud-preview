# Architecture

```text
GitHub webhook     operator browser       GitHub Action
 webhook.<domain>  control.<domain>       actions.<domain>
        \                 |                    /
         +-------- Caddy HTTPS wildcard edge -+
                            |
                   loopback control API
                            |
                  durable operation queue
                            |
                 one lifecycle-owner worker
                    /               \
       candidate build/runtime     TTL/close/stop cleanup
                    |
       stable preview route -> loopback container port
```

The default installation omits the edge and monitoring. The control API always
binds to `127.0.0.1`; Caddy and Prometheus are independent optional services.

## Domain records

Preview records live in `data/previews`. A preview is keyed by canonical
repository plus pull-request number, or repository plus direct project name.
It stores a stable ID/slug, all deployment IDs, and the authoritative
`current_deployment_id`.

Deployment records live in `data/deployments`. Each immutable attempt stores
its preview relationship, exact SHA, unique runtime slug, lifecycle state,
timestamps, current/superseded fields, and only references to secrets. The API
derives `current` from the preview pointer so history cannot expose two current
attempts.

Operation marker files live in `data/queue`. A request is durable before it is
woken in memory. The worker is the only component that invokes build, route,
Docker, and cleanup side effects. Duplicate stop requests coalesce through the
same idempotent cleanup path.

## Redeploy transaction

1. Create a candidate deployment with a unique workspace, log directory,
   image, container, and internal Docker network.
2. Leave the current container and route unchanged while cloning, building,
   starting, and health-checking the candidate.
3. Write the candidate promotion phase and atomically replace the stable route.
4. Validate/reload Caddy when active; restore the prior route on failure.
5. Commit the preview pointer, mark the candidate current, and mark the former
   current deployment `SUPERSEDED` with replacement/timestamp fields.
6. Clean only the superseded runtime while retaining the new stable route.

Candidate failure before step 5 leaves the former current record, container,
and route intact. PR status comments only show the authoritative current URL.

## Restart reconciliation

On service start, legacy flat records are migrated without deletion. Preview
relationships/current pointers are rebuilt, expired worker leases are cleared,
and durable work is reconstructed from state:

- `QUEUED` is enqueued;
- interrupted `BUILDING` becomes `QUEUED` with recovery metadata and is safely
  retried from its immutable SHA/runtime identity;
- `stop_requested` resumes cleanup;
- a `RUNNING` current deployment is preserved;
- duplicate legacy `RUNNING` records converge on one newest/persisted current,
  with older runtimes superseded and cleaned;
- terminal `FAILED`, `STOPPED`, and `SUPERSEDED` records are not resurrected.

Docker uses `--restart unless-stopped`, so a valid current runtime is preserved
across VM reboot. The service uses `Restart=always`; durable reconciliation
does not depend on an open terminal.

## Build profiles

Repositories can commit a Dockerfile or use an operator profile for uv, Python
requirements, npm, pnpm, Vite, or static Next output. Generated build files
exist only in the candidate workspace. Generic Python services require an
explicit `start_command`; the control plane does not guess processes.
