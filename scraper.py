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
from html import unescape
from pathlib import Path
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

class PriceTables(HTMLParser):
    """Keep exact source cells so a price can be bound to its row and header."""
    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.raw, self.tables, self.stack = raw, [], []
        self.offsets = [0]
        self.offsets.extend(match.end() for match in re.finditer('\n', raw))
        self.feed(raw)

    def source_offset(self):
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.stack.append({'rows':[], 'row':None, 'cell':None})
        elif self.stack:
            table = self.stack[-1]
            if tag == 'tr':
                table['row'] = []
            elif tag in ('td', 'th') and table['row'] is not None:
                table['cell'] = {'tag':tag, 'attrs':dict(attrs), 'start':self.source_offset()}

    def handle_endtag(self, tag):
        if not self.stack:
            return
        table = self.stack[-1]
        if tag in ('td', 'th') and table['cell'] is not None and table['cell']['tag'] == tag:
            cell = table['cell']
            end = self.raw.find('>', self.source_offset())
            cell['raw'] = self.raw[cell.pop('start'):end + 1]
            cell['text'] = visible_text(cell['raw'])
            table['row'].append(cell)
            table['cell'] = None
        elif tag == 'tr' and table['row'] is not None:
            table['rows'].append(table['row'])
            table['row'] = None
        elif tag == 'table':
            self.tables.append(self.stack.pop()['rows'])

def table_price_cell(raw, rule):
    matches = []
    for rows in PriceTables(raw).tables:
        if rule.get('table_match') and not re.search(rule['table_match'], ' '.join(cell['text'] for row in rows for cell in row)):
            continue
        headers = [(index, cell, row) for row in rows for index, cell in enumerate(row)
                   if cell['tag'] == 'th' and cell['attrs'].get('scope') != 'row'
                   and re.fullmatch(rule['column_match'], cell['text'])
                   and all(cell['attrs'].get(key) == value for key, value in rule.get('column_attributes', {}).items())]
        if len(headers) != 1:
            continue
        index, header, header_row = headers[0]
        if any(cell['attrs'].get('colspan', '1') != '1' for cell in header_row):
            continue
        for row in rows:
            if not row or len(row) != len(header_row) or not re.search(rule['row_match'], row[0]['text']):
                continue
            if any(cell['attrs'].get('colspan', '1') != '1' or cell['attrs'].get('rowspan', '1') != '1' for cell in row):
                continue
            if any(not re.search(pattern, ' '.join(cell['text'] for cell in row)) for pattern in rule.get('row_checks', [])):
                continue
            if any(row[index]['attrs'].get(key) != value for key, value in rule.get('column_attributes', {}).items()):
                continue
            matches.append((row[index], header, row[0]))
    return matches[0] if len(matches) == 1 else None

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
    'month': r'(?:/\s*(?:mo(?:nth)?|month)\b|\bper\s+month\b|\bmonthly\b|\bfirst[ -]month\b|\b(?:USD|CAD|AUD|EUR|GBP)\s+month\b)',
    'year': r'(?:/\s*(?:yr|year)\b|\bper\s+year\b|\bannual(?:ly)?\b)',
    'site': r'(?:/\s*site\b|\bper\s+site\b)',
    'server': r'(?:/\s*server\b|\bper\s+server\b)',
}

def price_has_matching_terms(segment, match, currency, billing_period, unit_evidence=''):
    """Require the price expression itself to support its displayed terms.

    A configured label is not evidence: the source must show the requested unit,
    and a nearby ISO currency label must not contradict the displayed currency.
    Currency symbols without an ISO label remain usable for existing US-source
    rules, but an explicit CAD/AUD/etc. can never be relabelled as USD.
    """
    quote = visible_text(match.group(0))
    if billing_period in PERIOD_PATTERNS and not re.search(PERIOD_PATTERNS[billing_period], quote, re.I):
        # A table header can establish the unit only after the exact row and
        # column were selected. A conflicting unit in the price is never fixed
        # by borrowing a header or an unrelated phrase elsewhere on the page.
        if (any(re.search(pattern, quote, re.I) for pattern in PERIOD_PATTERNS.values())
                or not re.search(PERIOD_PATTERNS[billing_period], unit_evidence, re.I)):
            return False
    left = max(0, match.start() - 16)
    right = min(len(segment), match.end() + 16)
    nearby_codes = set(re.findall(r'\b[A-Z]{3}\b', segment[left:right])) & CURRENCY_CODES
    return not nearby_codes or nearby_codes == {currency}

