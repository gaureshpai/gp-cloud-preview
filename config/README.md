# GP Cloud configuration and runtime files

The repository intentionally contains only safe configuration templates:

- [`gp-cloud.env.example`](gp-cloud.env.example) documents every supported
  host setting without containing credentials.
- The live host file is `/opt/gp-cloud/config/gp-cloud.env`. It is not copied
  into this source checkout because it contains API credentials and should
  remain mode `0600`.
- When public mode is explicitly enabled, Caddy receives only
  `/opt/gp-cloud/config/caddy.env`: preview domain/control port, ACME email,
  and the zone-limited DNS-01 token. It must not receive the full control or
  application environment file. The file is `root:caddy` mode `0640`.
- Runtime environment values for deployed applications belong in HashiCorp
  Vault KV v2, not in this repository or deployment JSON state.
- Project profiles should use canonical `owner/repository` keys. A legacy
  project-name key applies only to direct deployments and is never selected for
  a pull-request deployment from another same-named repository.
- PR deployments receive no Vault values by default. Enabling them requires
  both host-level `GP_CLOUD_ALLOW_PR_SECRETS=true` and repo-profile
  `allow_pr_secrets: true`; use only narrowly scoped, disposable preview
  credentials.

  ```json
  {
    "projects": {
      "owner/repository": {
        "vault_path": "gp-cloud/previews/owner-repository",
        "allow_pr_secrets": true,
        "allow_build_network": false
      }
    }
  }
  ```
- The dashboard can confirm the live env file path and show redacted variable
  names. It never displays secret values. Use the Vault panel and set a
  deployment's `vault_path` below `gp-cloud/` to make values available inside
  that deployment.
- The local dashboard can edit supported non-secret control settings. Restart
  `gp-cloud.service` after saving them. Keep API tokens, webhook secrets,
  Vault tokens, GitHub credentials, and `GP_CLOUD_ADMIN_PASSWORD` in the local
  mode-0600 env file; they are intentionally not editable or visible in UI.
- Docker Buildx is required. Build containers use
  `GP_CLOUD_BUILD_MEMORY_LIMIT` and `GP_CLOUD_BUILD_CPU_QUOTA`; runtime
  containers use the separate `GP_CLOUD_MEMORY_LIMIT` and
  `GP_CLOUD_CPU_LIMIT`. Images declaring `VOLUME` are rejected.
- Dockerfile build steps have no network by default. A trusted canonical
  repository profile may set `allow_build_network: true` when locked
  dependencies must be downloaded. PR builds additionally require
  `GP_CLOUD_ALLOW_PR_BUILD_NETWORK=true`. This restores ordinary Docker egress,
  so keep the host flag false for hostile PR source.

To inspect the live installation in VS Code, open `/opt/gp-cloud` as a folder
on the deployment VM. Do not commit or share `config/gp-cloud.env`.

Build checkouts are normally removed after each build to protect disk space.
For debugging, set `GP_CLOUD_RETAIN_WORKSPACES=true` in the live host env and
restart `gp-cloud.service`; retained sources appear at
`/opt/gp-cloud/deployments/<runtime-slug>/source`. Turn it off afterward and
remove old workspaces through the control UI or cleanup process.
