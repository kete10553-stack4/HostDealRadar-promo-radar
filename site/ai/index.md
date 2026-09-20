# HostDealRadar agent guide

HostDealRadar is a public English-language reference for hosting, VPS, website-builder, and domain-price terms recorded from official provider pages. It is not a hosting provider, checkout service, performance review, or purchasing agent.

## Public read-only record lookup

Use `GET /api/agent/lookup` with exactly one parameter:

- `provider`: an exact provider identifier, such as `namecheap`.
- `slug`: an exact record identifier, such as `raidboxes-mini`.

The response contains only public record fields, the official source URL, the capture time, and the record state. A `404` response means no public record matched the exact identifier. A `400` response means the request was missing an identifier or supplied both identifiers.

## Evidence boundaries

Every record links to the official provider page and keeps its own capture time. `current` means the latest source check reconfirmed that exact record; promotions also require a verified end date. `unverified`, `expired`, `retained`, and `stale` records are reference material, not current offers. Confirm the final checkout total, tax, eligibility, billing term, and renewal total with the provider.

## Useful public pages

- [How HostDealRadar checks sources](/methodology/)
- [Provider records](/providers/)
- [Comparison table](/compare/)
- [API description](/openapi.json)
- [Agent skill](/ai/skills/site-lookup/SKILL.md)
- [Authentication status](/auth.md)
