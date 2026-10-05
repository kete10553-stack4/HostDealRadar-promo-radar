import argparse, hashlib, html, json, re, shutil, math
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from string import Template
from urllib.parse import urlsplit
from config import ROOT, load_config

TEMPLATES=ROOT/'templates'; OUT=ROOT/'site'; DATA=ROOT/'data/offers.json'; ASSETS=ROOT/'assets'
# Sitemap lastmod state lives inside the payload the refresh workflow already
# commits, so persisting it needs no change to the workflow or extra token scope.
STATE_KEY='page_lastmod'
CURRENT='current'; HISTORY=('retained','stale','expired','unverified')
OFFICIAL_FIELD_MISSING='Not published in the official wording captured for this record.'
# Why a reachable official page still yields no deterministic price rule.
BLOCKER_TEXT={'price_rendered_by_js':'The published prices on this page are rendered by JavaScript, so no figure can be read without executing scripts.',
              'billing_term_unbound':'The source response does not reliably bind the displayed prices to the selected billing term. Automated prices are withheld; dated manual checkout examples are separate.',
              'unstable_field_structure':'The page markup changes between loads, so no stable field can be bound to a named plan.',
              'no_public_price':'This official page does not publish a price publicly.',
              'login_or_region_gated':'This official page requires a login or is limited to certain regions.',
              'source_page_forbidden':'The automated checker was allowed by robots.txt but the official source returned HTTP 403.',
              'robots_check_failed':'The automated source check could not proceed because the provider’s robots.txt could not be read successfully.'}
STATE_ONLY_LEAD='Official page checked. No deterministic price rule is available, so no price is published.'
CATEGORY_DEFINITIONS={
    'Hosting':'General hosting where the current record does not carry a narrower service label.',
    'Web hosting':'Hosting intended for a website; this is a service label, not a performance tier.',
    'VPS hosting':'Virtual private server hosting.',
    'Dedicated hosting':'Dedicated server hosting.',
    'Reseller hosting':'Hosting sold for resale to clients.',
    'Managed WordPress hosting':'Hosting whose captured record identifies managed WordPress service.',
    'WordPress plugins':'A WordPress plugin or plugin membership, rather than a hosting plan.',
    'Application hosting':'A platform for running or deploying an application.',
    'Web hosting and deployment':'A record that combines website hosting and deployment.',
    'Managed cloud hosting':'A managed cloud-hosting service.',
    'Frontend cloud platform':'A platform focused on hosting or deploying frontend applications.',
    'Colocation':'Space, power, or related services for customer-owned hardware.',
    'Email hosting':'A hosted email service.',
    'Domain registration':'A record that identifies a domain registration service or price.',
    'Domains':'The generic domains label used by existing capture rules.',
    'Website builder':'A service for building and publishing a website.'
}
def e(value): return html.escape(str(value or ''), quote=True)
CURRENCY_SYMBOLS={'USD':'$','GBP':'£','EUR':'€','CAD':'CA$','AUD':'A$','NZD':'NZ$','JPY':'¥'}
def money(value, currency='USD'):
    amount=f'{float(value):,.2f}'
    symbol=CURRENCY_SYMBOLS.get(currency)
    return f'{symbol}{amount}' if symbol else f'{currency} {amount}'
def state_only_display(status, blocker):
    """Return truthful copy for a source with no published price record."""
    if status.get('status') == 'evidenced' and status.get('capture_status') == 'no_price_rule' and status.get('http_status') == 200 and status.get('visible_excerpt'):
        return STATE_ONLY_LEAD, BLOCKER_TEXT.get(blocker, ''), 'Official page checked · no deterministic price rule'
    reason = status.get('reason') or 'No source check has run yet.'
    return 'Latest source check did not complete.', reason, 'Source check did not complete'
