"""Conservative official-source checker for HostDealRadar.

The provider list and every extraction instruction live in .ilang/site.ilang.
On a failed source or unmatched rule, existing source-backed records remain intact
with their original capture timestamps; this checker never invents replacements.
"""
import argparse
import hashlib
import math
import json
import re
from html.parser import HTMLParser
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urljoin
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler

from config import ROOT, load_config, slug

DATA = ROOT / 'data/offers.json'
NUMERIC_FIELDS = {'price', 'renewal_price'}
INTEGER_FIELDS = {'discount_percent', 'commitment_months'}

class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'): self.hidden += 1
    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'): self.hidden = max(0, self.hidden - 1)
    def handle_data(self, data):
        if not self.hidden: self.parts.append(data)

def visible_text(raw):
    parser = VisibleText()
    parser.feed(raw)
    return re.sub(r'\s+', ' ', ' '.join(parser.parts)).strip()

def robots_decision(text, source_url, product='hostdealradar'):
    groups, agents, directives = [], [], []
    for raw in text.splitlines():
        line = raw.split('#', 1)[0].strip()
        if ':' not in line: continue
        key, value = [x.strip() for x in line.split(':', 1)]
        if key.lower() == 'user-agent':
            if directives:
                groups.append((agents, directives))
                agents, directives = [], []
            agents.append(value.lower())
        elif key.lower() in ('allow', 'disallow') and agents:
            directives.append((key.lower(), value))
    if agents: groups.append((agents, directives))
    selected = [rules for names, rules in groups if product in names]
    if not selected: selected = [rules for names, rules in groups if '*' in names]
    parts = urlsplit(source_url)
    path = (parts.path or '/') + ('?' + parts.query if parts.query else '')
    matches = []
    for rules in selected:
        for kind, value in rules:
            if not value: continue
            terminal = value.endswith('$')
            pattern = '^' + re.escape(value[:-1] if terminal else value).replace(r'\*', '.*') + ('$' if terminal else '')
            if re.search(pattern, path):
                matches.append((len(value.encode('utf-8')), kind, value))
    if not matches: return True, 'robots.txt: no disallow rule matches this path'
    longest = max(item[0] for item in matches)
    chosen = sorted((item for item in matches if item[0] == longest), key=lambda item: item[1] != 'allow')[0]
    return chosen[1] == 'allow', 'robots.txt: ' + chosen[1] + ': ' + chosen[2]

def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None

def fetch(url, cfg, follow_redirects=True):
    request = Request(url, headers={'User-Agent': cfg['settings']['user_agent'], 'Accept': 'text/html,application/xhtml+xml,text/plain'})
    opener = urlopen if follow_redirects else build_opener(NoRedirect).open
    with opener(request, timeout=cfg['settings']['timeout_seconds']) as response:
        body = response.read(cfg['settings']['max_response_bytes'] + 1)
        if len(body) > cfg['settings']['max_response_bytes']:
            raise ValueError('response exceeds configured limit')
        return response.status, body.decode(response.headers.get_content_charset() or 'utf-8', 'replace')

def fetch_source(url, cfg):
    for _ in range(6):
        allowed, reason = permitted(url, cfg)
        if not allowed: raise ValueError(reason + ' at ' + url)
        try:
            status, raw = fetch(url, cfg, follow_redirects=False)
            return status, raw
        except HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308): raise
            target = urljoin(url, exc.headers.get('Location', ''))
            if not target.startswith('https://') or target == url: raise ValueError('Unsupported source redirect')
            url = target
    raise ValueError('Too many source redirects')

def permitted(source_url, cfg):
    parts = urlsplit(source_url)
    robots = f'{parts.scheme}://{parts.netloc}/robots.txt'
    try:
        status, text = fetch(robots, cfg)
    except (HTTPError, URLError, ValueError, TimeoutError, OSError) as exc:
        return False, f'robots check failed: {exc}'
    if status != 200:
        return False, f'robots returned HTTP {status}'
    if re.search(r'(?i)<(?:html|title|body)\b', text):
        return False, 'robots.txt returned HTML instead of crawl directives'
    return robots_decision(text, source_url)

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
    if any(not re.search(pattern, raw) for pattern in rule.get('raw_checks', [])):
        return None
    document = visible_text(raw) if rule.get('mode') == 'anchored_text' else raw
    if rule.get('mode') == 'presence':
        if not re.search(rule['pattern'], raw, re.I):
            return None
        segment = raw
    else:
        start = document.find(rule['anchor'])
        if start < 0:
            return None
        segment = document[start:start + int(rule.get('window_chars', 6000))]
        if rule.get('end_anchor'):
            end = segment.find(rule['end_anchor'], len(rule['anchor']))
            if end < 0: return None
            segment = segment[:end]
    if any(not re.search(pattern, segment) for pattern in rule.get('checks', [])):
        return None
    offer = {
        'slug': rule.get('slug') or slug(provider['id'] + '-' + rule['title']),
        'provider': provider['id'],
        'title': rule['title'],
        'category': rule.get('category', 'Hosting'),
        'source_url': provider['source_url'],
        'offer_url': provider['affiliate_url'] or provider['source_url'],
        'fetched_at': now(),
        'evidence': rule['structure'],
        'field_evidence': {},
    }
    for key in ('currency', 'billing_period', 'commitment_months', 'price_text', 'condition', 'kind'):
        if rule.get(key) is not None:
            offer[key] = rule[key]
    for field, pattern in rule.get('field_patterns', {}).items():
        value = extract_value(segment, pattern)
        if value is None:
            continue
        offer['field_evidence'][field] = visible_text(re.search(pattern, segment).group(0))
        if field in NUMERIC_FIELDS:
            offer[field] = float(value.replace(',', ''))
        elif field in INTEGER_FIELDS:
            offer[field] = int(value)
        elif field == 'valid_until':
            offer[field] = normalize_date(value)
        else:
            offer[field] = value
    required = rule.get('required_fields', ['price'] if rule.get('mode') != 'presence' else [])
    if any(offer.get(field) in (None, '') for field in required): return None
    if any(not math.isfinite(offer[field]) or offer[field] <= 0 for field in NUMERIC_FIELDS if field in offer): return None
    if 'price' in offer and not all(offer.get(key) for key in ('currency', 'billing_period')): return None
    if not any(offer.get(key) for key in ('price', 'price_text', 'discount_percent', 'coupon_code')): return None
    offer.setdefault('kind', 'promotion' if any(offer.get(k) for k in ('discount_percent', 'coupon_code', 'price_text')) else 'regular_price')
    offer['source_sha256'] = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    return offer

