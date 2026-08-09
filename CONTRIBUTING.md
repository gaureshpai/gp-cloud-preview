# Contributing

Run the local checks before submitting changes:

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile control/gp_cloud_control.py
bash -n scripts/*
uvx ruff format --check control tests
```

Ruff formatting is configured in `pyproject.toml`. Run
`uvx ruff format control tests` after changing Python code; the formatter does
not modify shell scripts, Dockerfiles, or Markdown.

Keep the control service standard-library-only. Add a function docstring and a
comment for security-sensitive behavior, state transitions, filesystem access,
Vault interactions, and Docker flags. Never commit real env files, Vault
tokens, deployment logs, source workspaces, or generated `__pycache__` files.

When changing an endpoint, update `README.md`, the relevant document in
`docs/`, and a contract test. When changing a deployment lifecycle path, test
both the state JSON and the Docker/Caddy cleanup behavior. The HTTP integration
tests use an ephemeral loopback server and temporary state, so they do not
require a public domain, Vault, GitHub, or a running Caddy edge.
