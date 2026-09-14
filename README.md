# HostDealRadar

Site: https://hostdealradar.com

An English-language US hosting, VPS and domain-deal reference built from public official provider pages.

## Editorial rules

Only source-backed prices and terms are shown. When a source fails, is blocked by robots.txt, presents a challenge, or does not expose a reliable term, the site shows no offer. The site currently uses official naked links only; affiliate links remain empty until a program application is approved and disclosure is added.

## Local checks

Run `python validate.py` to build the static site and verify the configuration-driven output. `scraper.py` reads `.ilang/site.ilang`; it checks robots.txt before public source pages and overwrites `data/offers.json` with only results it can support.

## Automation

GitHub Actions checks sources every six hours and commits changed source data and static output. Scheduled Actions can be delayed, and GitHub may disable inactive public-repository schedules; manual dispatch remains available. Cloudflare Pages is connected to the `main` branch. The initial GitHub Actions run will verify that an Actions-created commit triggers a Pages deployment; a Pages deploy hook is the documented fallback if it does not.

## Source of truth

`.ilang/site.ilang` defines the site identity, provider list, official source URLs and extraction rules. `scraper.py` and `build.py` both load it. `AGENTS.md` contains the standing operating rules.
