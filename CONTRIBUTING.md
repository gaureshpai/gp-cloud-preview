# Contributing

Run the local checks before submitting changes:

```sh
./scripts/check
```

CI installs the pinned Ruff version and ShellCheck, then runs the same script.
Ruff lint/format is configured in `pyproject.toml`; use Ruff 0.12.8 locally.

Keep the control service standard-library-only. Add a function docstring and a
comment for security-sensitive behavior, state transitions, filesystem access,
Vault interactions, and Docker flags. Never commit real env files, Vault
tokens, deployment logs, source workspaces, or generated `__pycache__` files.

When changing an endpoint, update `README.md`, the relevant document in
`docs/`, and a contract test. When changing a deployment lifecycle path, test
both the state JSON and the Docker/Caddy cleanup behavior. The HTTP integration
tests use an ephemeral loopback server and temporary state, so they do not
require a public domain, Vault, GitHub, or a running Caddy edge.
