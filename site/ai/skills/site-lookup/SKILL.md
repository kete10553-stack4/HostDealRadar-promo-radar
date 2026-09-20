---
name: site-lookup
description: Retrieve public HostDealRadar records by exact provider identifier or record slug.
---

# HostDealRadar public record lookup

Use this skill to retrieve published hosting, VPS, website-builder, or domain-price records from HostDealRadar. This service is read-only and does not fetch third-party URLs, test checkout, or make purchases.

## Endpoint

`GET https://hostdealradar.com/api/agent/lookup`

Supply exactly one query parameter:

- `provider`: exact provider identifier, for example `namecheap`.
- `slug`: exact published record ID, for example `raidboxes-mini`.

## Output and limits

The response includes public prices when published, currency, billing period, renewal price when supported, official source URL, capture time, and record state. A record state other than `current` must not be presented as a current offer. Confirm checkout total, tax, eligibility, billing term, and renewal total with the provider. Do not infer a price, discount, availability, coupon, or expiry date that the response does not carry.

## Errors

- `400 invalid_query`: supply exactly one supported parameter.
- `404 not_found`: no public record matched the supplied exact identifier.
- `405 method_not_allowed`: use `GET` only.