def date_text(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z','+00:00'))
        return dt.strftime('%b ') + str(dt.day) + dt.strftime(', %Y')
    except (ValueError, AttributeError): return str(value)
def documented_plan_rows(payload):
    """Dated browser-reviewed examples, never automated current-offer records."""
    review=payload.get('browser_term_observations') or {}
    try:
        datetime.fromisoformat(review['checked_on'])
    except (KeyError, TypeError, ValueError):
        return []
    rows=[]
    for record in review.get('records', []):
        try:
            months=record['commitment_months']
            if isinstance(months, bool) or not isinstance(months, int) or months <= 0:
                continue
            total=Decimal(str(record['first_term_total']))
            monthly=Decimal(str(record['monthly_equivalent']))
            renewal=Decimal(str(record['renewal_monthly_rate']))
            if not all(value.is_finite() and value > 0 for value in (total, monthly, renewal)):
                continue
            if (total / months).quantize(Decimal('0.01')) != monthly:
                continue
            evidence=record['field_evidence']
            if any(not evidence[field]['quote'] or not evidence[field]['url'].startswith('https://')
                   for field in ('first_term_total', 'commitment_months', 'renewal_monthly_rate')):
                continue
            if record['currency'] not in {'USD','GBP','EUR','CAD','AUD','NZD','JPY','CHF','SEK','NOK','DKK'} or not record['provider'] or not record['plan']:
                continue
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        rows.append(record)
    return rows
def stamp(): return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')
# Volatile text that must not by itself count as a material page change: capture
# timestamps and the "last snapshot" date move on every run even when the terms
# shown are identical.
VOLATILE=re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z|\b[A-Z][a-z]{2} \d{1,2}, \d{4}\b')
def material(text): return VOLATILE.sub('<t>', text)
def template(name, **fields): return Template((TEMPLATES/name).read_text(encoding='utf-8')).safe_substitute(**fields)
def asset_version(path):
    """Hash normalized text so Windows CRLF checkout conversion is immaterial."""
    content=path.read_text(encoding='utf-8').replace('\r\n','\n').replace('\r','\n')
    return hashlib.sha256(content.encode('utf-8')).hexdigest()[:12]
def price(offer):
    if offer.get('price') is not None:
        return money(offer['price'], offer.get('currency','USD'))
    if offer.get('price_text'):
        return offer['price_text']
    return 'Unknown'
def period_text(offer):
    return offer.get('billing_period') or 'month'

def renewal_supported(offer, rule=None):
    """Display an existing renewal value only when its captured context supports it.

    A list/strikethrough price or an alternative billing option is not renewal.
    This is a presentation gate; it never changes the source record.
    """
    rule = rule or {}
    value = offer.get('renewal_price')
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return False
    if not all(offer.get(k) for k in ('currency', 'billing_period', 'source_url', 'fetched_at')):
        return False
    for key, base in (('renewal_currency', 'currency'), ('renewal_billing_period', 'billing_period')):
        if offer.get(key) and offer[key] != offer[base]:
            return False
    evidence = (offer.get('field_evidence') or {}).get('renewal_price', '')
    if not evidence:
        return False
    condition = offer.get('condition') or ''
    if re.search(r'billed annually or|month.to.month|comparison figure', evidence + ' ' + condition, re.I):
        return False
    currency_evidence = (offer.get('field_evidence') or {}).get('renewal_currency', '')
    currencies = set(re.findall(r'\b(?:USD|CAD|AUD|GBP|EUR)\b', evidence + ' ' + currency_evidence))
    if '£' in evidence: currencies.add('GBP')
    if '€' in evidence: currencies.add('EUR')
    if currencies and currencies != {offer['currency']}:
        return False
    units = set(re.findall(r'/(month|mo|year|yr)\b', evidence, re.I))
    units = {'month' if unit.lower() in ('mo', 'month') else 'year' for unit in units}
    if units and units != {offer['billing_period']}:
        return False
    if re.search(r'\brenew|\bthen\b', evidence, re.I):
        return True
    if any(re.search(r'\brenew', check, re.I) for check in rule.get('checks', [])):
        return True
    return bool(re.search(r'\bregistration\b', condition, re.I)
                and re.search(r'second amount.+renewal price', condition, re.I))

def initial_label(offer, rule=None):
    context = (offer.get('condition') or '') + ' ' + (rule or {}).get('anchor', '')
    if re.search(r'first[ -]month', context, re.I):
        return 'First month'
    if re.search(r'first[ -]year', context, re.I):
        return 'First year rate'
    if re.search(r'\bregistration\b', context, re.I):
        return 'Registration rate'
    return 'Introductory rate' if offer.get('kind') == 'promotion' else 'Advertised rate'

def displayed_rate(offer, field):
    value = offer.get(field)
    if value is None:
        return 'Unknown'
    currency = offer.get('currency') or 'Unknown currency'
    period = offer.get('billing_period') or 'Unknown period'
    return f'{currency} {float(value):,.2f}/{period}'

def captured_field_evidence(offer, field):
    """Return the exact stored source fragment for one published field."""
    return str((offer.get('field_evidence') or {}).get(field) or '').strip()

def currency_wording(offer, field='price'):
    """Return only the currency marker present in this field's quote.

    The normalized currency in the record is extraction output. It is not
    public evidence by itself, so a page must not print it unless the stored
    official wording contains the code or a matching currency marker.
    """
    evidence=captured_field_evidence(offer, field)
    if field == 'renewal_price' and not has_currency_marker_for_offer(offer, evidence):
        evidence = captured_field_evidence(offer, 'renewal_currency')
    currency=offer.get('currency')
    markers={
        'USD': ('USD', 'US$'),
        'CAD': ('CAD', 'C$'),
        'AUD': ('AUD', 'A$'),
        'GBP': ('GBP', '£'),
        'EUR': ('EUR', '€'),
    }.get(currency, (currency,) if currency else ())
    for marker in markers:
        if marker and (marker in evidence if not marker.isalpha()
                       else re.search(r'(?<![A-Za-z])'+re.escape(marker)+r'(?![A-Za-z])', evidence, re.I)):
            return marker
    return ''

def has_currency_marker_for_offer(offer, evidence):
    """Allow a field's currency proof to be a separately quoted source fragment."""
    if any(marker in evidence for marker in ('$', '€', '£')):
        return True
    currency=offer.get('currency')
    marker={'USD':'USD','CAD':'CAD','AUD':'AUD','GBP':'GBP','EUR':'EUR'}.get(currency)
    return bool(marker and re.search(r'(?<![A-Za-z])'+marker+r'(?![A-Za-z])', evidence, re.I))

def billing_wording(offer, field='price'):
    """Return the exact billing marker present in this field's quote."""
    evidence=captured_field_evidence(offer, field)
    patterns={
        'month': r'/(?:mo|month)\b|\bper\s+month\b|\bmonthly\b',
        'year': r'/(?:yr|year)\b|\bper\s+year\b|\bannually\b|\byearly\b',
        'hour': r'/(?:hr|hour)\b|\bper\s+hour\b|\bhourly\b',
        'site': r'/(?:site)\b|\bper\s+site\b',
        'server': r'/(?:server)\b|\bper\s+server\b',
    }
    match=re.search(patterns.get(offer.get('billing_period'), r'(?!x)x'), evidence, re.I)
    return match.group(0) if match else ''

def official_rate_wording(offer, field):
    """Publish the exact official price phrase instead of reconstructed terms."""
    return captured_field_evidence(offer, field) or OFFICIAL_FIELD_MISSING

def sourced_term(label, value, evidence):
    """Render a provider-page field only with the official fragment behind it.

    A missing fragment is a missing public field, not an invitation to infer a
    value from another plan, billing period, or page.
    """
    if not evidence:
        return (f'<div><dt>{e(label)}</dt><dd>{e(OFFICIAL_FIELD_MISSING)}</dd></div>')
    return (f'<div><dt>{e(label)}</dt><dd>{e(value)}'
            f'<small class="capture">Official page wording: “{e(evidence)}”</small></dd></div>')

def public_terms(text):
    # Source refreshes can restore old promotional prose. Suppress claims in
    # presentation without rewriting captured data or the extraction rules.
    text = re.sub(r';[^;]*(?:discount[^;]*%|sav(?:e|ing)|reduce|lower)[^;]*', '', text, flags=re.I)
    if re.search(r'\bsav(?:e|es|ed|ing|ings)\b', text, re.I):
        return 'See the linked official page for the plan terms.'
    return text

def rate_source(offer):
    return (f'<small class="capture"><a href="{e(offer.get("source_url"))}" rel="noopener noreferrer">Official source</a>'
            f' · Captured {e(offer.get("fetched_at") or "Unknown")}</small>')

def rate_pair(offer, rule=None):
    source_ready=bool(offer.get('source_url') and offer.get('fetched_at'))
    initial=captured_field_evidence(offer, 'price')
    if offer.get('price') is None or not initial or not source_ready:
        initial='Unknown'
        initial_source=''
    else:
        initial_source=rate_source(offer)
    if renewal_supported(offer, rule):
        renewal=captured_field_evidence(offer, 'renewal_price')
        renewal_currency=captured_field_evidence(offer, 'renewal_currency')
        currency_note=(f'<small class="capture">Currency on source: “{e(renewal_currency)}”</small>'
                       if renewal_currency else '')
        renewal_source=rate_source(offer)+currency_note
    else:
        renewal='Unknown'
        renewal_source=''
    return ('<div class="source-bar">'
            f'<div><strong>{e(initial_label(offer, rule))}</strong><br><h3>{e(initial)}</h3>{initial_source}</div>'
            f'<div><strong>Renewal rate</strong><br><h3>{e(renewal)}</h3>{renewal_source}</div>'
            '</div>')

def complete_three_terms(offer, rule=None):
    """Only exact official wording for one plan and initial term qualifies.

    The advertised monthly rate is not evidence of the amount paid upfront.
    No total or monthly equivalent is computed from another field here.
    """
    return bool(offer.get('upfront_total') is not None
                and captured_field_evidence(offer, 'upfront_total')
                and offer.get('price') is not None
                and captured_field_evidence(offer, 'price')
                and billing_wording(offer, 'price')
                and offer.get('commitment_months')
                and captured_field_evidence(offer, 'commitment_months')
                and offer.get('billing_period') == 'month'
                and renewal_supported(offer, rule)
                and billing_wording(offer, 'renewal_price'))

def category_names(records):
    """Keep the configured record order while exposing its existing labels."""
    names=[]
    for record in records:
        category=record.get('category') or 'Unknown'
        if category not in names:
            names.append(category)
    return names

def provider_summary(records, earlier=None):
    record=next(iter(records), None)
    prefix='Captured plan example'
    if record is None:
        return 'No current source record is published.'
    return (prefix+': '+str(record.get('title') or 'Unknown')+
            '. Service label: '+str(record.get('category') or 'Unknown')+
            '. Captured '+str(record.get('fetched_at') or 'Unknown')+'.')

def offer_name(provider_name, title):
    """Give each record page a visitor-readable, provider-specific identity.

    Plan names such as "Basic" and "Pro" are common across providers.  The
    provider is already a captured field, so including it does not add an
    editorial claim or alter the underlying record.
    """
    if str(title).lower().startswith(str(provider_name).lower()):
        return str(title)
    return f'{provider_name} {title}'

def deal_description(provider_name, offer):
    """Describe only terms that this record can actually show."""
    plan=offer_name(provider_name, offer['title'])
    if offer.get('price') is not None:
        return (f'Official {plan} terms: captured price, billing period, '
                'renewal rate when stated, source link, and source-check status.')
    return (f'Official {plan} terms: source link, captured billing details when '
            'available, and source-check status. No price is published here.')

def provider_description(provider_name, current_records, earlier_records):
    """Keep provider-page metadata specific to its published record state."""
    if current_records:
        return (f'Official {provider_name} terms checked by HostDealRadar. '
                f'{len(current_records)} current record(s) list source, price fields, and status.')
    return (f'Latest official source-check status for {provider_name}. '
            'No current source record is published when the checked source cannot support one.')

def category_guide(records):
    items=[]
    for category in category_names(records):
        definition=CATEGORY_DEFINITIONS.get(category,
            'Definition: Unknown.')
        items.append(f'<li><strong>{e(category)}</strong>: {e(definition)}</li>')
    return ("<h2 id=\"service-labels\">Current service labels</h2>"
            "<p>Each captured record keeps one existing HostDealRadar service label. These labels describe the captured service type; they are not provider claims, performance scores, or recommendations.</p>"
            "<ul>"+''.join(items)+"</ul>"
            "<p><strong>Limits:</strong> some existing labels overlap, including Hosting and Web hosting and Domains and Domain registration. Labels vary in specificity and do not establish matching resources or performance. They remain unchanged. Definitions follow the first appearance of each label in the stored records, not a ranking.</p>")

def featured_renewals(current, rules):
    # Compare differences only inside one currency/unit/service group. Prefer
    # explicit first-month offers, whose duration is unambiguous to a visitor.
    groups = defaultdict(list)
    for offer in current:
        rule = rules.get((offer['provider'], offer['title']), {})
        if (offer.get('price') is not None and renewal_supported(offer, rule)
                and currency_wording(offer, 'price') and billing_wording(offer, 'price')
                and currency_wording(offer, 'renewal_price') and billing_wording(offer, 'renewal_price')
                and offer['renewal_price'] > offer['price']):
            groups[(offer['currency'], offer['billing_period'], offer.get('category', 'Unknown'))].append(offer)
    first_month = {key: [o for o in group if initial_label(o, rules.get((o['provider'], o['title']))) == 'First month']
                   for key, group in groups.items()}
    groups = {key: group for key, group in first_month.items() if group} or groups
    if not groups:
        return []
    key = min(groups, key=lambda key: (-len(groups[key]), key))
    return sorted(groups[key], key=lambda o: (-(o['renewal_price'] - o['price']), o['slug']))[:3]
def schema_offer(offer, canonical, name=None):
    item={'@type':'Offer','name':name or offer['title'],'url':canonical}
    # A schema currency claim follows the same evidence rule as visible text.
    if offer.get('price') is not None and currency_wording(offer, 'price'):
        item.update({'price':str(offer['price']),'priceCurrency':offer['currency']})
    if offer.get('valid_until'): item['priceValidUntil']=offer['valid_until']
    return item
def record_state(offer, statuses, settings):
    """Per-record publication state.

    `current` is the only state that may appear in the current offers list, in
    current-price structured data, or in the current comparison table. A record
    kept from an earlier run is `retained` even when its provider is `checked`,
    because provider-level status cannot say which slug failed this run.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    if offer.get('valid_until') and offer['valid_until'] < today:
        return 'expired', 'Expired on ' + offer['valid_until'] + '. See the official page for current terms.'
    if offer.get('kind') == 'promotion' and not offer.get('valid_until'):
        return 'unverified', 'Promotional end date not verified. Last captured ' + offer['fetched_at'] + '. This is not a current offer.'
    status = statuses.get(offer['provider'], {}) or {}
    captured_slugs = status.get('captured_slugs')
    if captured_slugs is not None and offer['slug'] not in captured_slugs:
        return 'retained', 'Not reconfirmed in the latest source check. Last captured ' + offer['fetched_at'] + '.'
    if (status.get('status') != 'evidenced' or status.get('capture_status') != 'matched'
            or status.get('http_status') != 200 or not status.get('visible_excerpt')):
        return 'stale', 'Latest source check did not complete. Last captured ' + offer['fetched_at'] + '.'
    try:
        captured = datetime.fromisoformat(offer['fetched_at'].replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return 'stale', 'Capture time could not be read.'
    if (datetime.now(timezone.utc) - captured).total_seconds() > settings['fresh_hours'] * 3600:
        return 'stale', 'Needs recheck. Last captured ' + offer['fetched_at'] + '.'
    return 'current', 'Captured ' + offer['fetched_at'] + '. Confirm current terms with the provider.'

def agent_record(offer, provider, state):
    """Return the bounded, public representation behind the read-only API."""
    return {
        'id': offer['slug'], 'provider': {'id': provider['id'], 'name': provider['name']},
        'title': offer['title'], 'category': offer.get('category', 'Unknown'),
        'record_state': state, 'listing_type': offer.get('kind', 'regular_price'),
        'price': offer.get('price'), 'price_wording': captured_field_evidence(offer, 'price') or None,
        'currency': (offer.get('currency') if currency_wording(offer, 'price') else None),
        'currency_wording': currency_wording(offer, 'price') or None,
        'billing_period': (offer.get('billing_period') if billing_wording(offer, 'price') else None),
        'billing_wording': billing_wording(offer, 'price') or None,
        'commitment_months': offer.get('commitment_months'),
        'renewal_price': offer.get('renewal_price'), 'coupon_code': offer.get('coupon_code'),
        'valid_until': offer.get('valid_until'), 'source_url': offer['source_url'],
        'captured_at': offer['fetched_at'], 'record_url': '/providers/' + provider['id'] + '/#record-' + offer['slug'],
        'limitations': 'Prices, eligibility, checkout totals, and renewal terms must be confirmed with the provider.'
    }

def agent_worker(withdrawn_provider_ids=()):
    """Return the Pages Worker for public agent discovery and read-only lookup."""
    worker = r'''const LINK_HEADER = [
  '</.well-known/api-catalog>; rel="api-catalog"',
  '</openapi.json>; rel="service-desc"',
  '</ai/>; rel="service-doc"',
  '</.well-known/ai-catalog.json>; rel="describedby"'
].join(', ');
const WITHDRAWN_PROVIDER_PATHS = new Set(/*WITHDRAWN_PROVIDER_PATHS*/);
function mergedResponse(response, additions = {}) {
  const headers = new Headers(response.headers);
  for (const [name, value] of Object.entries(additions)) headers.set(name, value);
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}
function json(body, status = 200, additions = {}) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', 'access-control-allow-origin': '*', ...additions } });
}
async function asset(env, request, pathname) {
  const url = new URL(request.url); url.pathname = pathname; url.search = '';
  return env.ASSETS.fetch(new Request(url.toString(), { method: 'GET', headers: request.headers }));
}
function wantsMarkdown(request) { return (request.headers.get('accept') || '').toLowerCase().includes('text/markdown'); }
function unavailable() {
  return json({ error: 'temporarily_unavailable', status: 'under_construction', available: false, capabilities_status: 'planned_contract_only', message: 'Coming soon; authentication is not available.', launch_date: null }, 503, { 'www-authenticate': 'Bearer error="temporarily_unavailable"' });
}
async function publicLookup(env, provider, slug) {
  if ((!provider && !slug) || (provider && slug)) return { status: 400, body: { error: 'invalid_query', message: 'Supply exactly one of provider or slug.' } };
  const records = await (await asset(env, new Request('https://hostdealradar.com/agent-data.json'), '/agent-data.json')).json();
  const matches = slug ? records.filter((record) => record.id === slug) : records.filter((record) => record.provider.id === provider || record.provider.name.toLowerCase() === provider);
  if (!matches.length) return { status: 404, body: { status: 'not_found', query: slug ? { slug } : { provider }, records: [], message: 'No public HostDealRadar record matched this exact identifier.' } };
  return { status: 200, body: { status: 'ok', query: slug ? { slug } : { provider }, record_count: Math.min(matches.length, 25), records: matches.slice(0, 25), limitations: 'This read-only API reports published source records. It does not test checkout, availability, eligibility, or provider performance.' } };
}
function mcpReply(id, result) { return json({ jsonrpc: '2.0', id: id === undefined ? null : id, result }); }
function mcpError(id, code, message) { return json({ jsonrpc: '2.0', id: id === undefined ? null : id, error: { code, message } }); }
async function mcp(request, env) {
  if (request.method !== 'POST') return json({ error: 'method_not_allowed', message: 'Use POST for this Streamable HTTP MCP endpoint.' }, 405, { allow: 'POST' });
  let message; try { message = await request.json(); } catch { return mcpError(null, -32700, 'Parse error'); }
  if (!message || message.jsonrpc !== '2.0' || typeof message.method !== 'string') return mcpError(message && message.id, -32600, 'Invalid Request');
  if (message.method === 'initialize') return mcpReply(message.id, { protocolVersion: '2025-03-26', capabilities: { tools: {} }, serverInfo: { name: 'hostdealradar-public-lookup', version: '1.0.0' } });
  if (message.method === 'tools/list') return mcpReply(message.id, { tools: [{ name: 'hostdealradar_lookup', description: 'Read bounded public HostDealRadar records by an exact provider ID or record slug.', inputSchema: { type: 'object', additionalProperties: false, oneOf: [{ required: ['provider'], properties: { provider: { type: 'string', description: 'Exact provider ID or name.' } } }, { required: ['slug'], properties: { slug: { type: 'string', description: 'Exact public record ID.' } } }] } }] });
  if (message.method === 'tools/call') {
    const params = message.params || {}; if (params.name !== 'hostdealradar_lookup') return mcpError(message.id, -32602, 'Unknown tool');
    const args = params.arguments || {}; const result = await publicLookup(env, typeof args.provider === 'string' ? args.provider.trim().toLowerCase() : '', typeof args.slug === 'string' ? args.slug.trim() : '');
    return mcpReply(message.id, { content: [{ type: 'text', text: JSON.stringify(result.body) }], structuredContent: result.body, isError: result.status !== 200 });
  }
  return mcpError(message.id, -32601, 'Method not found');
}
export default {
  async fetch(request, env) {
    const url = new URL(request.url); const path = url.pathname;
    const normalizedPath = path.endsWith('/') ? path : path + '/';
    if (WITHDRAWN_PROVIDER_PATHS.has(normalizedPath)) {
      return new Response('This provider page is not published because no verifiable official source record is available.', { status: 410, headers: { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' } });
    }
    const oldRecord = path.match(/^\/deals\/([a-z0-9-]+)\/?$/);
    if (oldRecord) {
      const result = await publicLookup(env, '', oldRecord[1]);
      if (result.status === 200) return Response.redirect(new URL(result.body.records[0].record_url, url.origin), 301);
    }
    if (path === '/api/agent/lookup') {
      if (request.method !== 'GET') return json({ error: 'method_not_allowed', message: 'Use GET for this read-only endpoint.' }, 405, { allow: 'GET' });
      const provider = (url.searchParams.get('provider') || '').trim().toLowerCase(); const slug = (url.searchParams.get('slug') || '').trim();
      const result = await publicLookup(env, provider, slug); return json(result.body, result.status);
    }
    if (path === '/mcp') return mcp(request, env);
    if (['/agent-auth/authorize', '/agent-auth/token', '/agent-auth/register', '/agent-auth/claim', '/agent-auth/revoke'].includes(path)) return unavailable();
    const specialAssets = { '/ai/': ['/ai/index.md', 'text/markdown; charset=utf-8'], '/ai/index.ilang': ['/ai/index.ilang', 'text/plain; charset=utf-8'], '/.well-known/api-catalog': ['/.well-known/api-catalog.json', 'application/linkset+json; charset=utf-8'], '/.well-known/oauth-authorization-server': ['/.well-known/oauth-authorization-server', 'application/json; charset=utf-8'], '/.well-known/oauth-protected-resource': ['/.well-known/oauth-protected-resource', 'application/json; charset=utf-8'], '/.well-known/jwks.json': ['/.well-known/jwks.json', 'application/json; charset=utf-8'], '/.well-known/mcp/server-card.json': ['/.well-known/mcp/server-card.json', 'application/json; charset=utf-8'] };
    if (specialAssets[path]) { const [assetPath, contentType] = specialAssets[path]; return mergedResponse(await asset(env, request, assetPath), { 'content-type': contentType, 'access-control-allow-origin': '*' }); }
    if (path === '/' && wantsMarkdown(request)) return mergedResponse(await asset(env, request, '/ai/index.md'), { 'content-type': 'text/markdown; charset=utf-8', 'vary': 'Accept', 'link': LINK_HEADER });
    const response = await env.ASSETS.fetch(request);
    if (path === '/') return mergedResponse(response, { 'vary': 'Accept', 'link': LINK_HEADER });
    if (path === '/.well-known/ai-catalog.json') return mergedResponse(response, { 'content-type': 'application/json; charset=utf-8', 'access-control-allow-origin': '*' });
    return response;
  },
};
'''
    paths=sorted('/providers/'+provider_id+'/' for provider_id in withdrawn_provider_ids)
    return worker.replace('/*WITHDRAWN_PROVIDER_PATHS*/', json.dumps(paths))

def build(config_path=None, output=None):
    global OUT
    OUT=Path(output) if output else ROOT/'site'
    cfg=load_config(config_path); domain=cfg['site']['domain'].rstrip('/'); payload=json.loads(DATA.read_text(encoding='utf-8'))
    style_version=asset_version(ASSETS/'style.css')
    wp_engine_kinsta_css_version=asset_version(ASSETS/'wp-engine-kinsta.css')
    ga4_measurement_id=cfg['settings'].get('ga4_measurement_id', '')
    if ga4_measurement_id and not re.fullmatch(r'G-[A-Z0-9]+', ga4_measurement_id):
        raise ValueError('Invalid GA4 measurement ID in SETTINGS')
    analytics_head=''
    analytics_settings=''
    if ga4_measurement_id:
        analytics_js_version=asset_version(ASSETS/'analytics.js')
        analytics_css_version=asset_version(ASSETS/'analytics.css')
        analytics_head=(f'<link rel="stylesheet" href="/assets/analytics.css?v={analytics_css_version}">'
                        f'<script src="/assets/analytics.js?v={analytics_js_version}" data-ga4-id="{e(ga4_measurement_id)}" defer></script>')
        analytics_settings='<button class="analytics-settings" type="button" data-analytics-settings>Analytics cookie settings</button>'
    byid={p['id']:p for p in cfg['providers']}; statuses=payload.get('source_status',{})
    rules={(r['provider'], r.get('title')): r for r in cfg['extractors']}
    # A provider whose official page was opened but yields no deterministic rule
    # is published as a state-only source: it states the reason and shows no figure.
    state_only={r['provider'] for r in cfg['extractors'] if r.get('mode')=='availability_only'}
    state_only={pid for pid in state_only if not any(r.get('mode')!='availability_only' for r in cfg['extractors'] if r.get('provider')==pid)}
    blockers={r['provider']:r.get('blocker') for r in cfg['extractors'] if r.get('mode')=='availability_only'}
    offers=[o for o in payload.get('offers',[]) if o.get('provider') in byid]
    states={o['slug']:record_state(o,statuses,cfg['settings']) for o in offers}
    current=[o for o in offers if states[o['slug']][0]==CURRENT]
    history=[o for o in offers if states[o['slug']][0] in HISTORY]
    providers=cfg['providers']
    guide_templates={'godaddy':'provider-guide.html','namecheap':'namecheap-provider-guide.html','cloudways':'cloudways-provider-guide.html','digitalocean':'digitalocean-provider-guide.html','hostinger':'hostinger-provider-guide.html','a2-hosting':'a2-provider-guide.html','wp-engine':'wp-engine-provider-guide.html'}
    # A source-only provider needs either a concrete official-page observation or
    # an existing editorial guide. Otherwise it has no public page to publish.
    reviewed_providers={record['provider'] for record in documented_plan_rows(payload)}
    unpublished_source_only={pid for pid in state_only if pid not in cfg['browser_observations'] and pid not in guide_templates and pid not in reviewed_providers}
    public_providers=[p for p in providers if p['id'] not in unpublished_source_only]
    rendered={}
    if OUT.exists(): shutil.rmtree(OUT)
    shutil.copytree(ASSETS, OUT/'assets')
    def write(path, text):
        path=Path(path); target=OUT/path; target.parent.mkdir(parents=True,exist_ok=True); target.write_text(text,encoding='utf-8')
        posix=path.as_posix()
        if posix.endswith('index.html'): rendered['/'+posix[:-len('index.html')]]=text
        elif posix.endswith('.html'): rendered['/'+posix]=text
    def write_bytes(path, data):
        path=Path(path); target=OUT/path; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(data)
    def page(title, description, canonical, content, schema, head_extra=''):
        return template('base.html',title=e(title),description=e(description),canonical=e(canonical),brand=e(cfg['site']['brand']),tagline=e(cfg['settings']['tagline']),repo=e(cfg['settings']['repo_url']),social_image='',head_extra=head_extra,analytics=analytics_head,analytics_settings=analytics_settings,footer_status=e('Data source checks are automated.'),content=content,schema=json.dumps(schema,separators=(',',':')),style_version=style_version)
    def card(o, historical=False):
        p=byid[o['provider']]; state, message=states[o['slug']]; terms=[]
        rule=rules.get((o['provider'], o['title']), {})
        if o.get('commitment_months') and captured_field_evidence(o,'commitment_months'):
            terms.append(f"{o['commitment_months']}-month term")
        label='Official price' if o.get('kind')=='regular_price' else 'Promotion'
        if historical: label={'retained':'Earlier record','stale':'Needs recheck','expired':'Expired','unverified':'Unverified'}.get(state,state.title())
        cls='card history' if historical else 'card'
        detail=f'/providers/{e(p["id"])}/#record-{e(o["slug"])}'
        condition=public_terms(o.get('condition') or '')
        return f'''<article class="{cls}"><div class="card-top"><span class="provider-name">{e(p['name'])}</span><span class="tag">{label}</span></div><h3><a href="{detail}">{e(o['title'])}</a></h3>{rate_pair(o, rule)}<p class="summary">{e(o.get('category','Hosting'))}</p>{f'<p class="small">{e(condition)}</p>' if condition else ''}{f'<dl>{''.join(f'<div><dt>{e(x.split(" ")[0])}</dt><dd>{e(x)}</dd></div>' for x in terms)}</dl>' if terms else ''}<a class="button" href="{detail}">View terms</a><p class="capture">{e(message)}</p></article>'''
    def record_detail(o):
        p=byid[o['provider']]; state, message=states[o['slug']]
        rule=rules.get((o['provider'], o['title']), {})
        advertised=official_rate_wording(o,'price')
        renewal_evidence=captured_field_evidence(o,'renewal_price') if renewal_supported(o,rule) else ''
        price_evidence=captured_field_evidence(o,'price')
        evidence_fields=[
            ('Commitment',captured_field_evidence(o,'commitment_months') if o.get('commitment_months') else ''),
            ('Renewal price',renewal_evidence),
            ('Renewal currency',captured_field_evidence(o,'renewal_currency')),
            ('Coupon code',captured_field_evidence(o,'coupon_code') if o.get('coupon_code') else ''),
            ('Valid until',captured_field_evidence(o,'valid_until') if o.get('valid_until') else ''),
        ]
        grouped=defaultdict(list)
        for label, quote in evidence_fields:
            if quote:
                grouped[quote].append(label)
        # The price evidence is the first-screen answer. If the same exact
        # quote also supports another field, do not print it again below.
        grouped.pop(price_evidence, None)
        source_terms=[]
        for quote, labels in grouped.items():
            source_terms.append(f'<div><dt>{e(" / ".join(labels))}</dt><dd>“{e(quote)}”</dd></div>')
        metadata=[('Service label',o.get('category','Hosting')),
                  ('Listing type','Regular price; no discount claimed' if o.get('kind')=='regular_price' else 'Promotion')]
        terms_html=''.join(source_terms)+''.join(f'<div><dt>{e(key)}</dt><dd>{e(value)}</dd></div>' for key,value in metadata)
        return (f'<section class="record-detail" id="record-{e(o["slug"])}"><h2>{e(offer_name(p["name"],o["title"]))}</h2>'
                f'<p class="lead"><strong>Official price wording:</strong> “{e(advertised)}”</p>'
                f'<p class="record-state state-{e(state)}"><strong>{e(state.title())} record.</strong> Checked {e(date_text(o["fetched_at"]))}. '
                f'<a href="{e(o["source_url"])}" rel="noopener noreferrer">Open official source ↗</a></p>'
                f'<dl class="terms">{terms_html}</dl></section>')
    def source_observation_record(p, observation, status):
        terms=[('Record type','Official source observation; not a current offer'),
               ('Official price','Not publicly disclosed on the observed page'),
               ('Promotion status','Not checked by this observation'),
               ('Observed at',observation['observed_at']),
               ('Automated source status',state_only_display(status, blockers.get(p['id']))[0])]
        terms_html=''.join(f'<div><dt>{e(key)}</dt><dd>{e(value)}</dd></div>' for key,value in terms)
        return (f'<section class="record-detail source-observation" id="source-observation"><h2>Official source record</h2>'
                f'<p>Official page text: “{e(observation["quote"])}”</p><dl class="terms">{terms_html}</dl>'
                f'<div class="source-note"><strong>Where this comes from</strong><p>{e(observation["context"])}</p>'
                f'<a href="{e(observation["url"])}" rel="noopener noreferrer">View the official page ↗</a></div>'
                '<p class="small">A public price not shown on this page does not mean that no promotion exists. '
                'This record does not change the automated source-check status or establish a current price.</p></section>')
    current_by_provider=defaultdict(list)
    for offer in current:
        current_by_provider[offer['provider']].append(offer)
    history_by_provider=defaultdict(list)
    for offer in history:
        history_by_provider[offer['provider']].append(offer)
    def tile(p):
        count=sum(o['provider']==p['id'] for o in current)
        status = statuses.get(p['id'], {})
        if p['id'] in state_only:
            label=state_only_display(status, blockers.get(p['id']))[2]
        elif status.get('status') == 'evidenced' and status.get('capture_status') == 'matched' and status.get('http_status') == 200 and status.get('visible_excerpt'):
            label=f'{count} current listings →' if count else 'Source-check details →'
        elif status.get('status') == 'evidenced' and status.get('capture_status') == 'unmatched':
            label='Latest source check produced no published record →'
        else:
            label='Latest source check did not complete →'
        return f'<a class="provider-tile" href="/providers/{e(p["id"])}/"><strong>{e(p["name"])}</strong><p>{e(provider_summary(current_by_provider[p["id"]], history_by_provider[p["id"]]))}</p><span>{e(label)}</span></a>'
    def directory_tile(p):
        pid=p['id']
        records=current_by_provider[pid]
        status=statuses.get(pid, {})
        if records:
            summary=provider_summary(records)
            label=f'{len(records)} current record(s) →'
        elif pid in state_only:
            heading, summary, _=state_only_display(status, blockers.get(pid))
            label=heading+' →'
        elif status.get('capture_status')=='unmatched':
            summary='The official page was read, but this check did not capture a verifiable current price.'
            label='No current record →'
        elif status.get('status')=='evidenced' and status.get('capture_status')=='matched':
            summary='The official page was read, but no captured record qualifies as a current offer.'
            label='No current record →'
        else:
            summary=status.get('reason') or 'No source check has run yet.'
            label='Latest source check did not complete →'
        return (f'<a class="provider-tile" href="/providers/{e(pid)}/"><strong>{e(p["name"])}</strong>'
                f'<p>{e(summary)}</p><span>{e(label)}</span></a>')
    provider_tiles=''.join(tile(p) for p in public_providers)
    complete=[o for o in current if complete_three_terms(o, rules.get((o['provider'],o['title']), {}))]
    reviewed=[record for record in documented_plan_rows(payload) if record['provider'] in byid]
    def record_review_date(record):
        return date_text(record.get('checked_on') or (payload.get('browser_term_observations') or {}).get('checked_on'))
    def reviewed_link(record, field, label):
        source=record['field_evidence'][field]['url']
        return f'<a href="{e(source)}" rel="noopener noreferrer">{e(label)}</a>'
    def reviewed_anchor(record):
        return 'plan-' + record.get('plan_id', record['provider'])
    def reviewed_card(record):
        name=byid[record['provider']]['name']
        total=money(record['first_term_total'],record['currency'])
        monthly=money(record['monthly_equivalent'],record['currency'])
        renewal=money(record['renewal_monthly_rate'],record['currency'])
        months=record['commitment_months']
        month_label='month' if months == 1 else 'months'
        return (f'<article class="card"><div class="card-top"><span class="provider-name">{e(name)}</span>'
                '<span class="tag reference">Manual review</span></div>'
                f'<h3>{e(record["plan"])}</h3><p class="price">{reviewed_link(record,"first_term_total",total)}'
                f'<span class="period"> upfront for {months} {month_label}</span></p>'
                f'<dl><div><dt>Commitment</dt><dd>{reviewed_link(record,"commitment_months",str(months)+" "+month_label)}</dd></div>'
                f'<div><dt>Monthly equivalent</dt><dd>{monthly} (calculated: {total} ÷ {months})'
                +(f'<small class="capture">{e(record["monthly_note"])}</small>' if record.get('monthly_note') else '')
                +'</dd></div>'
                f'<div><dt>Renewal monthly rate</dt><dd>{reviewed_link(record,"renewal_monthly_rate",renewal+"/mo")}</dd></div></dl>'
                f'<p class="capture">Official pages checked {e(record_review_date(record))} PT. Confirm the latest price and tax at checkout.'
                + (' The upfront total was seen in an official cart after plan selection; cart contents may vary by session.' if record.get('entry_url') else '')
                + '</p></article>')
    # A dated observation may support only one or two terms. Keep those
    # source-linked fields visible without putting the row in a comparison.
    partial=[record for record in (payload.get('browser_term_observations') or {}).get('records', [])
             if record.get('provider') in byid and record.get('plan')
             and record not in reviewed and record.get('field_evidence')]
    def partial_card(record):
        fields=[]
        for field, label in (('first_term_total','First-term total'),
                             ('commitment_months','Billing or renewal term'),
                             ('renewal_monthly_rate','Renewal rate')):
            evidence=(record.get('field_evidence') or {}).get(field) or {}
            value=record.get(field)
            if value is None or not evidence.get('quote') or not str(evidence.get('url','')).startswith('https://'):
                continue
            fields.append(f'<div><dt>{e(label)}</dt><dd><a href="{e(evidence["url"])}" rel="noopener noreferrer">'
                          f'“{e(evidence["quote"])}”</a></dd></div>')
        if not fields:
            return ''
        return (f'<article class="card"><div class="card-top"><span class="provider-name">{e(byid[record["provider"]]["name"])}</span>'
                '<span class="tag reference">Dated source note</span></div>'
                f'<h3>{e(record["plan"])}</h3><dl>{"".join(fields)}</dl>'
                f'<p class="capture">Official pages checked {e(record_review_date(record))} PT. This is not a complete plan comparison or checkout test.</p></article>')
    partial_cards=''.join(partial_card(record) for record in partial)
    review_intro=(f'{len(reviewed)} plan examples have an official upfront total, commitment term, and renewal monthly rate. '
                  'The monthly equivalent is calculated from the first two figures. Each example shows its own official-page check date; '
                  'these dated examples are not a live checkout test, savings claim, or price ranking. '
                  'A linked cart may need the same plan selected before its total appears.')
    home_reviewed=('<section class="wrap section"><div class="section-head"><div><div class="eyebrow">DOCUMENTED PLAN TERMS</div>'
                   '<h2>Upfront total and renewal rate</h2></div><a class="text-link" href="/compare/">Compare the terms →</a></div>'
                   f'<p class="muted">{e(review_intro)} Each source value links to its official page.</p><div class="cards">'
                   +''.join(reviewed_card(record) for record in reviewed)+'</div></section>') if reviewed else ''
    home=template('index.html',month=datetime.now().strftime('%B %Y'),provider_count=len(providers),reviewed_section=home_reviewed,partial_cards=partial_cards,providers=provider_tiles)
    # The homepage lists different services; its entries are navigation targets,
    # not merchant Offers for products that HostDealRadar sells.
    current_provider_ids={o['provider'] for o in current}
    home_schema={'@context':'https://schema.org','@graph':[
        {'@type':'WebSite','@id':domain+'/#website','name':cfg['site']['brand'],'url':domain+'/','inLanguage':'en-US','publisher':{'@id':domain+'/#organization'}},
        {'@type':'Organization','@id':domain+'/#organization','name':cfg['site']['brand'],'url':domain+'/','sameAs':[cfg['settings']['repo_url']]},
        {'@type':'ItemList','name':'Documented hosting plan comparisons','itemListElement':[{'@type':'ListItem','position':i+1,'item':{'@type':'WebPage','name':byid[record['provider']]['name']+' '+record['plan'],'url':domain+'/compare/#'+reviewed_anchor(record)}} for i,record in enumerate(reviewed)]}
    ]}
    write(Path('index.html'),page('HostDealRadar | Official hosting offers', 'Official hosting offers with source-check status and provider links.',domain+'/',home,home_schema,head_extra="<meta name='impact-site-verification' value='9f3ff63a-c432-478f-8859-af77a6120cbb'>"))
    with_current=[p for p in public_providers if current_by_provider[p['id']]]
    without_current=[p for p in public_providers if not current_by_provider[p['id']]]
    provider_listing=(
        '<section class="wrap section"><div class="eyebrow">OFFICIAL SOURCES</div><h1>Providers we check</h1>'
        '<p class="lead">Browse providers with current source records first. Other checked sources remain below with the result of their latest check. Within each group, providers follow the configured source-list order, not a recommendation or a ranking of price, quality, or value. <a href="/methodology/#service-labels">Read service-label definitions and limits</a>.</p>'
        f'<h2 id="current-records">Providers with current records ({len(with_current)})</h2>'
        '<div class="provider-grid">'+''.join(directory_tile(p) for p in with_current)+'</div></section>'
        '<section class="wrap section"><h2 id="without-current-records">Sources without current records '
        f'({len(without_current)})</h2><p>These provider pages remain available for source-check details, '
        'but any earlier records are kept as history, not displayed as current offers. These pages are not listed in the sitemap.</p>'
        '<div class="provider-grid">'+''.join(directory_tile(p) for p in without_current)+'</div></section>')
    write(Path('providers/index.html'),page('Providers | HostDealRadar','Hosting providers and their latest source-check status.',domain+'/providers/',provider_listing,{'@context':'https://schema.org','@type':'CollectionPage','name':'Providers'}))
    for p in public_providers:
        mine=[o for o in offers if o['provider']==p['id']]
        po=[o for o in mine if states[o['slug']][0]==CURRENT]
        manual_plans=[record for record in reviewed if record['provider']==p['id']]
        status=statuses.get(p['id'],{'status':'not checked','reason':'No source check has run yet.'})
        if p['id'] in state_only:
            status_text, detail, _ = state_only_display(status, blockers.get(p['id']))
            current_html='<div class="empty"><h3>'+e(status_text)+'</h3><p>'+e(detail)+'</p></div>'
            if manual_plans:
                links='; '.join(f'<a href="/compare/#{e(reviewed_anchor(record))}">{e(record["plan"])}</a>' for record in manual_plans)
                current_html+='<p>Dated manual plan-price records, separate from automated offers and coupon codes: '+links+'.</p>'
            observation=cfg['browser_observations'].get(p['id'])
            if observation:
                current_html+=source_observation_record(p, observation, status)
        else:
            source_matched=(status['status']=='evidenced' and status.get('capture_status')=='matched'
                            and status.get('http_status')==200 and status.get('visible_excerpt'))
            display_reason=status['reason']
            retained_without_capture=(not po and status.get('capture_status')=='unmatched'
                                      and status.get('retained_count',0))
            if retained_without_capture:
                display_reason=('The official page was read, but this check did not capture a verifiable current price. '
                                'Earlier records are kept as history and are not shown as current offers.')
            status_text=('Official page read; capture rules matched.' if source_matched else
                         'Official page read; no verifiable current price captured in this check.' if retained_without_capture else
                         e(display_reason))
            if po:
                current_html=''
            elif manual_plans:
                unverified_promotions=[o for o in mine if states[o['slug']][0]=='unverified' and o.get('kind')=='promotion']
                if unverified_promotions:
                    detail=('The automated capture matched the official plan cards, but their promotional end dates were not verified, '
                            'so those records are not labeled current. A separate dated manual review records package pricing and billing terms; '
                            'it is a plan-price record, not a current automated offer or coupon-code record.')
                else:
                    detail=('A dated manual review records package pricing and billing terms separately from automated current offers. '
                            'It is a plan-price record, not a coupon-code record.')
                links='; '.join(f'<a href="/compare/#{e(reviewed_anchor(record))}">{e(record["plan"])}</a>' for record in manual_plans)
                current_html=('<div class="empty"><h3>No current automated offer is published for this source</h3><p>'+e(detail)+'</p>'
                              '<p>Dated manual plan-price record'+('s' if len(manual_plans)!=1 else '')+': '+links+'.</p></div>')
            else:
                detail=('Official page read, but no captured record qualifies as a current offer.'
                        if source_matched else display_reason)
                current_html='<div class="empty"><h3>No current offer is published for this source</h3><p>'+e(detail)+'</p></div>'
        focus=cfg['page_focus'].get(p['id'])
        if po:
            note=provider_summary(po)+' Current source records follow stored record order; this is not a recommendation or a ranking of price, quality, or value.'
        elif manual_plans:
            note=('This status covers automated current offers. The dated manual plan-price record linked above documents the package price and billing term; '
                  'it is separate from a current automated offer and is not a coupon-code record.')
        else:
            note='No current source record is published. This page remains available to report the latest source-check status; it is not included in the sitemap.'
        if focus and po:
            note=(f'{focus}: official price, billing, and renewal terms appear below only when captured from the provider’s public page. '
                  f'This page keeps related {p["name"]} plan records together. '+note)
        elif focus and not manual_plans:
            note=(f'{focus}: no current source record is published. This page keeps the latest {p["name"]} source-check status only; '
                  'it is not included in the sitemap.')
        related_guide=template(guide_templates[p['id']]) if p['id'] in guide_templates else ''
        details=('<div class="record-details">'+''.join(record_detail(o) for o in po)+'</div>') if po else ''
        has_coupon=any(o.get('coupon_code') and captured_field_evidence(o,'coupon_code') for o in po)
        if po:
            base_heading=focus or p['name']
            heading=(base_heading if re.search(r'\b(?:pricing|prices?|coupon)\b',base_heading,re.I)
                     else base_heading+(' coupon code and pricing' if has_coupon else ' pricing'))
            first=po[0]
            description=(f'Official {p["name"]} pricing checked {date_text(first["fetched_at"])}. '
                         f'{offer_name(p["name"],first["title"])} is shown with the exact price wording from the provider page.')
        else:
            heading=f'{p["name"]} pricing availability'
            if manual_plans:
                description=(f'No automated {p["name"]} offer is currently published. '
                             'See dated manual plan-price records and the latest official source status.')
            else:
                description=(f'No current {p["name"]} pricing record is published. '
                             'See the latest official source status and source link.')
        page_disclosure=(f'Links on this page may be affiliate links; HostDealRadar may earn a commission at no extra cost to you.'
                         if p['affiliate_url'] else
                         f'Links on this page go to official {p["name"]} pages. HostDealRadar has no active affiliate relationship with {p["name"]}.')
        page_disclosure+=' Confirm availability, tax, billing terms, and renewal terms with the provider. Service labels are HostDealRadar classifications, not provider claims.'
        qualified=[o for o in po if complete_three_terms(o, rules.get((o['provider'],o['title']), {}))]
        judgment=(f'{len(qualified)} of {len(po)} current records meet the three-term comparison standard: '
                  'an official upfront total, published monthly equivalent, and monthly renewal rate for the same plan. '
                  'Records below show only the individual fields supported by their official source.'
                  if po else ('No automated offer is marked current for this provider. Dated manual plan-price records appear below; '
                              'they document package terms and are not coupon-code records.' if manual_plans else
                              'No current source record is published for this provider; no three-term comparison can be made.'))
        content=template('provider.html',provider=e(heading),judgment=e(judgment),note=e(note),source=e(p['source_url']),source_status=e(status_text),related_guide=related_guide,offers=current_html+details,page_disclosure=e(page_disclosure))
        write(Path('providers')/p['id']/'index.html',page(
            f'{heading} | HostDealRadar',
            description,
            domain+'/providers/'+p['id']+'/', content,
            {'@context':'https://schema.org','@type':'CollectionPage','name':heading},
            head_extra='<meta name="robots" content="noindex,follow">' if not po else ''))
    guide_route='/guides/godaddy-renewal-coupon/'
    guide=template('godaddy-domain-renewal.html')
    guide_schema={'@context':'https://schema.org','@type':'Article','headline':'GoDaddy coupon code: renewal savings','datePublished':'2026-09-15','dateModified':'2026-10-01','author':{'@type':'Organization','name':'HostDealRadar'},'publisher':{'@type':'Organization','name':'HostDealRadar'},'mainEntityOfPage':domain+guide_route}
    write(Path('guides/godaddy-renewal-coupon/index.html'),page('GoDaddy coupon code: renewal prices and transfer steps | HostDealRadar','Compare official .com renewal and transfer prices, rare account-specific GoDaddy renewal codes, auto-renew settings, transfer locks, DNS and email migration.',domain+guide_route,guide,guide_schema))
    namecheap_guide_route='/guides/namecheap-domain-renewal-coupon/'
    namecheap_guide=template('namecheap-domain-renewal-coupon.html')
    namecheap_guide_schema={'@context':'https://schema.org','@type':'Article','headline':'Namecheap domain renewal coupon: what works at renewal?','datePublished':'2026-09-15','dateModified':'2026-09-15','author':{'@type':'Organization','name':'HostDealRadar'},'publisher':{'@type':'Organization','name':'HostDealRadar'},'mainEntityOfPage':domain+namecheap_guide_route}
    write(Path('guides/namecheap-domain-renewal-coupon/index.html'),page('Namecheap domain renewal coupon: what works at renewal? | HostDealRadar','Namecheap renewal coupons, current .com renewal pricing, official terms, and three linked user reports.',domain+namecheap_guide_route,namecheap_guide,namecheap_guide_schema))
    namecheap_promo_route='/guides/namecheap-promo-code/'
    namecheap_promo=template('namecheap-promo-code.html')
    namecheap_promo_schema={'@context':'https://schema.org','@type':'Article','headline':'Namecheap promo code: which September codes does Namecheap publish?','datePublished':'2026-09-26','dateModified':'2026-09-26','author':{'@type':'Organization','name':'HostDealRadar'},'publisher':{'@type':'Organization','name':'HostDealRadar'},'mainEntityOfPage':domain+namecheap_promo_route}
    write(Path('guides/namecheap-promo-code/index.html'),page('Namecheap promo code: official September 2026 cards | HostDealRadar','Namecheap official September promo codes by product, with the published window, renewal limit, source links, and a dated checkout boundary.',domain+namecheap_promo_route,namecheap_promo,namecheap_promo_schema))
    cloudways_guide_route='/guides/cloudways-coupon-code/'
    cloudways_guide=template('cloudways-coupon-code.html')
    cloudways_guide_schema={'@context':'https://schema.org','@type':'FAQPage','mainEntity':[
        {'@type':'Question','name':'What Cloudways offer does the official page show?','acceptedAnswer':{'@type':'Answer','text':'As read on October 4, 2026, it displayed 40% off all hosting plans for four months, with the promo code applied automatically, plus unlimited free migrations for WordPress and WooCommerce websites. This page check did not test checkout.'}},
        {'@type':'Question','name':'What code should I enter?','acceptedAnswer':{'@type':'Answer','text':'The visible page says the promo code is already applied and does not print a human-readable code. A saved signup-link parameter contains SUMMER404, but that does not prove it is the code for the displayed offer.'}},
        {'@type':'Question','name':'Does Cloudways list an exact end date for this offer?','acceptedAnswer':{'@type':'Answer','text':'No calendar deadline was visible on the offer page when checked on October 4, 2026. It labels the promotion as limited-time.'}},
        {'@type':'Question','name':'Who qualifies for the displayed Cloudways offer?','acceptedAnswer':{'@type':'Answer','text':'The official FAQ says new users who sign up and upgrade from the free trial to a paid plan; it says existing customers are not eligible. We did not verify an account or checkout.'}},
        {'@type':'Question','name':'Did this check verify checkout or the amount on a final bill?','acceptedAnswer':{'@type':'Answer','text':'No. It records the public page only, not signup, checkout, account eligibility, tax, or a final bill.'}},
        {'@type':'Question','name':"What were Cloudways’ own Black Friday codes?",'acceptedAnswer':{'@type':'Answer','text':'Saved Cloudways pages printed BFCM18, BFCM40, BFCM2021 and BFCM4030 in their respective years. These are historical source statements, not current working codes. The retained 2020 response is a security-check page with no offer text.'}},
        {'@type':'Question','name':'Are the Cloudways codes on coupon sites Cloudways codes?','acceptedAnswer':{'@type':'Answer','text':"Not necessarily. Cloudways’ saved roundups also listed partner offers for Inspectlet, NotificationX and MexBS. Those were not additional Cloudways hosting discounts."}}
    ]}
    write(Path('guides/cloudways-coupon-code/index.html'),page('Cloudways coupon code: current official 40% offer | HostDealRadar','Cloudways currently displays 40% off hosting plans for four months, eligibility, automatic code application, migration offer, and evidence limits.',domain+cloudways_guide_route,cloudways_guide,cloudways_guide_schema))
    # The archived table moved here from the coupon guide so the head query stays
    # with the current-status guide; this page carries the archive wording only.
    cloudways_archive_route='/guides/cloudways-coupon-archive/'
    cloudways_archive=template('cloudways-coupon-archive.html')
    cloudways_archive_schema={'@context':'https://schema.org','@type':'Article','headline':'Cloudways coupon archive: what Cloudways’ own pages printed, 2018 to 2022','datePublished':'2026-09-25','dateModified':'2026-09-25','author':{'@type':'Organization','name':'HostDealRadar'},'publisher':{'@type':'Organization','name':'HostDealRadar'},'mainEntityOfPage':domain+cloudways_archive_route}
    write(Path('guides/cloudways-coupon-archive/index.html'),page('Cloudways coupon archive: 2018–2022 codes Cloudways itself printed | HostDealRadar','Archived Cloudways records: BFCM18, BFCM40, BFCM2021 and BFCM4030 as printed on Cloudways’ own pages, with Common Crawl snapshots and explicit evidence limits.',domain+cloudways_archive_route,cloudways_archive,cloudways_archive_schema))
    digitalocean_guide_route='/guides/digitalocean-promo-code/'
    digitalocean_guide=template('digitalocean-promo-code.html')
    digitalocean_guide_schema={'@context':'https://schema.org','@type':'Article','headline':'DigitalOcean promo code: the signup credit needs no code','datePublished':'2026-09-24','dateModified':'2026-09-24','author':{'@type':'Organization','name':'HostDealRadar'},'publisher':{'@type':'Organization','name':'HostDealRadar'},'mainEntityOfPage':domain+digitalocean_guide_route}
    write(Path('guides/digitalocean-promo-code/index.html'),page('DigitalOcean promo code: official signup credit and code terms | HostDealRadar','DigitalOcean says its first-team signup credit needs no code. Read the official eligibility, expiration and billing limits beside the separate promotional-code terms.',domain+digitalocean_guide_route,digitalocean_guide,digitalocean_guide_schema))
    hosting_coupons_route='/guides/hosting-coupons/'
    hosting_coupons=template('hosting-coupons.html')
    hosting_coupons_schema={'@context':'https://schema.org','@type':'Article','headline':'Hosting coupons: first find out what actually applies','datePublished':'2026-09-24','dateModified':'2026-09-24','author':{'@type':'Organization','name':'HostDealRadar'},'publisher':{'@type':'Organization','name':'HostDealRadar'},'mainEntityOfPage':domain+hosting_coupons_route}
    write(Path('guides/hosting-coupons/index.html'),page('Hosting coupons: code, no-code sale, or signup credit? | HostDealRadar','Three official examples show why a hosting code, an automatic promotion, and an account credit need different eligibility and billing checks.',domain+hosting_coupons_route,hosting_coupons,hosting_coupons_schema))
    hostinger_guide_route='/guides/hostinger-coupon-code/'
    hostinger_guide=template('hostinger-coupon-code.html')
    hostinger_guide_schema={'@context':'https://schema.org','@type':'Article','headline':'Hostinger coupon code: what you pay upfront and what renews','datePublished':'2026-09-25','dateModified':'2026-09-25','author':{'@type':'Organization','name':'HostDealRadar'},'publisher':{'@type':'Organization','name':'HostDealRadar'},'mainEntityOfPage':domain+hostinger_guide_route}
    write(Path('guides/hostinger-coupon-code/index.html'),page('Hostinger coupon code: 48-month upfront totals and renewal rates | HostDealRadar','Official Hostinger coupon cards show each 48-month upfront total beside a published renewal rate. The next renewal invoice total depends on a future selected term.',domain+hostinger_guide_route,hostinger_guide,hostinger_guide_schema))
    hostinger_vps_route='/guides/hostinger-vps-coupon-code/'
    hostinger_vps=template('hostinger-vps-coupon-code.html')
    hostinger_vps_schema={'@context':'https://schema.org','@type':'Article','headline':'Hostinger VPS coupon code: which code does the official page show?','datePublished':'2026-09-28','dateModified':'2026-09-28','author':{'@type':'Organization','name':'HostDealRadar'},'publisher':{'@type':'Organization','name':'HostDealRadar'},'mainEntityOfPage':domain+hostinger_vps_route}
    write(Path('guides/hostinger-vps-coupon-code/index.html'),page('Hostinger VPS coupon code: official code and last check | HostDealRadar','See the code shown on Hostinger official KVM VPS cards, its individual check date, product scope, and what was not verified at checkout.',domain+hostinger_vps_route,hostinger_vps,hostinger_vps_schema))
    hostinger_domain_route='/guides/hostinger-domain-coupon-code/'
    hostinger_domain_schema={'@context':'https://schema.org','@type':'Article','headline':'Hostinger domain coupon code: what can you actually use?','datePublished':'2026-09-29','dateModified':'2026-09-29','author':{'@type':'Organization','name':'HostDealRadar'},'publisher':{'@type':'Organization','name':'HostDealRadar'},'mainEntityOfPage':domain+hostinger_domain_route}
    write(Path('guides/hostinger-domain-coupon-code/index.html'),page('Hostinger domain coupon code: registration, free domains and renewal | HostDealRadar','Official domain offer check: .com first-year and renewal prices, free-domain eligibility, transfer limits and refund exceptions, with dated source records.',domain+hostinger_domain_route,template('hostinger-domain-coupon-code.html'),hostinger_domain_schema))
    a2_guide_route='/guides/a2-hosting-coupon-code/'
    a2_guide_schema={'@context':'https://schema.org','@type':'Article','headline':'A2 Hosting coupon code & renewal savings','datePublished':'2026-10-01','dateModified':'2026-10-04','author':{'@type':'Organization','name':'HostDealRadar'},'publisher':{'@type':'Organization','name':'HostDealRadar'},'mainEntityOfPage':domain+a2_guide_route}
    write(Path('guides/a2-hosting-coupon-code/index.html'),page('A2 Hosting coupon code and Hosting.com renewal savings | HostDealRadar','Check first-term offers and three routes for a higher Hosting.com renewal: account quotes, new-plan eligibility and migration, with dated official sources.',domain+a2_guide_route,template('a2-hosting-coupon-code.html'),a2_guide_schema))
    bluehost_guide_route='/guides/bluehost-promo-code/'
    bluehost_guide_schema={'@context':'https://schema.org','@type':'Article','headline':'Bluehost promo code: what can be verified?','datePublished':'2026-10-03','dateModified':'2026-10-03','author':{'@type':'Organization','name':'HostDealRadar'},'publisher':{'@type':'Organization','name':'HostDealRadar'},'mainEntityOfPage':domain+bluehost_guide_route}
    write(Path('guides/bluehost-promo-code/index.html'),page('Bluehost promo code: what can be verified? | HostDealRadar','A dated Bluehost promo-code check: what is unknown about eligibility, validity and checkout, and how to verify your own order total.',domain+bluehost_guide_route,template('bluehost-promo-code.html'),bluehost_guide_schema))
    hostgator_guide_route='/guides/hostgator-coupon-code/'
    hostgator_guide_schema={'@context':'https://schema.org','@type':'Article','headline':'HostGator coupon code: check the renewal rules first','datePublished':'2026-10-04','dateModified':'2026-10-04','author':{'@type':'Organization','name':'HostDealRadar'},'publisher':{'@type':'Organization','name':'HostDealRadar'},'mainEntityOfPage':domain+hostgator_guide_route}
    write(Path('guides/hostgator-coupon-code/index.html'),page('HostGator coupon code: renewal rules and regular prices | HostDealRadar','HostGator warns against cancelling and transferring an existing package to obtain a new-package discount. Check renewal totals, cPanel license inclusion and separate email charges.',domain+hostgator_guide_route,template('hostgator-coupon-code.html'),hostgator_guide_schema))
    wpe_guide_route='/guides/wp-engine-renewal-overage/'
    wpe_guide_schema={'@context':'https://schema.org','@type':'Article','headline':'Why did my WP Engine bill increase?','datePublished':'2026-10-01','dateModified':'2026-10-04','author':{'@type':'Organization','name':'HostDealRadar'},'publisher':{'@type':'Organization','name':'HostDealRadar'},'mainEntityOfPage':domain+wpe_guide_route}
    write(Path('guides/wp-engine-renewal-overage/index.html'),page('WP Engine renewal increase, overage fees and staging | HostDealRadar','Distinguish WordPress.com from WP Engine, trace a renewal increase, check billable visits and staging usage, and compare Cloudways Flexible with complete agency costs.',domain+wpe_guide_route,template('wp-engine-renewal-overage.html'),wpe_guide_schema))
    wpk_guide_route='/guides/wp-engine-vs-kinsta/'
    wpk_guide_schema={'@context':'https://schema.org','@type':'Article','headline':'WP Engine vs Kinsta: what will renewal cost?','datePublished':'2026-10-01','dateModified':'2026-10-01','author':{'@type':'Organization','name':'HostDealRadar'},'publisher':{'@type':'Organization','name':'HostDealRadar'},'mainEntityOfPage':domain+wpk_guide_route}
    write(Path('guides/wp-engine-vs-kinsta/index.html'),page('WP Engine vs Kinsta: renewal, overage and migration costs | HostDealRadar','Compare an existing WP Engine renewal with Kinsta Lead and Agency 20: billing periods, traffic meters, staging, update tools and illustrated migration payback.',domain+wpk_guide_route,template('wp-engine-vs-kinsta.html'),wpk_guide_schema,head_extra=f'<link rel="stylesheet" href="/assets/wp-engine-kinsta.css?v={wp_engine_kinsta_css_version}">'))
    def reviewed_row(record):
        total=money(record['first_term_total'],record['currency'])
        monthly=money(record['monthly_equivalent'],record['currency'])
        renewal=money(record['renewal_monthly_rate'],record['currency'])
        months=record['commitment_months']
        month_label='month' if months == 1 else 'months'
        return (f'<tr id="{e(reviewed_anchor(record))}"><td><strong>{e(byid[record["provider"]]["name"])}</strong>'
                f'<span>{e(record["plan"])}</span></td>'
                f'<td>{reviewed_link(record,"first_term_total",total)}</td>'
                f'<td>{reviewed_link(record,"commitment_months",str(months)+" "+month_label)}</td>'
                f'<td>{monthly}<span>Calculated: {total} ÷ {months}</span>'
                +(f'<span>{e(record["monthly_note"])}</span>' if record.get('monthly_note') else '')
                +'</td>'
                f'<td>{reviewed_link(record,"renewal_monthly_rate",renewal+"/mo")}</td>'
                f'<td>{e(record_review_date(record))} PT<span>Manual official-page review</span></td></tr>')
    compare_reviewed=('<h2>Complete plan examples</h2><p>'+e(review_intro)+'</p><div class="table-wrap"><table>'
                      '<caption>Complete plan examples from a dated manual review, in review order</caption>'
                      '<thead><tr><th>Provider / plan</th><th>First-term total</th><th>Commitment</th>'
                      '<th>Monthly equivalent</th><th>Renewal monthly rate</th><th>Checked</th></tr></thead><tbody>'
                      +''.join(reviewed_row(record) for record in reviewed)+'</tbody></table></div>') if reviewed else ''
    compare=template('compare.html',reviewed_section=compare_reviewed,partial_cards=partial_cards)
    write(Path('compare/index.html'),page('Compare plan terms | HostDealRadar','Compare documented upfront totals, terms, and renewal monthly rates from official hosting pages.',domain+'/compare/',compare,{'@context':'https://schema.org','@type':'WebPage','name':'Compare documented hosting plan terms'}))
    prose=lambda heading,body: f'<section class="wrap section prose"><div class="eyebrow">HOSTDEALRADAR</div><h1>{heading}</h1>{body}</section>'
    methodology='<p class="lead">Every listed term comes from an official public provider page. We do not estimate missing prices or invent promotions.</p><h2>What is included</h2><ul><li>We retrieve public pages only when robots.txt allows it.</li><li>We record the source URL and capture time with every record.</li><li>A term is listed as current only when the latest source check reconfirmed that exact record. A promotion also needs a verified end date; a successful fetch alone does not establish that it is still valid.</li></ul><h2 id="display-order">How pages are ordered</h2><p>Provider grids follow the configured source-list order. Offer cards and comparison rows follow the captured record order in the latest dataset. Homepage examples require current records with an established renewal rate above the initial rate. We group them by currency, billing unit, and service label. If any explicitly identify a first-month rate, only those records are eligible for the example group; otherwise all eligible records are considered. We choose the group with the most eligible records; ties use alphabetical currency, unit, and label order. Up to three records from that group come first, ordered by the larger numeric change within each record; equal changes use the record identifier. The remaining positions, up to nine cards in total, follow stored record order, excluding those examples. The example count is recalculated for each published snapshot. Initial terms and plan resources can differ, so the examples do not establish equivalent plans or an amount a buyer would save. These display orders are not recommendations, quality rankings, price rankings, or value rankings.</p>'+category_guide(offers)+'<h2>What happens when a source cannot be checked</h2><ul><li>If a source is blocked, challenged, or unclear, we publish no new offer for it.</li><li>Unverified or earlier records retain their actual capture time and are not shown as current offers.</li><li>Expired promotions are labelled expired and are never shown as a current offer.</li></ul><h2>What to verify before purchase</h2><p>Confirm checkout total, tax, eligibility, billing term, and renewal amount with the provider. A captured offer is not a checkout test or a performance review.</p>'
    old_order=('Offer cards and comparison rows follow the captured record order in the latest dataset. Homepage examples require current records with an established renewal rate above the initial rate. We group them by currency, billing unit, and service label. If any explicitly identify a first-month rate, only those records are eligible for the example group; otherwise all eligible records are considered. We choose the group with the most eligible records; ties use alphabetical currency, unit, and label order. Up to three records from that group come first, ordered by the larger numeric change within each record; equal changes use the record identifier. The remaining positions, up to nine cards in total, follow stored record order, excluding those examples. The example count is recalculated for each published snapshot.')
    methodology=methodology.replace(old_order, 'Complete-plan comparison cards and rows show dated manual reviews in review order. Other dated source notes show only their separately supported fields. Provider grids follow the configured source-list order. None of these displays is a recommendation or a price ranking.')
    methodology += ('<h2 id="three-term-standard">Three-term comparison standard</h2>'
                    '<p>A plan enters the complete-plan comparison only after a dated manual review documents its first-term total, commitment in months, and renewal monthly rate from official pages for that same plan. If there are no qualifying plans, that comparison block is omitted. Separately, a dated source note may show an individual term only when its official wording and source URL were recorded; missing fields are omitted. The monthly equivalent in a complete row is labelled as our calculation: first-term total divided by commitment months. We do not calculate a total from an advertised monthly rate, infer a renewal invoice, or combine different plans, terms, or currencies. The manual examples are separate from automated source-record statuses on provider pages and are not a live checkout test or a price ranking.</p>')
    write(Path('methodology/index.html'),page('How we check | HostDealRadar','How HostDealRadar checks official source pages.',domain+'/methodology/',prose('How we check offers',methodology),{'@context':'https://schema.org','@type':'WebPage','name':'Methodology'}))
    about='<p class="lead">HostDealRadar publishes source-checked records of publicly available hosting terms.</p><p>Every listing links to the provider page it came from and carries its own capture time. We show a price, currency, billing unit, and renewal term only when the official page supports that field.</p><p>Source checks run every six hours. When a source cannot be checked, we do not publish a new price for it; earlier records stay labelled as earlier records instead of being presented as current.</p><p>HostDealRadar is maintained under the HostDealRadar name. It is not a hosting provider and does not sell hosting plans.</p><p>Read <a href="/methodology/">how we check sources</a> for the rules behind the records.</p>'
    write(Path('about/index.html'),page('About | HostDealRadar','What HostDealRadar records and how the site is maintained.',domain+'/about/',prose('About HostDealRadar',about),{'@context':'https://schema.org','@type':'AboutPage','name':'About HostDealRadar'}))
    contact='<p class="lead">Contact HostDealRadar about a source record, a correction, or a change on a provider page.</p><p>Email <a href="mailto:contact@hostdealradar.com">contact@hostdealradar.com</a>.</p><p>This address reaches the person who maintains HostDealRadar. For a record correction, include the page URL and the specific term that has changed so it can be checked against the official source.</p>'
    write(Path('contact/index.html'),page('Contact | HostDealRadar','Contact HostDealRadar about source records and corrections.',domain+'/contact/',prose('Contact',contact),{'@context':'https://schema.org','@type':'ContactPage','name':'Contact HostDealRadar'}))
    write(Path('disclosure/index.html'),page('Affiliate disclosure | HostDealRadar','Affiliate disclosure for HostDealRadar.',domain+'/disclosure/',prose('Affiliate disclosure','<p class="lead">HostDealRadar currently links to official provider pages and does not use affiliate links.</p><p>If we later use an approved affiliate link, the link and relevant page will say so clearly. We will not use cookie injection, self-referrals, brand-keyword ads, or links that break an affiliate program’s terms.</p>'),{'@context':'https://schema.org','@type':'WebPage','name':'Affiliate disclosure'}))
    analytics_privacy=''
    if ga4_measurement_id:
        analytics_privacy=('''<h2>Optional Google Analytics</h2>
<p>We use Google Analytics 4 to understand page visits and engagement and improve this website. The Google Analytics tag loads only after you choose Allow analytics. Reject analytics keeps that tag from loading. Google processes usage data on our behalf, including page paths, browser and device information and approximate location. We do not deliberately send your name, email address, purchase details, or page URL query strings to Google Analytics.</p>
<p>After you allow analytics, Google Analytics may store first-party cookies named _ga and _ga_&lt;identifier&gt; in your browser. We configure their lifetime to 180 days; browsers may shorten this. Advertising consent remains denied, and our tag disables Google signals and advertising personalization.</p>
<p>We store your analytics choice and its date in your browser's local storage for up to 180 days. This stores the choice without sending it to Google before consent. You can change or withdraw your choice through Analytics cookie settings in the footer of any page. Rejection stops subsequent Analytics collection on this page and removes accessible Google Analytics cookies for this hostname; it does not erase data already sent. If browser storage is unavailable, we ask again on the next page.</p>
<p>Read <a href="https://policies.google.com/technologies/partner-sites">how Google uses information from sites that use its services</a>, the <a href="https://policies.google.com/privacy">Google Privacy Policy</a>, and the <a href="https://business.safety.google/adsprocessorterms/">Google data processing terms</a>. Google and its contracted processors may process data in other countries. You may also use Google's <a href="https://tools.google.com/dlpage/gaoptout">Analytics opt-out browser add-on</a>.</p>
<h2>Hosting and traffic statistics</h2>
<p>Cloudflare delivers this site and processes network requests, including IP addresses, for hosting and security. Its server-side traffic statistics operate separately from the optional Google Analytics tag. Rejecting Google Analytics does not stop the processing needed to deliver and protect the website. Read the <a href="https://www.cloudflare.com/privacypolicy/">Cloudflare Privacy Policy</a>.</p>
<h2>Privacy questions</h2><p>Use the <a href="/contact/">contact page</a> to ask about this policy. Please do not include passwords or payment details.</p>''')
    write(Path('privacy/index.html'),page('Privacy | HostDealRadar','Privacy information for HostDealRadar.',domain+'/privacy/',prose('Privacy','<p class="lead">This static site does not require accounts or collect purchase details.</p><p>Provider links open their own sites, where their privacy policies apply. We do not use affiliate-cookie injection or sell visitor information.</p><p>HostDealRadar currently does not display third-party advertising or use affiliate links. If either is introduced, we will disclose it here and update this page.</p>'+analytics_privacy),{'@context':'https://schema.org','@type':'WebPage','name':'Privacy'}))
    agent_markdown='''# HostDealRadar agent guide

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
'''
    skill_markdown='''---
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
'''
    auth_markdown='''# auth.md — HostDealRadar authentication status

Status: under construction

Authentication is not available. HostDealRadar's public record lookup is available without an account and is read-only. Planned authentication metadata is a contract placeholder only: it does not register users, issue credentials, send email, store identity data, or redirect to an authorization screen.

- status: `under_construction`
- available: `false`
- capabilities_status: `planned_contract_only`
- message: `Coming soon; authentication is not available.`
- launch_date: `null`

Planned endpoints return HTTP 503 with `temporarily_unavailable` until authentication is actually implemented.

## Agent registration (planned)

`agent_auth` metadata is published only to describe the future contract. The planned registration endpoint is `/agent-auth/register`, but agent registration is unavailable: it creates no account, issues no credential, and accepts no identity data. The sole planned identity type is `anonymous`, with the planned credential type `planned_contract_only`; neither is an available enrollment method. The planned claim and revocation endpoints also return HTTP 503. Until a real implementation exists, agents use the public read-only lookup without credentials.
'''
    agent_records=[agent_record(o, byid[o['provider']], states[o['slug']][0]) for o in offers]
    write(Path('agent-data.json'),json.dumps(agent_records,separators=(',',':')))
    write(Path('ai/index.md'),agent_markdown)
    write(Path('ai/index.ilang'),'''::ILANG
[TYPE:site-guide][LANG:en]

::STATE{@SITE, name:HostDealRadar, access:public, scope:published hosting and domain source records}
::OBJECTIVE{lookup_public_records}
  target: Read a bounded published record by exact provider ID or record slug.
  endpoint: https://hostdealradar.com/api/agent/lookup
  method: GET
  input: provider=<exact provider ID or name> OR slug=<exact record ID>
  output: source URL, capture time, listed terms, and record state.
::RULE{read_only}
  No checkout, availability, eligibility, or provider-performance claim is verified here.
::RULE{one_query}
  Supply exactly one of provider or slug.
::FACT{key:limits|value:Results are public and bounded to 25 records.}
''')
    # The discovery digest must describe the exact bytes a client receives;
    # write this generated Markdown without platform newline conversion.
    write_bytes(Path('ai/skills/site-lookup/SKILL.md'),skill_markdown.encode('utf-8'))
    write(Path('auth.md'),auth_markdown)
    write(Path('openapi.json'),json.dumps({'openapi':'3.1.0','info':{'title':'HostDealRadar public record lookup','version':'1.0.0','description':'Read-only lookup of public source records. It does not fetch third-party URLs, test checkout, or perform write operations.'},'servers':[{'url':domain}],'paths':{'/api/agent/lookup':{'get':{'summary':'Look up public records by exact provider ID or record slug','parameters':[{'name':'provider','in':'query','required':False,'schema':{'type':'string'},'description':'Exact provider ID or name; cannot be combined with slug.'},{'name':'slug','in':'query','required':False,'schema':{'type':'string'},'description':'Exact public record ID; cannot be combined with provider.'}],'responses':{'200':{'description':'One or more bounded public records.'},'400':{'description':'Missing or ambiguous query.'},'404':{'description':'No exact public record found.'},'405':{'description':'GET only.'}}}}}},separators=(',',':')))
    write(Path('.well-known/api-catalog.json'),json.dumps({'linkset':[{'anchor':domain+'/api/agent/lookup','service-desc':[{'href':domain+'/openapi.json','type':'application/json'}],'service-doc':[{'href':domain+'/ai/','type':'text/markdown'}]}]},separators=(',',':')))
    write(Path('.well-known/agent-skills/index.json'),json.dumps({'$schema':'https://schemas.agentskills.io/discovery/0.2.0/schema.json','skills':[{'name':'site-lookup','type':'skill-md','description':'Retrieve actual public HostDealRadar records by exact provider identifier or record slug.','url':domain+'/ai/skills/site-lookup/SKILL.md','digest':'sha256:'+hashlib.sha256(skill_markdown.encode('utf-8')).hexdigest()}]},separators=(',',':')))
    write(Path('.well-known/ai-catalog.json'),json.dumps({'specVersion':'1.0','host':{'displayName':cfg['site']['brand'],'identifier':'did:web:'+urlsplit(domain).hostname},'entries':[{'identifier':'urn:air:'+urlsplit(domain).hostname+':api:public-record-lookup','displayName':'HostDealRadar public record lookup','type':'application/vnd.oai.openapi+json','url':domain+'/openapi.json','representativeQueries':['look up a published HostDealRadar provider record','find the official source URL for a HostDealRadar record','retrieve a hosting price record by slug']},{'identifier':'urn:air:'+urlsplit(domain).hostname+':skill:site-lookup','displayName':'HostDealRadar site lookup skill','type':'text/markdown','url':domain+'/ai/skills/site-lookup/SKILL.md','representativeQueries':['how to query HostDealRadar public records','find records by exact hosting provider ID','understand HostDealRadar source-record limits']}]},separators=(',',':')))
    construction={'status':'under_construction','available':False,'capabilities_status':'planned_contract_only','message':'Coming soon; authentication is not available.','launch_date':None}
    write(Path('.well-known/oauth-authorization-server'),json.dumps({'issuer':domain,'authorization_endpoint':domain+'/agent-auth/authorize','token_endpoint':domain+'/agent-auth/token','jwks_uri':domain+'/.well-known/jwks.json','response_types_supported':['code'],'grant_types_supported':['authorization_code'],'agent_auth':{'skill':domain+'/auth.md','register_uri':domain+'/agent-auth/register','claim_uri':domain+'/agent-auth/claim','identity_types_supported':['anonymous'],'anonymous':{'credential_types_supported':['planned_contract_only']},'revocation_uri':domain+'/agent-auth/revoke','methods':[{'type':'anonymous','credential_type':'planned_contract_only','available':False,'description':'Construction placeholder: registration is not available.'}]},**construction},separators=(',',':')))
    write(Path('.well-known/oauth-protected-resource'),json.dumps({'resource':domain,'authorization_servers':[domain],'scopes_supported':['public:records:read'],'bearer_methods_supported':['header'],'planned_resource_endpoint':domain+'/api/agent/lookup',**construction},separators=(',',':')))
    write(Path('.well-known/jwks.json'),json.dumps({'keys':[],'status':'under_construction','message':'No token validation keys are active.'},separators=(',',':')))
    write(Path('.well-known/mcp/server-card.json'),json.dumps({'serverInfo':{'name':'hostdealradar-public-lookup','version':'1.0.0'},'description':'Read bounded public HostDealRadar records by provider ID or record slug.','transport':{'type':'streamable-http','endpoint':domain+'/mcp'},'transports':[{'type':'streamable-http','endpoint':domain+'/mcp'}],'capabilities':{'tools':{}},'tools':[{'name':'hostdealradar_lookup','description':'Read published public records by exact provider ID or record slug.'}]},separators=(',',':')))
    write(Path('assets/agent-tools.js'),'''(() => {
  const api = navigator.modelContext;
  if (!api || typeof api.registerTool !== 'function') return;
  const controller = new AbortController();
  api.registerTool({
    name: 'hostdealradar_lookup',
    description: 'Read bounded public HostDealRadar records by exact provider ID or record slug.',
    inputSchema: { type: 'object', additionalProperties: false, oneOf: [{ required: ['provider'], properties: { provider: { type: 'string' } } }, { required: ['slug'], properties: { slug: { type: 'string' } } }] },
    execute: async (input) => {
      const params = new URLSearchParams(); if (typeof input.provider === 'string') params.set('provider', input.provider); if (typeof input.slug === 'string') params.set('slug', input.slug);
      const response = await fetch('/api/agent/lookup?' + params.toString(), { headers: { accept: 'application/json' } });
      return await response.json();
    }
  }, { signal: controller.signal });
})();
''')
    write(Path('_worker.js'),agent_worker(unpublished_source_only))
    write(Path('robots.txt'),'User-agent: *\nAllow: /\nContent-Signal: ai-train=no, search=yes, ai-input=no\nAgentmap: '+domain+'/.well-known/ai-catalog.json\nSitemap: '+domain+'/sitemap.xml\n')
    write(Path('404.html'),page('Page not found | HostDealRadar','This page does not exist.',domain+'/404.html',prose('Page not found','<p><a href="/">Return to current offers</a></p>'),{'@context':'https://schema.org','@type':'WebPage','name':'Page not found'}))
    routes=['/','/providers/','/compare/','/methodology/','/about/','/contact/','/disclosure/','/privacy/',guide_route,namecheap_guide_route,namecheap_promo_route,cloudways_guide_route,cloudways_archive_route,digitalocean_guide_route,hosting_coupons_route,hostinger_guide_route]
    routes.append(hostinger_vps_route)
    routes.append(hostinger_domain_route)
    routes.append(a2_guide_route)
    routes.append(bluehost_guide_route)
    routes.append(hostgator_guide_route)
    routes.append(wpe_guide_route)
    routes.append(wpk_guide_route)
    # Keep zero-current provider pages accessible for truthful status reporting,
    # but do not submit them as index targets until a current source record exists.
    routes+=[f'/providers/{p["id"]}/' for p in public_providers if p['id'] in current_provider_ids]
    # lastmod tracks the rendered page itself: it only moves when the page's
    # material content changes, not when a capture timestamp is refreshed.
    previous=payload.get(STATE_KEY)
    if not isinstance(previous, dict): previous={}
    now_stamp=stamp(); next_state={}; entries=[]
    for route in routes:
        digest=hashlib.sha256(material(rendered.get(route,'')).encode('utf-8')).hexdigest()
        prior=previous.get(route) or {}
        lastmod=prior.get('lastmod') if prior.get('hash')==digest and prior.get('lastmod') else now_stamp
        next_state[route]={'hash':digest,'lastmod':lastmod}
        entries.append((route,lastmod))
    payload[STATE_KEY]=next_state
    DATA.write_text(json.dumps(payload,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    write(Path('sitemap.xml'),'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'+''.join('<url><loc>'+e(domain+path)+'</loc><lastmod>'+e(lastmod)+'</lastmod></url>' for path,lastmod in entries)+'</urlset>')
    return {'current':len(current),'history':len(history)}
if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--config'); parser.add_argument('--output'); args=parser.parse_args(); build(args.config,args.output); print('Static site built.')
