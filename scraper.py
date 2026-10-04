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
NUMERIC_FIELDS = {'price', 'renewal_price', 'first_term_total', 'renewal_monthly_rate'}
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

CHALLENGE = re.compile(r'(?i)(<title>\s*Just a moment|challenge-running|cf-error-details|access denied|verify you are human)')

def excerpt(raw, limit=280):
    """Store a bounded piece of visible response text, never script or markup."""
    return visible_text(raw)[:limit] or None

def compact(value):
    return re.sub(r'\s+', ' ', value).strip()

def anchored_excerpt(text, match, before=96, after=192):
    """Keep the matched source statement plus a small amount of context.

    A page-level opening excerpt only proves that a response arrived.  Field
    evidence must instead carry the exact source fragment from which the field
    was extracted.  ``text`` is either the configured raw segment or its
    visible-text equivalent, so the stored quote is never reconstructed.
    """
    start = max(0, match.start() - before)
    end = min(len(text), match.end() + after)
    return compact(text[start:end])

def claim_evidence(text, match, location, surface_hint=None):
    quote = compact(visible_text(match.group(0)))
    context = anchored_excerpt(text, match)
    visible_context = compact(visible_text(text[max(0, match.start() - 96):min(len(text), match.end() + 192)]))
    # Attribute and metadata matches can contain markup that has no visible
    # text node.  Retain the exact response fragment in that case and say so
    # in the location; do not relabel it as visitor-visible page copy.
    surface = surface_hint or 'visible source text'
    if not quote:
        quote = compact(match.group(0))
        surface = 'source response markup'
    elif surface_hint is None and quote not in visible_context:
        # The raw response still contains the exact match, but the source's
        # HTML does not expose it as reader-visible text (for example an href
        # query parameter).  Preserve that distinction instead of calling it
        # visible copy.
        surface = 'source response markup'
    return {'quote': quote, 'excerpt': context, 'visible_excerpt': visible_context,
            'location': location, 'surface': surface}

def configured_source_evidence(rules, raw):
    """Collect non-offer source statements configured in site.ilang.

    This is intentionally configuration-driven: provider-specific evidence
    patterns never live in Python.
    """
    output = {}
    for rule in rules:
        for label, pattern in rule.get('source_evidence_patterns', {}).items():
            if label in output:
                continue
            match = re.search(pattern, raw)
            if match:
                output[label] = claim_evidence(raw, match,
                    'configured source-evidence pattern: ' + label)
    return output

class SourceProbeError(Exception):
    def __init__(self, state, reason, request_url, http_status=None, visible_excerpt=None):
        super().__init__(reason)
        self.state = state
        self.reason = reason
        self.request_url = request_url
        self.http_status = http_status
        self.visible_excerpt = visible_excerpt

def error_body(exc, cfg):
    raw = exc.read(cfg['settings']['max_response_bytes'] + 1)[:cfg['settings']['max_response_bytes']]
    charset = exc.headers.get_content_charset() if hasattr(exc.headers, 'get_content_charset') else None
    return raw.decode(charset or 'utf-8', 'replace')

def probe_fields(state, request_url, http_status=None, visible_excerpt=None):
    return {'status': state, 'request_url': request_url, 'http_status': http_status,
            'visible_excerpt': visible_excerpt, 'checked_at': now()}

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
        permitted(url, cfg)
        try:
            status, raw = fetch(url, cfg, follow_redirects=False)
        except HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308):
                text = error_body(exc, cfg)
                state = 'challenge' if CHALLENGE.search(text) else 'unreadable'
                raise SourceProbeError(state, f'Official source returned HTTP {exc.code}.', url, exc.code, excerpt(text)) from exc
            target = urljoin(url, exc.headers.get('Location', ''))
            if not target.startswith('https://') or target == url: raise ValueError('Unsupported source redirect')
            url = target
            continue
        if status != 200 or CHALLENGE.search(raw):
            state = 'challenge' if CHALLENGE.search(raw) else 'unreadable'
            raise SourceProbeError(state, f'Official source returned HTTP {status} or a challenge page.', url, status, excerpt(raw))
        if not excerpt(raw):
            raise SourceProbeError('unreadable', 'Official source returned no visible text.', url, status)
        return status, raw, url
    raise SourceProbeError('unreadable', 'Too many source redirects.', url)

