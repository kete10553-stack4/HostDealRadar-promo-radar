# HostDealRadar engineering handoff

**Prepared:** 2026-09-11 (America/Chicago)  
**Status:** public site is live; the expansion work is researched but is not merged or deployed.

## Identity and current baseline

| Item | Value |
| --- | --- |
| Repository | https://github.com/kete10553-stack4/HostDealRadar-promo-radar |
| Local checkout | `C:\\Users\\Administrator\\Documents\\Codex\\2026-09-10\\agent-powershell-openssh-1-3-powershell\\outputs\\HostDealRadar-promo-radar` |
| Branch | `main` |
| Checked baseline commit | `a46f2bb1f1b237c9c01bd3d22c778511c0c8f35b` — `Refresh official source snapshot` |
| Public site | https://hostdealradar-promo-radar.pages.dev/ |
| Hosting | Cloudflare Pages, Git integration from `main` |
| Pages build command / output | `python build.py` / `site` (recorded configuration; recheck in Cloudflare before changing it) |

The live site had HTTP 200 responses for its home page, `/robots.txt`, and `/sitemap.xml` when this handoff was written.

## Authority and data flow

`.ilang/site.ilang` is the only source for the brand, provider list, source URLs, and extraction rules. `config.py`, `scraper.py`, and `build.py` read it; do not make a second provider list in Python, a workflow, or a spreadsheet.

1. `python scraper.py` fetches only public official sources after checking `robots.txt`, and writes `data/offers.json`.
2. `python build.py` renders `site/` from the configuration and that data.
3. `python validate.py` validates and also rebuilds `site/`. It is **not read-only**: it removes and regenerates the configured output directory.
4. GitHub Actions commits changed `data/offers.json` and `site/`; Cloudflare Pages receives the Git push and deploys it.

Use the bundled Python currently available on this machine:

```powershell
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe scraper.py
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe validate.py
```

Run mutable checks only in an isolated clone/worktree or after a complete backup of the working tree. Never run `validate.py` merely to inspect it.

## Automation and deployment

Workflow: `.github/workflows/refresh.yml` (name: `Refresh official source snapshot`). It runs at minute 17 of 00:00, 06:00, 12:00, and 18:00 UTC, plus `workflow_dispatch`. Scheduled jobs can be delayed by GitHub; the cron expression alone is not proof of a successful refresh.

The workflow uses its per-run `GITHUB_TOKEN` with `contents: write`, runs the scraper and validator, then commits and pushes only if `data/offers.json` or `site/` changed. The source code does not show a Cloudflare credential in that workflow.