def offer_from_rule(provider, rule, raw, sibling_anchors=(), diagnostics=None):
    def missing(reason):
        if diagnostics is not None:
            diagnostics.append(reason)
        return None
    if rule.get('mode') == 'availability_only':
        return missing('No price extractor is configured.')
    if any(not re.search(pattern, raw) for pattern in rule.get('raw_checks', [])):
        return missing('Required source-level wording was not found.')
    document = visible_text(raw) if rule.get('mode') == 'anchored_text' else raw
    if rule.get('scope_anchor'):
        scope_start = document.find(rule['scope_anchor'])
        if scope_start < 0:
            return missing('The configured product-family scope was not found.')
        document = document[scope_start:scope_start + rule.get('scope_window_chars', 20000)]
    table_evidence = None
    unit_evidence = ''
    if rule.get('mode') == 'table_cell':
        selected = table_price_cell(raw, rule)
        if selected is None:
            return missing('An unambiguous price cell with the configured plan row and column header was not found.')
        cell, header, row_header = selected
        segment = cell['text']
        unit_evidence = header['text']
        table_evidence = {'column_quote':header['text'], 'column_attributes':header['attrs'],
                          'row_quote':row_header['text'], 'surface':'official HTML table'}
    elif rule.get('mode') == 'presence':
        if not re.search(rule['pattern'], raw, re.I):
            return missing('Required source wording was not found.')
        segment = raw
    else:
        start = document.find(rule['anchor'])
        if start < 0:
            return missing('The configured plan anchor was not found.')
        segment = document[start:start + int(rule.get('window_chars', 6000))]
        if rule.get('end_anchor'):
            end = segment.find(rule['end_anchor'], len(rule['anchor']))
            if end < 0: return missing('The configured end of this plan card was not found.')
            segment = segment[:end]
        else:
            # A price may not cross into another configured plan card.  This is
            # a safe default for rules that did not supply an explicit end anchor.
            ends = [segment.find(anchor, len(rule['anchor'])) for anchor in sibling_anchors if anchor]
            ends = [end for end in ends if end >= 0]
            if ends:
                segment = segment[:min(ends)]
    if any(not re.search(pattern, segment) for pattern in rule.get('checks', [])):
        return missing('Required wording was not found inside this plan card.')
    array_price = None
    array_evidence = None
    unit_claim = None
    if rule.get('mode') == 'parallel_arrays':
        arrays, array_matches = {}, {}
        for field, pattern in rule['array_patterns'].items():
            match = re.search(pattern, segment)
            if not match:
                return missing('A required price or resource array was not found: ' + field + '.')
            arrays[field] = json.loads(match.group('value'))
            array_matches[field] = match
        index = rule['array_index']
        if (not isinstance(index, int) or isinstance(index, bool) or index < 0
                or not all(isinstance(values, list) and len(values) == len(arrays['price']) and index < len(values) for values in arrays.values())
                or not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in arrays['price'])):
            return missing('Parallel plan arrays have incompatible lengths or invalid price values.')
        if any(arrays[field][index] != value for field, value in rule.get('array_match', {}).items()):
            return missing('The selected price array entry no longer matches the configured plan resources.')
        unit_match = re.search(rule['billing_unit_pattern'], raw)
        if not unit_match:
            return missing('The source-wide price formatter no longer establishes currency and billing unit.')
        unit_evidence = unit_match.group('value')
        unit_claim = {'quote':unit_evidence, 'excerpt':compact(unit_match.group(0)),
                      'visible_excerpt':unit_evidence, 'location':'official pricing-widget formatter', 'surface':'source response markup'}
        values = array_matches['price'].group('value')
        tokens = list(re.finditer(r'-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?', values))
        if len(tokens) != len(arrays['price']):
            return missing('The numeric price array cannot be read unambiguously.')
        array_price = (values, tokens[index])
        array_evidence = {'index':index, 'resources':rule.get('array_match', {}),
                          'source_arrays':{field:match.group('value') for field, match in array_matches.items()}}
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
    if table_evidence:
        offer['table_evidence'] = table_evidence
        if any(re.search(pattern, unit_evidence, re.I) for pattern in PERIOD_PATTERNS.values()):
            offer['field_evidence']['billing_period'] = unit_evidence
            offer['claim_evidence']['billing_period'] = {'quote':unit_evidence, 'excerpt':header['raw'],
                'visible_excerpt':unit_evidence, 'location':'selected price column header', 'surface':'visible source text'}
    if array_evidence:
        offer['array_evidence'] = array_evidence
        offer['field_evidence']['price_currency'] = unit_evidence
        offer['field_evidence']['billing_period'] = unit_evidence
        offer['claim_evidence']['price_currency'] = unit_claim
        offer['claim_evidence']['billing_period'] = unit_claim
    for key in ('currency', 'billing_period', 'commitment_months', 'price_text', 'condition', 'kind'):
        if rule.get(key) is not None:
            offer[key] = rule[key]
    patterns = dict(rule.get('field_patterns', {}))
    if array_price:
        patterns['price'] = None
    for field, pattern in patterns.items():
        field_segment, match = array_price if field == 'price' and array_price else (segment, re.search(pattern, segment))
        if not match:
            continue
        value = match.group(0) if field == 'price' and array_price else match.groupdict().get('value') or match.group(1)
        if field == 'price':
            metadata_unit = re.search(r'\bdata-(?:on-value|billing-period)=["\'](monthly|yearly|annual)["\']', match.group(0), re.I)
            price_unit = unit_evidence or (metadata_unit.group(1) if metadata_unit else '')
            if not price_has_matching_terms(field_segment, match, offer.get('currency'), offer.get('billing_period'), price_unit):
                return missing('The price quote does not establish the configured currency and billing unit.')
            unit_currencies = set(re.findall(r'\b[A-Z]{3}\b', price_unit)) & CURRENCY_CODES
            if unit_currencies and unit_currencies != {offer.get('currency')}:
                return missing('The source-wide price currency conflicts with the configured currency.')
            if metadata_unit and not unit_evidence:
                offer['field_evidence']['billing_period'] = metadata_unit.group(0)
                offer['claim_evidence']['billing_period'] = claim_evidence(match.group(0), metadata_unit,
                    'billing-period attribute on the same price element', 'source response markup')
        source_claim = claim_evidence(field_segment, match,
            'configured field pattern: ' + field,
            'visible source text' if rule.get('mode') in ('anchored_text', 'table_cell') else 'source response markup')
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
    absent = [field for field in required if offer.get(field) in (None, '')]
    if absent: return missing('Required fields were not found: ' + ', '.join(absent) + '.')
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