def permitted(source_url, cfg):
    parts = urlsplit(source_url)
    robots = f'{parts.scheme}://{parts.netloc}/robots.txt'
    try:
        status, text = fetch(robots, cfg)
    except HTTPError as exc:
        raw = error_body(exc, cfg)
        state = 'challenge' if CHALLENGE.search(raw) else 'unreadable'
        raise SourceProbeError(state, f'robots.txt returned HTTP {exc.code}; source page was not requested.', robots, exc.code, excerpt(raw)) from exc
    except (URLError, ValueError, TimeoutError, OSError) as exc:
        raise SourceProbeError('unreadable', f'robots check failed: {exc}; source page was not requested.', robots) from exc
    if status != 200:
        raise SourceProbeError('unreadable', f'robots.txt returned HTTP {status}; source page was not requested.', robots, status, excerpt(text))
    if re.search(r'(?i)<(?:html|title|body)\b', text):
        state = 'challenge' if CHALLENGE.search(text) else 'unreadable'
        raise SourceProbeError(state, 'robots.txt returned HTML instead of crawl directives; source page was not requested.', robots, status, excerpt(text))
    allowed, reason = robots_decision(text, source_url)
    if not allowed:
        raise SourceProbeError('unreadable', reason + '; source page was not requested.', robots, status, reason)

def extract_value(segment, pattern):
    match = re.search(pattern, segment)
    if not match:
        return None
    return match.groupdict().get('value') or match.group(1)

def normalize_date(value):
    cleaned = re.sub(r'(st|nd|rd|th)\b', '', value, flags=re.I)
    return datetime.strptime(cleaned, '%d %B %Y').date().isoformat()

CURRENCY_CODES = {'USD', 'CAD', 'AUD', 'NZD', 'EUR', 'GBP', 'JPY', 'CHF', 'SEK', 'NOK', 'DKK'}
PERIOD_PATTERNS = {
    'month': r'(?:/\s*(?:mo(?:nth)?|month)\b|\bper\s+month\b|\bpaid\s+monthly\b)',
    'year': r'(?:/\s*(?:yr|year)\b|\bper\s+year\b)',
    'site': r'(?:/\s*site\b|\bper\s+site\b)',
    'server': r'(?:/\s*server\b|\bper\s+server\b)',
}

def price_has_matching_terms(segment, match, currency, billing_period):
    """Require the price expression itself to support its displayed terms.

    A configured label is not evidence: the source must show the requested unit,
    and a nearby ISO currency label must not contradict the displayed currency.
    Currency symbols without an ISO label remain usable for existing US-source
    rules, but an explicit CAD/AUD/etc. can never be relabelled as USD.
    """
    if billing_period in PERIOD_PATTERNS and not re.search(PERIOD_PATTERNS[billing_period], match.group(0), re.I):
        return False
    left = max(0, match.start() - 16)
    right = min(len(segment), match.end() + 16)
    nearby_codes = set(re.findall(r'\b[A-Z]{3}\b', segment[left:right])) & CURRENCY_CODES
    return not nearby_codes or nearby_codes == {currency}

def offer_from_rule(provider, rule, raw, sibling_anchors=()):
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
        else:
            # A price may not cross into another configured plan card.  This is
            # a safe default for rules that did not supply an explicit end anchor.
            ends = [segment.find(anchor, len(rule['anchor'])) for anchor in sibling_anchors if anchor]
            ends = [end for end in ends if end >= 0]
            if ends:
                segment = segment[:min(ends)]
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
        'claim_evidence': {},
    }
    for key in ('currency', 'billing_period', 'commitment_months', 'price_text', 'condition', 'kind'):
        if rule.get(key) is not None:
            offer[key] = rule[key]
    for field, pattern in rule.get('field_patterns', {}).items():
        match = re.search(pattern, segment)
        if not match:
            continue
        value = match.groupdict().get('value') or match.group(1)
        if field == 'price' and not price_has_matching_terms(segment, match, offer.get('currency'), offer.get('billing_period')):
            return None
        source_claim = claim_evidence(segment, match,
            'configured field pattern: ' + field,
            'visible source text' if rule.get('mode') == 'anchored_text' else 'source response markup')
        offer['field_evidence'][field] = source_claim['quote']
        offer['claim_evidence'][field] = source_claim
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

def retained_status(reason, records, probe):
    """Provider-level status for a run that produced no fresh capture.

    Every existing record is retained, so the retained slug list is explicit:
    the renderer must not infer per-record state from a provider-level flag.
    """
    slugs = [offer['slug'] for offer in records]
    return {**probe, 'capture_status': 'not_attempted', 'reason': reason, 'published_count': len(records),
            'captured_count': 0, 'retained_count': len(records),
            'captured_slugs': [], 'retained_slugs': slugs}

