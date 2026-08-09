# GP Cloud configuration and runtime files

The repository intentionally contains only safe configuration templates:

- [`gp-cloud.env.example`](gp-cloud.env.example) documents every supported
  host setting without containing credentials.
- The live host file is `/opt/gp-cloud/config/gp-cloud.env`. It is not copied
  into this source checkout because it contains API credentials and should
  remain mode `0600`.
- Caddy receives only `/opt/gp-cloud/config/caddy.env`, containing the preview
  domain. It must not receive the full secret environment file.
- Runtime environment values for deployed applications belong in HashiCorp
  Vault KV v2, not in this repository or deployment JSON state.
- The dashboard can confirm the live env file path and show redacted variable
  names. It never displays secret values. Use the Vault panel and set a
  deployment's `vault_path` below `gp-cloud/` to make values available inside
  that deployment.
- The local dashboard can edit supported non-secret control settings. Restart
  `gp-cloud.service` after saving them. Keep API tokens, webhook secrets,
  Vault tokens, GitHub credentials, and `GP_CLOUD_ADMIN_PASSWORD` in the local
  mode-0600 env file; they are intentionally not editable or visible in UI.

To inspect the live installation in VS Code, open `/opt/gp-cloud` as a folder
on the deployment VM. Do not commit or share `config/gp-cloud.env`.

Build checkouts are normally removed after each build to protect disk space.
For debugging, set `GP_CLOUD_RETAIN_WORKSPACES=true` in the live host env and
restart `gp-cloud.service`; retained sources appear at
`/opt/gp-cloud/deployments/<slug>/source`. Turn it off afterward and remove
old workspaces through the control UI or cleanup process.
