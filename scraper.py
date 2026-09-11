"""Conservative official-source checker for HostDealRadar.

The provider list and every extraction instruction live in .ilang/site.ilang.
On a failed source or unmatched rule, existing source-backed records remain intact
with their original capture timestamps; this checker never invents replacements.
"""
import argparse
import json
import re
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from config import ROOT, load_config, slug

DATA = ROOT / 'data/offers.json'
NUMERIC_FIELDS = {'price', 'renewal_price'}
INTEGER_FIELDS = {'discount_percent', 'commitment_months'}

def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')

def fetch(url, cfg):
    request = Request(url, headers={'User-Agent': cfg['settings']['user_agent'], 'Accept': 'text/html,application/xhtml+xml,text/plain'})
    with urlopen(request, timeout=cfg['settings']['timeout_seconds']) as response:
        body = response.read(cfg['settings']['max_response_bytes'] + 1)
        if len(body) > cfg['settings']['max_response_bytes']:
            raise ValueError('response exceeds configured limit')
        return response.status, body.decode(response.headers.get_content_charset() or 'utf-8', 'replace')

def permitted(source_url, cfg):
    parts = urlsplit(source_url)
    robots = f'{parts.scheme}://{parts.netloc}/robots.txt'
    try:
        status, text = fetch(robots, cfg)
    except (HTTPError, URLError, ValueError) as exc:
        return False, f'robots check failed: {exc}'
    if status != 200:
        return False, f'robots returned HTTP {status}'
    groups, agents, directives = [], [], []
    for raw in text.splitlines() + ['']:
        line = raw.split('#', 1)[0].strip()
        if not line:
            if agents:
                groups.append((agents, directives))
            agents, directives = [], []
            continue
        if ':' not in line:
            continue
        key, value = [x.strip() for x in line.split(':', 1)]
        if key.lower() == 'user-agent':
            if directives:
                groups.append((agents, directives))
                agents, directives = [], []
            agents.append(value.lower())
        elif key.lower() in ('allow', 'disallow') and agents:
            directives.append((key.lower(), value))
    path = parts.path or '/'
    matching = [directive for group_agents, directives in groups if '*' in group_agents or 'hostdealradar' in group_agents for directive in directives]
    applicable = [(len(value), kind) for kind, value in matching if value and path.startswith(value)]
    if not applicable:
        return True, 'allowed by robots.txt'
    longest = max(length for length, _ in applicable)
    kinds = [kind for length, kind in applicable if length == longest]
    return ('allow' in kinds), ('allowed' if 'allow' in kinds else 'disallowed') + ' by robots.txt'

def extract_value(segment, pattern):
    match = re.search(pattern, segment)
    if not match:
        return None
    return match.groupdict().get('value') or match.group(1)

def normalize_date(value):
    cleaned = re.sub(r'(st|nd|rd|th)\b', '', value, flags=re.I)
    return datetime.strptime(cleaned, '%d %B %Y').date().isoformat()

def offer_from_rule(provider, rule, raw):
    if rule.get('mode') == 'availability_only':
        return None
    if rule.get('mode') == 'presence':
        if not re.search(rule['pattern'], raw, re.I):
            return None
        segment = raw
    else:
        start = raw.find(rule['anchor'])
        if start < 0:
            return None
        segment = raw[start:start + int(rule.get('window_chars', 6000))]
    offer = {
        'slug': rule.get('slug') or slug(provider['id'] + '-' + rule['title']),
        'provider': provider['id'],
        'title': rule['title'],
        'category': rule.get('category', 'Hosting'),
        'source_url': provider['source_url'],
        'offer_url': provider['affiliate_url'] or provider['source_url'],
        'fetched_at': now(),
        'evidence': rule['structure'],
    }
    for key in ('currency', 'billing_period', 'commitment_months', 'price_text', 'condition'):
        if rule.get(key) is not None:
            offer[key] = rule[key]
    for field, pattern in rule.get('field_patterns', {}).items():
        value = extract_value(segment, pattern)
        if value is None:
            continue
        if field in NUMERIC_FIELDS:
            offer[field] = float(value)
        elif field in INTEGER_FIELDS:
            offer[field] = int(value)
        elif field == 'valid_until':
            offer[field] = normalize_date(value)
        else:
            offer[field] = value
    return offer