def suspended_after_403(provider, records, previous_status):
    """Keep a 403-blocked source dormant until a person reviews and clears it.

    Scheduled runs must not turn a persistent denial into a request every six
    hours. The stored request URL and timestamp remain those of the actual 403.
    """
    reason = (f"Automatic requests suspended after HTTP 403 from {previous_status.get('request_url')}. "
              f"No request was sent in this run. Last 403 response: {previous_status.get('checked_at')}. "
              "A status-row deletion cannot resume checks. One request requires same-day manual evidence tied to this 403: the exact official page URL and a verbatim quote from its visible text, plus the official robots.txt HTTP 200 excerpt with an explicit Allow rule for this configured path, recorded in source_403_clearances.")
    probe = {
        'status': previous_status.get('status', 'unreadable'),
        'request_url': previous_status.get('request_url') or provider['source_url'],
        'http_status': 403,
        'visible_excerpt': previous_status.get('visible_excerpt'),
        'checked_at': previous_status.get('checked_at'),
    }
    result = retained_status(reason, records, probe)
    result['suspended_after_403'] = True
    return result

def source_origin(url):
    parts = urlsplit(url)
    return f'{parts.scheme}://{parts.netloc}'

def valid_403_clearance(provider, lock, evidence):
    """Accept only fresh, one-use human evidence for the exact locked source."""
    if not isinstance(evidence, dict) or not isinstance(lock, dict):
        return False
    source_url = provider['source_url']
    expected_robots_url = source_origin(source_url) + '/robots.txt'
    checked_on = datetime.now(timezone.utc).date().isoformat()
    quote = compact(evidence.get('official_quote') or '')
    visible_excerpt = compact(evidence.get('official_visible_excerpt') or '')
    robots_excerpt = evidence.get('robots_excerpt') or ''
    if (evidence.get('review_method') != 'manual_browser_review'
            or evidence.get('checked_on') != checked_on
            or evidence.get('clears_403_checked_at') != lock.get('checked_at')
            or evidence.get('source_url') != source_url
            or evidence.get('official_page_url') != source_url
            or evidence.get('official_http_status') != 200
            or len(quote) < 12 or quote not in visible_excerpt
            or evidence.get('robots_url') != expected_robots_url
            or evidence.get('robots_http_status') != 200
            or not robots_excerpt.strip()):
        return False
    allowed, reason = robots_decision(robots_excerpt, source_url)
    # Absence of a Disallow directive is not affirmative permission for this
    # manual reauthorization gate; require an explicit matching Allow rule.
    return allowed and ': allow:' in reason.lower()

