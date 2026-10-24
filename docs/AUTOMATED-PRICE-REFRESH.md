# Automated price refresh

The provider list, official source URLs, and extraction rules live only in `.ilang/site.ilang`.
GitHub Actions runs the same scraper every six hours. A successful job means the
refresh and build completed; its summary separately counts numeric prices,
text-only captures, and retained historical records.

## Reading the result

- `captured_slugs` identifies the exact records read from the official response.
- `retained_slugs` identifies earlier records that were not reconfirmed. Their
  original quotes, currencies and capture dates stay intact.
- `rule_failures` explains missing anchors, required fields, billing evidence,
  and ambiguous table cells. These failures also appear as Actions warnings.
- `no_price_rule` is an explicitly configured source limitation, not a price update.
- A text-only promotion match is counted separately from a numeric price update.

The Actions summary lists every source in that run. `refresh_scope` in
`data/offers.json` records which providers were actually requested during a
targeted refresh. Other providers' records and status timestamps remain intact.

## Prices and billing evidence

An advertised plan price uses `kind: plan_price`. It can document a quoted
introductory rate and its conditions without claiming a promotion's expiry has
been verified. Promotional codes and campaigns still require their existing
validity checks. Every published current price still requires a fresh successful
capture of that exact record.

For an HTML table, `mode: table_cell` binds `row_match` to `column_match` within
one table. Optional `column_attributes` distinguish similarly named plan
variants. Ambiguous headers, merged price cells and conflicting billing units
produce no record. The original row and column evidence is retained; an hourly
column cannot supply a monthly price.

For a card whose billing period is in a `data-on-value` or
`data-billing-period` attribute, the attribute must be on the same matched price
element. The original attribute is retained as source-markup evidence. A price
cannot borrow a billing unit from another card or an unrelated page sentence.

These automated price records do not replace dated manual plan reviews.
The manual comparison requires its own official upfront total, commitment,
Calculated monthly equivalent, and explicit renewal rate. Builds and refreshes
never change those manual review dates.

Provider pages show fresh source prices before dated manual examples. An official
upfront total is displayed with its original quote; a monthly equivalent is
explicitly labelled Calculated and uses that total and its recorded commitment.

For a page that renders prices from a directly referenced public resource,
`SOURCE_RESOURCES` configures the exact same-origin resource URL. The page must
still link that resource on each run; both requests follow robots and the same
provider lock. `parallel_arrays` binds one index in a price array to the same
index in its resource arrays, with explicit currency and billing-unit evidence
from the official formatter. It does not execute vendor JavaScript. Campaign
controls that can modify the base rates prevent capture rather than inventing a
discounted price. The price data and formatter keep their actual resource URL,
source fragments and capture time.

## Targeted diagnostics

Use the existing workflow's manual-run `providers` input with comma-separated
configured identifiers, or run:

```sh
python scraper.py --providers hostinger,kinsta --summary work/refresh-report.md
```

The optional `capture_responses` workflow input saves successful public HTML
responses as an artifact for seven days. The CLI equivalent is
`--capture-dir work/source-responses`. Do not commit those raw responses.
These options use the ordinary source checker and preserve robots restrictions
and persistent HTTP 403 locks. They do not authorize retries of locked sources.

Before rebuilding locally, make a complete worktree backup: `validate.py`
removes and regenerates `site/`. Run extraction regressions with
`python -m unittest test_scraper -q`; the workflow runs these before network
requests, followed by `validate.py` before committing refreshed data.