def run(config_path=None, provider_ids=None, capture_dir=None):
    cfg = load_config(config_path)
    selected = set(provider_ids) if provider_ids is not None else {p['id'] for p in cfg['providers']}
    unknown = selected - {p['id'] for p in cfg['providers']}
    if unknown:
        raise ValueError('Unknown provider identifiers: ' + ', '.join(sorted(unknown)))
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
        if provider['id'] not in selected:
            output.extend(old)
            if prior_status:
                statuses[provider['id']] = prior_status
            continue
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
            resources = {}
            resource_probes = []
            for url in cfg.get('source_resources', {}).get(provider['id'], []):
                references = re.findall(r'<script\b[^>]*\bsrc=["\']([^"\']+)', raw, re.I)
                if url not in [urljoin(request_url, unescape(reference)) for reference in references]:
                    raise ValueError('The configured supplementary price resource is no longer linked by the official source page')
                resource_status, resource_raw, resource_url = fetch_source(url, cfg)
                resources[url] = resource_raw
                resource_probes.append({'request_url':resource_url, 'http_status':resource_status,
                                        'checked_at':now(), 'sha256':hashlib.sha256(resource_raw.encode('utf-8')).hexdigest()})
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
        if capture_dir is not None:
            folder = Path(capture_dir)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / (provider['id'] + '.html')).write_text(raw, encoding='utf-8')
            for index, resource_raw in enumerate(resources.values(), 1):
                (folder / (provider['id'] + f'--resource-{index}.txt')).write_text(resource_raw, encoding='utf-8')
        rules = [rule for rule in cfg['extractors'] if rule.get('provider') == provider['id']]
        source_probe = probe_fields('evidenced', request_url, status, excerpt(raw))
        if resource_probes:
            source_probe['source_resources'] = resource_probes
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
        matched, errors, unmatched_rules, rule_failures = [], [], [], {}
        for rule in rules:
            diagnostics = []
            try:
                sibling_anchors = [candidate.get('anchor') for candidate in rules if candidate is not rule and candidate.get('mode') == rule.get('mode')]
                rule_provider, rule_raw = provider, raw
                if rule.get('source_resource'):
                    rule_raw = resources[rule['source_resource']]
                    rule_provider = dict(provider, source_url=rule['source_resource'], affiliate_url=provider['affiliate_url'] or provider['source_url'])
                if any(re.search(pattern, raw) for pattern in rule.get('primary_reject_patterns', [])):
                    diagnostics.append('The primary pricing page has campaign controls that can modify the published base prices; no payable price is inferred.')
                    offer = None
                else:
                    offer = offer_from_rule(rule_provider, rule, rule_raw, sibling_anchors, diagnostics)
            except (ValueError, IndexError) as exc:
                errors.append(str(exc))
                offer = None
            if offer:
                matched.append(offer)
            else:
                unmatched_rules.append(rule['title'])
                rule_failures[rule['title']] = diagnostics[0] if diagnostics else 'Invalid extracted value; no record published.'
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
        statuses[provider['id']]['rule_failures'] = rule_failures
    payload = {'generated_at': now(), 'offers': output, 'source_status': statuses,
               'source_403_locks': locks, 'source_403_clearances': clearances,
               'source_403_history': clearance_history,
               'refresh_scope': [p['id'] for p in cfg['providers'] if p['id'] in selected]}
    # Carry over any top-level key this checker does not own. build.py persists
    # the sitemap lastmod state into this same file, so dropping unknown keys
    # here would reset every lastmod on the next refresh run.
    for key, value in previous.items():
        if key not in payload: payload[key] = value
    DATA.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return payload