def run(config_path=None):
    cfg = load_config(config_path)
    previous = json.loads(DATA.read_text(encoding='utf-8')) if DATA.exists() else {'offers': []}
    locks = dict(previous.get('source_403_locks') or {})
    clearances = dict(previous.get('source_403_clearances') or {})
    clearance_history = list(previous.get('source_403_history') or [])
    prior_by_provider = {provider['id']: [] for provider in cfg['providers']}
    for offer in previous.get('offers', []):
        if offer.get('provider') in prior_by_provider:
            prior_by_provider[offer['provider']].append(offer)
    output, statuses = [], {}
    for provider in cfg['providers']:
        old = prior_by_provider[provider['id']]
        prior_status = (previous.get('source_status') or {}).get(provider['id'], {})
        if prior_status.get('http_status') == 403 and provider['id'] not in locks:
            locks[provider['id']] = {key: prior_status.get(key) for key in
                                     ('status', 'request_url', 'http_status', 'checked_at', 'visible_excerpt')}
        lock = locks.get(provider['id'])
        clearance = clearances.get(provider['id'])
        clearance_used = bool(lock and valid_403_clearance(provider, lock, clearance))
        if lock and not clearance_used:
            output.extend(old)
            status_source = prior_status if prior_status.get('http_status') == 403 else lock
            statuses[provider['id']] = suspended_after_403(provider, old, status_source)
            continue
        if clearance_used:
            # A manual clearance permits one request only. A failed response
            # consumes it; a new 403 therefore needs fresh human evidence.
            clearances.pop(provider['id'], None)
        try:
            status, raw, request_url = fetch_source(provider['source_url'], cfg)
        except SourceProbeError as exc:
            output.extend(old)
            if clearance_used:
                clearance_history.append({'provider': provider['id'], 'clearance': clearance,
                                          'attempted_at': now(), 'result': 'request failed',
                                          'http_status': exc.http_status, 'request_url': exc.request_url})
            if exc.http_status == 403:
                locks[provider['id']] = {'status': exc.state, 'request_url': exc.request_url, 'http_status': 403,
                                         'checked_at': now(), 'visible_excerpt': exc.visible_excerpt}
                reason = (exc.reason + ' This provider is now locked from further automatic requests. '
                          'A status-row deletion cannot resume checks; one recheck requires same-day manual evidence tied to this 403: the official page text and an HTTP 200 robots.txt response with an explicit Allow rule for this configured path.')
            else:
                reason = exc.reason
                if clearance_used:
                    reason += ' The prior HTTP 403 lock remains; its one-use manual authorization was consumed, so another recheck requires fresh evidence.'
            statuses[provider['id']] = retained_status(reason, old,
                probe_fields(exc.state, exc.request_url, exc.http_status, exc.visible_excerpt))
            continue
        except (URLError, ValueError, TimeoutError, OSError) as exc:
            output.extend(old)
            reason = f'Official source could not be checked: {exc}.'
            if clearance_used:
                clearance_history.append({'provider': provider['id'], 'clearance': clearance,
                                          'attempted_at': now(), 'result': 'request failed',
                                          'http_status': None, 'request_url': provider['source_url']})
                reason += ' The prior HTTP 403 lock remains; its one-use manual authorization was consumed, so another recheck requires fresh evidence.'
            statuses[provider['id']] = retained_status(reason, old,
                probe_fields('unreadable', provider['source_url']))
            continue
        if clearance_used:
            locks.pop(provider['id'], None)
            clearance_history.append({'provider': provider['id'], 'clearance': clearance,
                                      'attempted_at': now(), 'result': 'source returned HTTP 200',
                                      'http_status': status, 'request_url': request_url})
        rules = [rule for rule in cfg['extractors'] if rule.get('provider') == provider['id']]
        source_probe = probe_fields('evidenced', request_url, status, excerpt(raw))
        auxiliary = configured_source_evidence(rules, raw)
        if auxiliary:
            source_probe['source_claim_evidence'] = auxiliary
        if rules and all(rule.get('mode') == 'availability_only' for rule in rules):
            output.extend(old)
            blocker = rules[0].get('blocker', 'unspecified')
            blocker_evidence = rules[0].get('blocker_evidence', '')
            reason = f'Official source and robots.txt were checked successfully. No deterministic price rule can be written ({blocker}), so no offer was published. Evidence: {blocker_evidence}'
            statuses[provider['id']] = {**source_probe, 'capture_status': 'no_price_rule', 'reason': reason, 'blocker': blocker, 'blocker_evidence': blocker_evidence, 'published_count': len(old), 'captured_count': 0, 'retained_count': len(old), 'captured_slugs': [], 'retained_slugs': [offer['slug'] for offer in old]}
            continue
        matched, errors, unmatched_rules = [], [], []
        for rule in rules:
            try:
                sibling_anchors = [candidate.get('anchor') for candidate in rules if candidate is not rule and candidate.get('mode') == rule.get('mode')]
                offer = offer_from_rule(provider, rule, raw, sibling_anchors)
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
            statuses[provider['id']] = {**source_probe, 'capture_status': 'matched', 'reason': reason, 'published_count': len(matched) + len(retained), 'captured_count': len(matched), 'retained_count': len(retained), 'captured_slugs': sorted(matched_slugs), 'retained_slugs': [offer['slug'] for offer in retained], 'rule_errors': errors}
        else:
            statuses[provider['id']] = {**source_probe, 'capture_status': 'unmatched', 'reason': 'Official source responded, but no configured extraction rule matched; existing records were retained.', 'published_count': len(retained), 'captured_count': 0, 'retained_count': len(retained), 'captured_slugs': [], 'retained_slugs': [offer['slug'] for offer in retained], 'rule_errors': errors}
        statuses[provider['id']]['unmatched_rules'] = unmatched_rules
    payload = {'generated_at': now(), 'offers': output, 'source_status': statuses,
               'source_403_locks': locks, 'source_403_clearances': clearances,
               'source_403_history': clearance_history}
    # Carry over any top-level key this checker does not own. build.py persists
    # the sitemap lastmod state into this same file, so dropping unknown keys
    # here would reset every lastmod on the next refresh run.
    for key, value in previous.items():
        if key not in payload: payload[key] = value
    DATA.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return payload

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config')
    args = parser.parse_args()
    result = run(args.config)
    for provider, status in result['source_status'].items():
        print(f"{provider}: captured={status.get('captured_count', 0)} retained={status.get('retained_count', 0)} status={status['status']}")
