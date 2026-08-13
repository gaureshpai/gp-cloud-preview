# Security reporting

Do not open a public issue for a vulnerability that could expose credentials,
host access, or another preview. Use GitHub's private vulnerability-reporting
feature for this repository. Include the affected commit, a minimal
reproduction, impact, and any safe mitigation already tested. Never include
real webhook secrets, API tokens, Vault values, private keys, or build logs
containing application data.

The current `main` branch is the supported release line. Security fixes should
include a regression test and an update to `docs/SECURITY.md` when the trust
model changes.
