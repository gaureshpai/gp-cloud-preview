# Test fixtures

These small Dockerfiles are used for local smoke and failure-path testing.
They do not represent production application templates.

## `smoke-app`

Runs BusyBox HTTPD on container port `2222` and returns HTTP 200 for `/`. Use
it to verify Docker build, startup, port publication, health checks, candidate
promotion, and cleanup.

## `failing-health-app`

Declares port `2222` but intentionally exits immediately. Use it to verify
that a failed candidate removes only its deployment-specific runtime, records
failure, and leaves any healthy current preview route unchanged.

Both fixtures are intentionally dependency-free and use only a pinned base
image tag. A real end-to-end run still requires a working Docker daemon.