One verified historical scheduled run is [run 34599764304](https://github.com/kete10553-stack4/HostDealRadar-promo-radar/actions/runs/34599764304), which completed successfully and produced the baseline commit above. This alone does **not** prove every scheduled execution, Pages deployment, or public page update has succeeded.

For each production acceptance, retain all four links or records:

1. the specific Actions run, with its green completion status;
2. the commit produced by that run;
3. the Cloudflare Pages deployment record that names that commit; and
4. the public URL showing the expected data and capture time.

If the Git push does not trigger Pages, create and store a Pages deploy-hook secret in GitHub Actions Secrets, then call it after the push. Do not put it in the repository or a chat message.

## Current published data

The baseline `data/offers.json` contains 5 providers and 10 captured listings: Hostinger (2), SiteGround (3), DreamHost (3), Kinsta (1), and Cloudways (1). Links are official bare links; no affiliate relationship is active. Kinsta is represented as “First month free on select plans,” not as a $0 monthly price.

The Cloudways record has an explicit historical `valid_until` value of 2026-09-15. It must not be shown as a current promotion after that date.

## Uncommitted work: preserve; do not publish as-is

At handoff time, these files are modified or untracked:

| File | Purpose / state |
| --- | --- |
| `scraper.py` | Adds bounded extraction, robots parsing, redirect checks, raw checks, and retain-on-failure behavior. Needs review before release. |
| `build.py` | Adds capture/staleness display and provider pages. It has known stale/expired rendering defects below. |
| `validate.py` | Adds configuration/output/link validation. It rebuilds the output directory. |
| `templates/index.html` | Copy/count wording update. |
| `test_scraper.py` | New unit tests for extraction boundaries, robots precedence, invalid prices, and retention. |

A recovery copy was made before this handoff at:

`C:\\Users\\Administrator\\Documents\\Codex\\2026-09-10\\handoff-backup-20260911-1700`

It contains `base-commit.txt`, `status.txt`, `uncommitted.patch`, and direct copies of the dirty/untracked files. SHA-256 for `uncommitted.patch` is `8AFBEB49C8478EA67BB1EE0319F139532D27E330E7A2D2C9C65F1A4DECCE583B`.

Before any integration, create a separate worktree or clone from the baseline, apply/review the patch there, run focused unit tests, then run the scraper and validator only in that isolated directory. One person should own the final merge and production deployment.

## Known defects and required fixes before release

1. A provider may be marked `checked` while a particular rule failed and an older record was retained. The rendering state must be tracked per record (for example captured vs retained slugs), not only per provider.
2. Stale or expired records currently can still appear as normal `Offer` prices in JSON-LD and on the comparison page. Do not emit current-price structured data for those records; visibly label them with their actual capture time, or omit them from current comparisons.
3. The comparison view currently hard-codes a renewal display as USD/month in one path. It must use the record currency and billing period.
4. Sitemap `lastmod` currently derives from source capture timestamps. `lastmod` must change only when the rendered page itself materially changes. Preserve the last rendered content hash/date, or omit `lastmod` until that is implemented.
5. Methodology copy says blocked/expired sources produce no offer, while retain-on-failure code keeps previously sourced records. The published wording must describe the actual behavior.

These are release blockers for the uncommitted patch. They do not establish that the currently deployed baseline is broken.

## Seed-expansion research awaiting independent live verification

No new provider rules have been merged. Research evidence lives outside the repository under `_research/` in the shared workspace and must be independently re-fetched from official pages at integration time.

| Research set | Candidates with tested rules | Specific excluded / blocked candidates |
| --- | ---: | --- |
| Managed hosting | 8 providers, 15 rules | Namecheap `/promos/`: official page returned HTTP 403 after permitted robots check; HostGator: `robots.txt` returned HTTP 403, so its coupon page was not fetched. |
| Cloud, domains, site builders | 10 providers, 25 rules | No claimed exclusions in that package; OVH’s annual-selection interpretation needs a fresh review before publishing a 12-month claim. |
| Site builders | 5 providers, 7 rules | SITE123: no numeric price bound to Premium; Jimdo: no paid-plan price in the response; Weebly: USD evidence applies to domain registration rather than hosting plans; Tilda: dollar symbol without an official USD confirmation; Durable: conflicting card/table prices and ambiguous billing binding. |

For every provider, record the official source URL, exact check time, HTTP/robots result, bounded selector or text structure, and the precise failure reason. Do not reuse research prices or timestamps as a new live capture. A provider belongs in `.ilang/site.ilang` only when a current official response supplies every field that will be displayed.

## Credentials and access

No secret values belong in this document, the repository, terminal output, screenshots, or chat.

| System | Observed/required use | Approved storage and scope | Expiry / rotation |
| --- | --- | --- | --- |
| GitHub Actions | Scheduled checkout and commit/push | Per-run dynamic `GITHUB_TOKEN`; `contents: write` in workflow | Generated per job. No evidence ties it to a 2026-10-11 expiry. |
| Local GitHub publishing | A local Git credential was usable for `git push` during prior work | Windows Credential Manager/Git Credential Manager; repository-scoped access should be used | Inspect the credential metadata before assuming an expiry. Rotate/revoke when the maintainer changes. |
| Cloudflare Pages Git deployment | Git integration deploys on push | Cloudflare’s GitHub App authorization; no local Cloudflare token required by the existing workflow | Recheck App authorization if Pages stops receiving pushes. |
| Cloudflare API / DNS | Needed only for API management, custom-domain DNS, or Pages API actions | Store a least-privilege token outside the repository, in credential storage or a protected secret | The reported 2026-10-11 expiry is unverified: identify the token and its consumer before rotating it. |
| Google Search Console | Ownership verification and sitemap submission | Google account with the site permission; DNS TXT can be added through authorized DNS access | Not configured/verified in this repository; Cloudflare credentials do not replace Google access. |

The repository `.gitignore` excludes `.env*`, `*.pem`, `*.dpapi`, and `work/`. Keep it that way; verify with `git status` before every commit.

## Safe release and rollback

Release only after an isolated worktree passes the intended tests, a manual Actions run has produced a commit, Pages has deployed that commit, and the public site matches the data. Keep the action/commit/deployment/public links in the release note.

To roll back a bad production build, identify the previously verified commit, revert the bad commit on `main` (or make a corrective commit), push it, and verify the four acceptance records again. Do not use `reset --hard` on the shared working directory, and do not delete the backup before a verified release.