def retained_status(reason, records):
    return {'status': 'unavailable', 'reason': reason, 'published_count': len(records), 'retained_count': len(records)}

def run(config_path=None):
    cfg = load_config(config_path)
    previous = json.loads(DATA.read_text(encoding='utf-8')) if DATA.exists() else {'offers': []}
    prior_by_provider = {provider['id']: [] for provider in cfg['providers']}
    for offer in previous.get('offers', []):
        if offer.get('provider') in prior_by_provider:
            prior_by_provider[offer['provider']].append(offer)
    output, statuses = [], {}
    for provider in cfg['providers']:
        old = prior_by_provider[provider['id']]
        allowed, why = permitted(provider['source_url'], cfg)
        if not allowed:
            output.extend(old)
            statuses[provider['id']] = retained_status(why, old)
            continue
        try:
            status, raw = fetch(provider['source_url'], cfg)
        except HTTPError as exc:
            output.extend(old)
            statuses[provider['id']] = retained_status(f'Official source returned HTTP {exc.code}.', old)
            continue
        except (URLError, ValueError) as exc:
            output.extend(old)
            statuses[provider['id']] = retained_status(f'Official source could not be checked: {exc}.', old)
            continue
        if status != 200 or re.search(r'(?i)(<title>\s*Just a moment|challenge-running|cf-error-details|access denied|verify you are human)', raw):
            output.extend(old)
            statuses[provider['id']] = retained_status('Official source was unavailable or presented a challenge.', old)
            continue
        rules = [rule for rule in cfg['extractors'] if rule.get('provider') == provider['id']]
        if rules and all(rule.get('mode') == 'availability_only' for rule in rules):
            output.extend(old)
            statuses[provider['id']] = {'status': 'available_no_price_rule', 'reason': 'Official source and robots.txt were checked successfully; no deterministic price rule is approved, so no offer was published.', 'published_count': len(old), 'captured_count': 0, 'retained_count': len(old)}
            continue
        matched, errors = [], []
        for rule in rules:
            try:
                offer = offer_from_rule(provider, rule, raw)
            except (ValueError, IndexError) as exc:
                errors.append(str(exc))
                offer = None
            if offer:
                matched.append(offer)
        matched_slugs = {offer['slug'] for offer in matched}
        superseded_slugs = {slug for rule in cfg['extractors'] if rule.get('provider') == provider['id'] for slug in rule.get('supersedes_slugs', [])}
        retained = [offer for offer in old if offer.get('slug') not in matched_slugs and offer.get('slug') not in superseded_slugs]
        output.extend(matched + retained)
        if matched:
            reason = 'Official source checked; matched configured extraction rules.'
            if retained:
                reason += ' Some earlier records remain because their configured rule did not match this run.'
            if errors:
                reason += ' Some rules errored and their existing records were retained.'
            statuses[provider['id']] = {'status': 'checked', 'reason': reason, 'published_count': len(matched) + len(retained), 'captured_count': len(matched), 'retained_count': len(retained), 'rule_errors': errors}
        else:
            statuses[provider['id']] = {'status': 'unmatched', 'reason': 'Official source responded, but no configured extraction rule matched; existing records were retained.', 'published_count': len(retained), 'captured_count': 0, 'retained_count': len(retained), 'rule_errors': errors}
    payload = {'generated_at': now(), 'offers': output, 'source_status': statuses}
    DATA.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    return payload

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config')
    args = parser.parse_args()
    result = run(args.config)
    for provider, status in result['source_status'].items():
        print(f"{provider}: captured={status.get('captured_count', 0)} retained={status.get('retained_count', 0)} status={status['status']}")
