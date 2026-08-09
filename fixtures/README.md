# Test fixtures

These small Dockerfiles are used for local smoke and failure-path testing.
They do not represent production application templates.

## `smoke-app`

Runs BusyBox HTTPD on container port `2222` and returns HTTP 200 for `/`. Use
it to verify Docker build, startup, port publication, health checks, route
creation, and cleanup.

## `failing-health-app`

Declares port `2222` but intentionally exits immediately. Use it to verify
that a failed startup removes the container and generated route and records a
failed deployment.

Both fixtures are intentionally dependency-free and use only a pinned base
image tag. A real end-to-end run still requires a working Docker daemon.