def retained_status(reason, records):
    """Provider-level status for a run that produced no fresh capture.

    Every existing record is retained, so the retained slug list is explicit:
    the renderer must not infer per-record state from a provider-level flag.
    """
    slugs = [offer['slug'] for offer in records]
    return {'status': 'unavailable', 'reason': reason, 'published_count': len(records),
            'captured_count': 0, 'retained_count': len(records),
            'captured_slugs': [], 'retained_slugs': slugs}

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
        try:
            status, raw = fetch_source(provider['source_url'], cfg)
        except HTTPError as exc:
            output.extend(old)
            statuses[provider['id']] = retained_status(f'Official source returned HTTP {exc.code}.', old)
            continue
        except (URLError, ValueError, TimeoutError, OSError) as exc:
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
            blocker = rules[0].get('blocker', 'unspecified')
            evidence = rules[0].get('blocker_evidence', '')
            reason = f'Official source and robots.txt were checked successfully. No deterministic price rule can be written ({blocker}), so no offer was published. Evidence: {evidence}'
            statuses[provider['id']] = {'status': 'available_no_price_rule', 'reason': reason, 'blocker': blocker, 'blocker_evidence': evidence, 'published_count': len(old), 'captured_count': 0, 'retained_count': len(old), 'captured_slugs': [], 'retained_slugs': [offer['slug'] for offer in old]}
            continue
        matched, errors, unmatched_rules = [], [], []
        for rule in rules:
            try:
                offer = offer_from_rule(provider, rule, raw)
            except (ValueError, IndexError) as exc:
                errors.append(str(exc))
                offer = None
            if offer:
                matched.append(offer)
            else:
                unmatched_rules.append(rule['title'])
        matched_slugs = {offer['slug'] for offer in matched}
        superseded_slugs = {previous_slug for rule in rules if (rule.get('slug') or slug(provider['id'] + '-' + rule['title'])) in matched_slugs for previous_slug in rule.get('supersedes_slugs', [])}
        retained = [offer for offer in old if offer.get('slug') not in matched_slugs and offer.get('slug') not in superseded_slugs]
        output.extend(matched + retained)
        if matched:
            reason = 'Official source checked; matched configured extraction rules.'
            if retained:
                reason += ' Some earlier records remain because their configured rule did not match this run.'
            if errors:
                reason += ' Some rules errored and their existing records were retained.'
            statuses[provider['id']] = {'status': 'checked', 'reason': reason, 'published_count': len(matched) + len(retained), 'captured_count': len(matched), 'retained_count': len(retained), 'captured_slugs': sorted(matched_slugs), 'retained_slugs': [offer['slug'] for offer in retained], 'rule_errors': errors}
        else:
            statuses[provider['id']] = {'status': 'unmatched', 'reason': 'Official source responded, but no configured extraction rule matched; existing records were retained.', 'published_count': len(retained), 'captured_count': 0, 'retained_count': len(retained), 'captured_slugs': [], 'retained_slugs': [offer['slug'] for offer in retained], 'rule_errors': errors}
        statuses[provider['id']]['unmatched_rules'] = unmatched_rules
    for status in statuses.values(): status['checked_at'] = now()
    payload = {'generated_at': now(), 'offers': output, 'source_status': statuses}
    # Carry over any top-level key this checker does not own. build.py persists
    # the sitemap lastmod state into this same file, so dropping unknown keys
    # here would reset every lastmod on the next refresh run.
    for key, value in previous.items():
        if key not in payload: payload[key] = value
    DATA.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    return payload

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config')
    args = parser.parse_args()
    result = run(args.config)
    for provider, status in result['source_status'].items():
        print(f"{provider}: captured={status.get('captured_count', 0)} retained={status.get('retained_count', 0)} status={status['status']}")
