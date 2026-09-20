# auth.md — HostDealRadar authentication status

Status: under construction

Authentication is not available. HostDealRadar's public record lookup is available without an account and is read-only. Planned authentication metadata is a contract placeholder only: it does not register users, issue credentials, send email, store identity data, or redirect to an authorization screen.

- status: `under_construction`
- available: `false`
- capabilities_status: `planned_contract_only`
- message: `Coming soon; authentication is not available.`
- launch_date: `null`

Planned endpoints return HTTP 503 with `temporarily_unavailable` until authentication is actually implemented.

## Agent registration (planned)

`agent_auth` metadata is published only to describe the future contract. The planned registration endpoint is `/agent-auth/register`, but agent registration is unavailable: it creates no account, issues no credential, and accepts no identity data. The sole planned registration method is `planned_contract_only`; it is not an available enrollment method. Until a real implementation exists, agents use the public read-only lookup without credentials.