def refresh_report(payload, provider_ids=None):
    """Report fresh numeric prices separately from text matches and old records."""
    selected = set(provider_ids) if provider_ids is not None else set(payload['source_status'])
    captured = {slug for pid, status in payload['source_status'].items() if pid in selected
                for slug in status.get('captured_slugs', [])}
    prices = {offer['slug'] for offer in payload['offers']
              if offer['slug'] in captured and offer.get('price') is not None}
    retained = sum(status.get('retained_count', 0) for pid, status in payload['source_status'].items() if pid in selected)
    lines = ['# Official source refresh', '',
             f'{len(selected)} sources checked: **{len(prices)} numeric price records refreshed**, '
             f'{len(captured - prices)} text-only records captured, {retained} earlier records retained.', '',
             'A completed job does not mean that every price was updated. Retained records keep their original capture dates. '
             'Dated manual plan reviews are separate and are never redated by this job.', '',
             '| Provider | New price records | Text-only captures | Earlier records retained | Result |',
             '| --- | ---: | ---: | ---: | --- |']
    warnings = []
    for pid, status in payload['source_status'].items():
        if pid not in selected:
            continue
        fresh = set(status.get('captured_slugs', []))
        outcome = status.get('capture_status', 'not_attempted')
        detail = status.get('reason', outcome)
        if status.get('rule_failures'):
            detail += ' ' + '; '.join(f'{title}: {reason}' for title, reason in status['rule_failures'].items())
        if outcome == 'unmatched' or status.get('status') != 'evidenced' or status.get('rule_errors') or status.get('unmatched_rules'):
            warnings.append((pid, detail))
        safe_detail = detail.replace('|', '\\|').replace('\n', ' ')
        lines.append(f'| {pid} | {len(fresh & prices)} | {len(fresh - prices)} | {status.get("retained_count", 0)} | {safe_detail} |')
    return '\n'.join(lines) + '\n', warnings

def annotation_text(value):
    return str(value).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config')
    parser.add_argument('--providers', help='Comma-separated configured provider IDs; all others keep their data and dates.')
    parser.add_argument('--summary', help='Write a Markdown refresh report, e.g. GITHUB_STEP_SUMMARY.')
    parser.add_argument('--capture-dir', help='Save successful public HTML responses for explicit diagnostics.')
    args = parser.parse_args()
    selected = [value.strip() for value in args.providers.split(',') if value.strip()] if args.providers else None
    result = run(args.config, selected, args.capture_dir)
    for provider, status in result['source_status'].items():
        if selected is None or provider in selected:
            print(f"{provider}: captured={status.get('captured_count', 0)} retained={status.get('retained_count', 0)} result={status.get('capture_status')} source={status['status']}")
    report, warnings = refresh_report(result, selected)
    if args.summary:
        Path(args.summary).write_text(report, encoding='utf-8')
    for provider, reason in warnings:
        print(f'::warning title=Price refresh incomplete ({annotation_text(provider)})::{annotation_text(reason)}')
