"""Fetch every batch-3 provider live and run each new rule through offer_from_rule.

Prints the value each rule would publish so it can be compared with the page.
Nothing is written.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import scraper  # noqa: E402
from _add3 import PROVIDERS, RULES, build_rule  # noqa: E402

EXPECTED = {
    ('shopify', 'Basic'): 29, ('shopify', 'Grow'): 79, ('shopify', 'Advanced'): 299,
    ('bigcommerce', 'Core'): 39, ('bigcommerce', 'Growth'): 105, ('bigcommerce', 'Scale'): 399,
    ('duda', 'Basic'): 25, ('duda', 'Team'): 39, ('duda', 'Agency'): 69, ('duda', 'White Label'): 199,
    ('framer', 'Basic'): 10, ('framer', 'Pro'): 30,
    ('ghost', 'Starter'): 18, ('ghost', 'Publisher'): 29, ('ghost', 'Business'): 199,
    ('tilda', 'Personal'): 10, ('tilda', 'Business'): 20,
    ('accuweb-hosting', 'Budget'): 1.99, ('accuweb-hosting', 'Bootstrap ++'): 2.79,
    ('accuweb-hosting', 'Premium ++'): 3.99,
    ('kamatera', 'Basic'): 4, ('kamatera', 'Standard'): 25, ('kamatera', 'Pro'): 39,
    ('gridpane', 'PeakFreq'): 19, ('gridpane', 'Bespoke Hosting'): 2000,
    ('koyeb', 'Pro'): 29, ('koyeb', 'Scale'): 299,
    ('flywheel', 'Starter'): 25, ('flywheel', 'Freelance'): 96, ('flywheel', 'Agency'): 242,
    ('10web', 'AI Starter'): 10, ('10web', 'AI Premium'): 15, ('10web', 'AI Ultimate'): 22.5,
    ('10web', 'Agency starter'): 42.5, ('10web', 'Agency core'): 80,
    ('webflow', 'Core'): 19, ('webflow', 'Plus'): 74, ('webflow', 'Optimize'): 299,
    ('gandi', '.com registration'): 11.0, ('gandi', '.fr registration'): 6.0,
    ('fasthosts', 'Start'): 1.0, ('fasthosts', 'Scale'): 1.0, ('fasthosts', 'Pro'): 1.0,
    ('name-com', '.com registration'): 12.99, ('name-com', '.net registration'): 16.49,
    ('name-com', '.org registration'): 8.49,
    ('pressable', 'Signature 1'): 250, ('pressable', 'Signature 3'): 600,
    ('pressable', 'Signature 5'): 1550, ('pressable', 'Signature 8'): 6750,
    ('railway', 'Hobby'): 5, ('railway', 'Pro'): 20,
    ('wordpress-com', 'Personal'): 9, ('wordpress-com', 'Premium'): 18, ('wordpress-com', 'Business'): 40,
    ('heroku', 'Eco'): 5, ('heroku', 'Basic'): 7, ('heroku', 'Standard-1X'): 25,
    ('heroku', 'Standard-2X'): 50,
    ('krystal', 'Amethyst'): 7, ('krystal', 'Ruby'): 11, ('krystal', 'Emerald'): 19,
    ('krystal', 'Sapphire'): 37, ('krystal', 'Diamond'): 67, ('krystal', 'Tanzanite'): 97,
    ('scaleway', 'Start'): 4.99,
    ('site123', 'Custom domain plan'): 10.8,
    ('a2-hosting', '.com registration'): 11.99, ('a2-hosting', '.org registration'): 15.99,
    ('a2-hosting', '.online registration'): 2.0,
}

cfg = scraper.load_config()
by_id = {pid: (name, source) for pid, name, _w, source, _n in PROVIDERS}
fetched = {}
problems = 0
for pid, (name, source) in by_id.items():
    try:
        status, raw = scraper.fetch_source(source, cfg)
        fetched[pid] = raw
    except Exception as exc:  # noqa: BLE001
        print(f'FETCH FAIL {pid}: {type(exc).__name__}: {str(exc)[:110]}')
        problems += 1

for row in RULES:
    pid = row[0]
    if pid not in fetched:
        print(f'NO PAGE   {pid:16} {row[1]}')
        problems += 1
        continue
    got = scraper.offer_from_rule({'id': pid, 'source_url': by_id[pid][1], 'affiliate_url': ''},
                                  build_rule(row), fetched[pid])
    want = EXPECTED.get((pid, row[1]))
    if got is None:
        print(f'NO MATCH  {pid:16} {row[1]:26} (expected {want})')
        problems += 1
        continue
    flag = 'ok ' if got.get('price') == want else 'BAD'
    if flag == 'BAD':
        problems += 1
    renewal = got.get('renewal_price')
    print(f'{flag} {pid:16} {row[1]:26} {got.get("currency")} {got.get("price")} /{got.get("billing_period")}'
          + (f'  renewal {renewal}' if renewal is not None else ''))

print(f'\n{len(RULES) - problems}/{len(RULES)} rules matched, {len(fetched)}/{len(by_id)} pages fetched')
