import argparse, hashlib, html, json, re, shutil, math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from urllib.parse import urlsplit
from config import ROOT, load_config

TEMPLATES=ROOT/'templates'; OUT=ROOT/'site'; DATA=ROOT/'data/offers.json'; ASSETS=ROOT/'assets'
# Sitemap lastmod state lives inside the payload the refresh workflow already
# commits, so persisting it needs no change to the workflow or extra token scope.
STATE_KEY='page_lastmod'
CURRENT='current'; HISTORY=('retained','stale','expired','unverified')
# Why a reachable official page still yields no deterministic price rule.
BLOCKER_TEXT={'price_rendered_by_js':'The published prices on this page are rendered by JavaScript, so no figure can be read without executing scripts.',
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
def money(value, currency='USD'): return f'${float(value):,.2f}' if currency=='USD' else f'{currency} {float(value):,.2f}'
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
def stamp(): return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')
# Volatile text that must not by itself count as a material page change: capture
# timestamps and the "last snapshot" date move on every run even when the terms
# shown are identical.
VOLATILE=re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z|\b[A-Z][a-z]{2} \d{1,2}, \d{4}\b')
def material(text): return VOLATILE.sub('<t>', text)
def template(name, **fields): return Template((TEMPLATES/name).read_text(encoding='utf-8')).safe_substitute(**fields)
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
    currencies = set(re.findall(r'\b(?:USD|CAD|AUD|GBP|EUR)\b', evidence))
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
    renewal = displayed_rate(offer, 'renewal_price') if renewal_supported(offer, rule) else 'Unknown'
    initial = displayed_rate(offer, 'price') if offer.get('price') is not None else price(offer)
    return (f'<div class="source-bar"><div><strong>{e(initial_label(offer, rule))}</strong><br>'
            f'<h3>{e(initial)}</h3>{rate_source(offer)}</div>'
            f'<div><strong>Renewal rate</strong><br><h3>{e(renewal)}</h3>{rate_source(offer)}</div></div>')

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
        record=next(iter(earlier or []), None)
        prefix='Earlier plan example, not current'
    if record is None:
        return 'Captured plan details: Unknown.'
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
    if earlier_records:
        return (f'Earlier {provider_name} terms retained with their original source and '
                'capture time. They are not presented as current offers.')
    return (f'Latest official source-check status for {provider_name}. '
            'No price record is published when the source cannot support one.')

def category_guide(records):
    items=[]
    for category in category_names(records):
        definition=CATEGORY_DEFINITIONS.get(category,
            'Definition: Unknown.')
        items.append(f'<li><strong>{e(category)}</strong>: {e(definition)}</li>')
    return ("<h2 id=\"service-labels\">Current service labels</h2>"
            "<p>Each captured record keeps one existing HostDealRadar service label. These labels describe the captured service type; they are not provider claims, performance scores, or recommendations.</p>"
            "<ul>"+''.join(items)+"</ul>"
            "<p><strong>Limits:</strong> some existing labels overlap, including Hosting and Web hosting and Domains and Domain registration. Labels vary in specificity and do not establish matching resources or performance. They remain unchanged. Homepage examples use a shared label to narrow the selection, which does not make the plans equivalent. Definitions follow the first appearance of each label in the stored records, not a ranking.</p>")

def featured_renewals(current, rules):
    # Compare differences only inside one currency/unit/service group. Prefer
    # explicit first-month offers, whose duration is unambiguous to a visitor.
    groups = defaultdict(list)
    for offer in current:
        rule = rules.get((offer['provider'], offer['title']), {})
        if (offer.get('price') is not None and renewal_supported(offer, rule)
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
    if offer.get('price') is not None: item.update({'price':str(offer['price']),'priceCurrency':offer.get('currency','USD')})
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
        'price': offer.get('price'), 'currency': offer.get('currency'),
        'billing_period': offer.get('billing_period'), 'commitment_months': offer.get('commitment_months'),
        'renewal_price': offer.get('renewal_price'), 'coupon_code': offer.get('coupon_code'),
        'valid_until': offer.get('valid_until'), 'source_url': offer['source_url'],
        'captured_at': offer['fetched_at'], 'record_url': '/deals/' + offer['slug'] + '/',
        'limitations': 'Prices, eligibility, checkout totals, and renewal terms must be confirmed with the provider.'
    }

def agent_worker():
    """Return the Pages Worker for public agent discovery and read-only lookup."""
    return r'''const LINK_HEADER = [
  '</.well-known/api-catalog>; rel="api-catalog"',
  '</openapi.json>; rel="service-desc"',
  '</ai/>; rel="service-doc"',
  '</.well-known/ai-catalog.json>; rel="describedby"'
].join(', ');
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
export default {
  async fetch(request, env) {
    const url = new URL(request.url); const path = url.pathname;
    if (path === '/api/agent/lookup') {
      if (request.method !== 'GET') return json({ error: 'method_not_allowed', message: 'Use GET for this read-only endpoint.' }, 405, { allow: 'GET' });
      const provider = (url.searchParams.get('provider') || '').trim().toLowerCase(); const slug = (url.searchParams.get('slug') || '').trim();
      if ((!provider && !slug) || (provider && slug)) return json({ error: 'invalid_query', message: 'Supply exactly one of provider or slug.' }, 400);
      const records = await (await asset(env, request, '/agent-data.json')).json();
      const matches = slug ? records.filter((record) => record.id === slug) : records.filter((record) => record.provider.id === provider || record.provider.name.toLowerCase() === provider);
      if (!matches.length) return json({ status: 'not_found', query: slug ? { slug } : { provider }, records: [], message: 'No public HostDealRadar record matched this exact identifier.' }, 404);
      return json({ status: 'ok', query: slug ? { slug } : { provider }, record_count: Math.min(matches.length, 25), records: matches.slice(0, 25), limitations: 'This read-only API reports published source records. It does not test checkout, availability, eligibility, or provider performance.' });
    }
    if (['/agent-auth/authorize', '/agent-auth/token', '/agent-auth/register', '/agent-auth/claim'].includes(path)) return unavailable();
    const specialAssets = { '/ai/': ['/ai/index.md', 'text/markdown; charset=utf-8'], '/.well-known/api-catalog': ['/.well-known/api-catalog.json', 'application/linkset+json; charset=utf-8'], '/.well-known/oauth-authorization-server': ['/.well-known/oauth-authorization-server', 'application/json; charset=utf-8'], '/.well-known/oauth-protected-resource': ['/.well-known/oauth-protected-resource', 'application/json; charset=utf-8'], '/.well-known/jwks.json': ['/.well-known/jwks.json', 'application/json; charset=utf-8'] };
    if (specialAssets[path]) { const [assetPath, contentType] = specialAssets[path]; return mergedResponse(await asset(env, request, assetPath), { 'content-type': contentType, 'access-control-allow-origin': '*' }); }
    if (path === '/' && wantsMarkdown(request)) return mergedResponse(await asset(env, request, '/ai/index.md'), { 'content-type': 'text/markdown; charset=utf-8', 'vary': 'Accept', 'link': LINK_HEADER });
    const response = await env.ASSETS.fetch(request);
    if (path === '/') return mergedResponse(response, { 'vary': 'Accept', 'link': LINK_HEADER });
    if (path === '/.well-known/ai-catalog.json') return mergedResponse(response, { 'content-type': 'application/json; charset=utf-8', 'access-control-allow-origin': '*' });
    return response;
  },
};
'''

def build(config_path=None, output=None):
    global OUT
    OUT=Path(output) if output else ROOT/'site'
    cfg=load_config(config_path); domain=cfg['site']['domain'].rstrip('/'); payload=json.loads(DATA.read_text(encoding='utf-8'))
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
    def page(title, description, canonical, content, schema):
        return template('base.html',title=e(title),description=e(description),canonical=e(canonical),brand=e(cfg['site']['brand']),tagline=e(cfg['settings']['tagline']),repo=e(cfg['settings']['repo_url']),social_image='',footer_status=e('Data source checks are automated.'),content=content,schema=json.dumps(schema,separators=(',',':')))
    def card(o, historical=False):
        p=byid[o['provider']]; state, message=states[o['slug']]; terms=[]
        rule=rules.get((o['provider'], o['title']), {})
        if o.get('commitment_months'): terms.append(f"{o['commitment_months']}-month term")
        label='Official price' if o.get('kind')=='regular_price' else 'Promotion'
        if historical: label={'retained':'Earlier record','stale':'Needs recheck','expired':'Expired','unverified':'Unverified'}.get(state,state.title())
        cls='card history' if historical else 'card'
        return f'''<article class="{cls}"><div class="card-top"><span class="provider-name">{e(p['name'])}</span><span class="tag">{label}</span></div><h3><a href="/deals/{e(o['slug'])}/">{e(o['title'])}</a></h3>{rate_pair(o, rule)}<p class="summary">{e(o.get('category','Hosting'))}</p><p class="small">{e(public_terms(o.get('condition') or ('Prepaid term: '+str(o['commitment_months'])+' months.' if o.get('commitment_months') else 'Initial term: Unknown.')))}</p><dl>{''.join(f'<div><dt>{e(x.split(" ")[0])}</dt><dd>{e(x)}</dd></div>' for x in terms) or '<div><dt>Commitment</dt><dd>Unknown</dd></div>'}</dl><a class="button" href="/deals/{e(o['slug'])}/">View terms</a><p class="capture">{e(message)}</p></article>'''
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
            label=f'{count} current listings →'
        elif status.get('status') == 'evidenced' and status.get('capture_status') == 'unmatched':
            label='Latest source check produced no published record →'
        else:
            label='Latest source check did not complete →'
        return f'<a class="provider-tile" href="/providers/{e(p["id"])}/"><strong>{e(p["name"])}</strong><p>{e(provider_summary(current_by_provider[p["id"]], history_by_provider[p["id"]]))}</p><span>{e(label)}</span></a>'
    provider_tiles=''.join(tile(p) for p in providers)
    featured=featured_renewals(current, rules)
    featured_slugs={o['slug'] for o in featured}
    shown=(featured+[o for o in current if o['slug'] not in featured_slugs])[:9]
    selection_note=(f'The first {len(featured)} cards are renewal-change examples selected from one currency, billing unit, and service label, with larger recorded changes first. '
                    if featured else 'No eligible renewal-change examples are available in this snapshot. ')
    selection_note+='Other cards follow stored record order. This is not a recommendation or a ranking of price, quality, or value.'
    home=template('index.html',month=datetime.now().strftime('%B %Y'),deal_count=len(current),provider_count=len(providers),updated=e('Last source snapshot: '+date_text(payload.get('generated_at','Unknown'))),selection_note=e(selection_note),offers='<div class="cards">'+''.join(card(o) for o in shown)+'</div>' if shown else '<div class="empty"><h3>No current offers are published</h3><p>We only show terms that were captured from an official source in the latest check. Check back after the next source run.</p></div>',providers=provider_tiles)
    # The homepage lists different services; its entries are navigation targets,
    # not merchant Offers for products that HostDealRadar sells.
    home_schema={'@context':'https://schema.org','@graph':[
        {'@type':'WebSite','@id':domain+'/#website','name':cfg['site']['brand'],'url':domain+'/','inLanguage':'en-US','publisher':{'@id':domain+'/#organization'}},
        {'@type':'Organization','@id':domain+'/#organization','name':cfg['site']['brand'],'url':domain+'/','sameAs':[cfg['settings']['repo_url']]},
        {'@type':'ItemList','name':'HostDealRadar official hosting offers','itemListElement':[{'@type':'ListItem','position':i+1,'item':{'@type':'WebPage','name':o['title'],'url':domain+'/deals/'+o['slug']+'/'}} for i,o in enumerate(current)]}
    ]}
    write(Path('index.html'),page('HostDealRadar | Official hosting offers', 'Official hosting offers with source-check status and provider links.',domain+'/',home,home_schema))
    provider_listing='<section class="wrap section"><div class="eyebrow">OFFICIAL SOURCES</div><h1>Providers we check</h1><p class="lead">Providers have public source pages in our list. Each provider page shows whether the latest source check confirmed listings, produced no published record, or did not complete. The grid follows the configured source-list order; it is not a recommendation, quality ranking, or price ranking. Each summary names the first current record, or an explicitly marked earlier record when none is current. Open a provider for the matching official source. <a href="/methodology/#service-labels">Read service-label definitions and limits</a>. Earlier records stay clearly marked.</p><div class="provider-grid">'+provider_tiles+'</div></section>'
    write(Path('providers/index.html'),page('Providers | HostDealRadar','Hosting providers and their latest source-check status.',domain+'/providers/',provider_listing,{'@context':'https://schema.org','@type':'CollectionPage','name':'Providers'}))
    for p in providers:
        mine=[o for o in offers if o['provider']==p['id']]
        po=[o for o in mine if states[o['slug']][0]==CURRENT]; ph=[o for o in mine if states[o['slug']][0] in HISTORY]
        status=statuses.get(p['id'],{'status':'not checked','reason':'No source check has run yet.'})
        if p['id'] in state_only:
            status_text, detail, _ = state_only_display(status, blockers.get(p['id']))
            current_html='<div class="empty"><h3>'+e(status_text)+'</h3><p>'+e(detail)+'</p></div>'
        else:
            status_text='Official page read; capture rules matched.' if status['status']=='evidenced' and status.get('capture_status')=='matched' and status.get('http_status')==200 and status.get('visible_excerpt') else e(status['reason'])
            current_html='<div class="cards">'+''.join(card(o) for o in po)+'</div>' if po else '<div class="empty"><h3>No current offer is published for this source</h3><p>'+e('Promotional end date not verified.' if any(states[o['slug']][0]=='unverified' for o in ph) else status['reason'])+'</p></div>'
        history_html=''
        if ph:
            history_html='<section class="history-block"><h2>Unverified or earlier records kept for reference</h2><p class="muted">These records may be expired, stale, not reconfirmed, or missing a verified promotional end date. Each keeps its actual capture time and is not a current offer.</p><div class="cards">'+''.join(card(o,historical=True) for o in ph)+'</div></section>'
        note=provider_summary(po, ph)+' The summary uses the first current record, or the first reference record if none is current. Cards in each section follow stored record order; this is not a recommendation or a ranking of price, quality, or value.'
        guide_templates={'godaddy':'provider-guide.html','namecheap':'namecheap-provider-guide.html','cloudways':'cloudways-provider-guide.html'}
        related_guide=template(guide_templates[p['id']]) if p['id'] in guide_templates else ''
        content=template('provider.html',provider=e(p['name']),note=e(note),source=e(p['source_url']),source_status=e(status_text),related_guide=related_guide,offers=current_html+history_html)
        write(Path('providers')/p['id']/'index.html',page(
            f'{p["name"]} source-check status and terms | HostDealRadar',
            provider_description(p['name'], po, ph),
            domain+'/providers/'+p['id']+'/', content,
            {'@context':'https://schema.org','@type':'CollectionPage','name':p['name']+' terms and source-check status'}))
    guide_route='/guides/godaddy-renewal-coupon/'
    guide=template('godaddy-renewal-coupon.html')
    guide_schema={'@context':'https://schema.org','@type':'Article','headline':'GoDaddy renewal coupon: do renewal promo codes work?','datePublished':'2026-09-15','dateModified':'2026-09-15','author':{'@type':'Organization','name':'HostDealRadar'},'publisher':{'@type':'Organization','name':'HostDealRadar'},'mainEntityOfPage':domain+guide_route}
    write(Path('guides/godaddy-renewal-coupon/index.html'),page('GoDaddy renewal coupon: do renewal promo codes work? | HostDealRadar','GoDaddy renewal coupons, customer-specific renewal codes, current .com renewal terms, and three linked user reports.',domain+guide_route,guide,guide_schema))
    namecheap_guide_route='/guides/namecheap-domain-renewal-coupon/'
    namecheap_guide=template('namecheap-domain-renewal-coupon.html')
    namecheap_guide_schema={'@context':'https://schema.org','@type':'Article','headline':'Namecheap domain renewal coupon: what works at renewal?','datePublished':'2026-09-15','dateModified':'2026-09-15','author':{'@type':'Organization','name':'HostDealRadar'},'publisher':{'@type':'Organization','name':'HostDealRadar'},'mainEntityOfPage':domain+namecheap_guide_route}
    write(Path('guides/namecheap-domain-renewal-coupon/index.html'),page('Namecheap domain renewal coupon: what works at renewal? | HostDealRadar','Namecheap renewal coupons, current .com renewal pricing, official terms, and three linked user reports.',domain+namecheap_guide_route,namecheap_guide,namecheap_guide_schema))
    cloudways_guide_route='/guides/cloudways-coupon-code/'
    cloudways_guide=template('cloudways-coupon-code.html')
    cloudways_status=statuses.get('cloudways', {})
    countdown=(cloudways_status.get('source_claim_evidence') or {}).get('countdown', {})
    countdown_quote=countdown.get('visible_excerpt') or countdown.get('quote') or 'No countdown statement was captured in this release.'
    checked_at=cloudways_status.get('checked_at', '')
    checked_date=checked_at[:10] if checked_at else 'the recorded source-check time'
    cloudways_guide=(cloudways_guide
        .replace('{{CLOUDWAYS_CHECKED_DATE}}', e(checked_date))
        .replace('{{CLOUDWAYS_COUNTDOWN_QUOTE}}', e(countdown_quote)))
    cloudways_guide_schema={'@context':'https://schema.org','@type':'FAQPage','mainEntity':[
        {'@type':'Question','name':'Is SUMMER404 a verified current Cloudways hosting coupon code?','acceptedAnswer':{'@type':'Answer','text':'No. The source check found historical page material and a zeroed countdown, but it did not verify current checkout redemption.'}},
        {'@type':'Question','name':'What date does the official promo page show?','acceptedAnswer':{'@type':'Answer','text':'The stored source evidence says: '+countdown_quote+'. The response does not state a time zone, so the guide does not convert it to another date.'}},
        {'@type':'Question','name':'Did this check verify another current general hosting coupon code?','acceptedAnswer':{'@type':'Answer','text':'No current general code was verified by this page check. It did not test checkout or every Cloudways product.'}}
    ]}
    write(Path('guides/cloudways-coupon-code/index.html'),page('Cloudways coupon code: source status for SUMMER404 | HostDealRadar','Cloudways source evidence for SUMMER404, including the recorded countdown statement and its limits.',domain+cloudways_guide_route,cloudways_guide,cloudways_guide_schema))
    for o in offers:
        state, message=states[o['slug']]
        rule=rules.get((o['provider'], o['title']), {})
        advertised = price(o) + (' / ' + period_text(o) if o.get('price') is not None else '')
        summary_text=public_terms(o.get('condition') or 'Terms captured from the official provider page.')
        p=byid[o['provider']]; terms=[('Listing type','Regular price; no discount claimed' if o.get('kind')=='regular_price' else 'Promotion'),('Advertised price',advertised),('Commitment',str(o['commitment_months'])+' months' if o.get('commitment_months') else 'Unknown'),('Renewal price',displayed_rate(o,'renewal_price') if renewal_supported(o,rule) else 'Unknown'),('Coupon code',o.get('coupon_code') or 'Unknown'),('Valid until',o.get('valid_until') or 'Unknown'),('Captured at',o['fetched_at']),('Record state',state)]
        terms_html=''.join(f'<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>' for k,v in terms)
        rel='rel="noopener noreferrer"' if not p['affiliate_url'] else 'rel="sponsored noopener noreferrer"'
        disclosure='This is an official link; no affiliate relationship is active.' if not p['affiliate_url'] else 'This may be an affiliate link; we may earn a commission at no extra cost to you.'
        status_html=f'<p class="record-state state-{e(state)}"><strong>{e(state.title())}</strong> {e(message)}</p>'
        display_name=offer_name(p['name'], o['title'])
        content=template('deal.html',provider=e(p['name']),provider_id=e(p['id']),category=e(o.get('category','Hosting')),offer_title=e(display_name),summary=e(summary_text),rate_pair=rate_pair(o,rule),terms=terms_html,source_note=e(public_terms(o['evidence'])),source_url=e(o['source_url']),price=e(price(o)),billing=e('Billed under the provider terms.'),status=status_html,outbound=e(o['offer_url']),rel=rel,disclosure=e(disclosure))
        canonical=domain+'/deals/'+o['slug']+'/'
        # Product snippets require an active price. Earlier records and current
        # records without a numeric price remain useful pages, but they must not
        # claim Product/Offer eligibility by publishing incomplete price data.
        if state==CURRENT and o.get('price') is not None:
            schema={'@context':'https://schema.org','@type':'Product','name':display_name,'description':summary_text,'offers':schema_offer(o,canonical,display_name)}
        else:
            schema={'@context':'https://schema.org','@type':'WebPage','name':display_name,'description':summary_text}
        write(Path('deals')/o['slug']/'index.html',page(
            f'{display_name}: official terms | HostDealRadar',
            deal_description(p['name'], o), canonical, content, schema))
    def row(o):
        state,message=states[o['slug']]
        rule=rules.get((o['provider'], o['title']), {})
        renewal=displayed_rate(o,'renewal_price') if renewal_supported(o,rule) else 'Unknown'
        return f'<tr><td><strong>{e(byid[o["provider"]]["name"])}</strong><span>{e(o["title"])}</span></td><td>{e(displayed_rate(o,"price") if o.get("price") is not None else price(o))}<span>{e(initial_label(o,rule))}</span>{rate_source(o)}</td><td>{e(str(o.get("commitment_months") or "Unknown"))}</td><td>{e(renewal)}<br>{rate_source(o)}</td><td>{e(state.title())}<span>{e(o["fetched_at"])}</span></td><td><a href="{e(o["source_url"])}" rel="noopener noreferrer">Official page ↗</a></td></tr>'
    rows=''.join(row(o) for o in current)
    history_rows=''.join(row(o) for o in history)
    history_html=''
    if history:
        history_html='<div class="table-wrap history-block"><h2>Unverified or earlier records, not current offers</h2><p class="muted">These records may be expired, stale, not reconfirmed, or missing a verified promotional end date. Shown in captured record order for reference with their own currency, billing period and capture time. This is not a ranking or recommendation.</p><table><thead><tr><th>Provider / plan</th><th>Advertised price</th><th>Commitment</th><th>Renewal</th><th>State</th><th>Source</th></tr></thead><tbody>'+history_rows+'</tbody></table></div>'
    compare=template('compare.html',rows=rows,history=history_html,empty='' if current else '<div class="empty"><h3>No current offers available</h3><p>The latest source check did not confirm any publishable terms.</p></div>')
    write(Path('compare/index.html'),page('Compare terms | HostDealRadar','Compare hosting terms captured from official sources.',domain+'/compare/',compare,{'@context':'https://schema.org','@type':'WebPage','name':'Compare hosting terms'}))
    prose=lambda heading,body: f'<section class="wrap section prose"><div class="eyebrow">HOSTDEALRADAR</div><h1>{heading}</h1>{body}</section>'
    methodology='<p class="lead">Every listed term comes from an official public provider page. We do not estimate missing prices or invent promotions.</p><h2>What is included</h2><ul><li>We retrieve public pages only when robots.txt allows it.</li><li>We record the source URL and capture time with every record.</li><li>A term is listed as current only when the latest source check reconfirmed that exact record. A promotion also needs a verified end date; a successful fetch alone does not establish that it is still valid.</li></ul><h2 id="display-order">How pages are ordered</h2><p>Provider grids follow the configured source-list order. Offer cards and comparison rows follow the captured record order in the latest dataset. Homepage examples require current records with an established renewal rate above the initial rate. We group them by currency, billing unit, and service label. If any explicitly identify a first-month rate, only those records are eligible for the example group; otherwise all eligible records are considered. We choose the group with the most eligible records; ties use alphabetical currency, unit, and label order. Up to three records from that group come first, ordered by the larger numeric change within each record; equal changes use the record identifier. The remaining positions, up to nine cards in total, follow stored record order, excluding those examples. The example count is recalculated for each published snapshot. Initial terms and plan resources can differ, so the examples do not establish equivalent plans or an amount a buyer would save. These display orders are not recommendations, quality rankings, price rankings, or value rankings.</p>'+category_guide(offers)+'<h2>What happens when a source cannot be checked</h2><ul><li>If a source is blocked, challenged, or unclear, we publish no new offer for it.</li><li>Unverified or earlier records retain their actual capture time and are not shown as current offers.</li><li>Expired promotions are labelled expired and are never shown as a current offer.</li></ul><h2>What to verify before purchase</h2><p>Confirm checkout total, tax, eligibility, billing term, and renewal amount with the provider. A captured offer is not a checkout test or a performance review.</p>'
    write(Path('methodology/index.html'),page('How we check | HostDealRadar','How HostDealRadar checks official source pages.',domain+'/methodology/',prose('How we check offers',methodology),{'@context':'https://schema.org','@type':'WebPage','name':'Methodology'}))
    about='<p class="lead">HostDealRadar publishes source-checked records of publicly available hosting terms.</p><p>Every listing links to the provider page it came from and carries its own capture time. We show a price, currency, billing unit, and renewal term only when the official page supports that field.</p><p>Source checks run every six hours. When a source cannot be checked, we do not publish a new price for it; earlier records stay labelled as earlier records instead of being presented as current.</p><p>HostDealRadar is maintained under the HostDealRadar name. It is not a hosting provider and does not sell hosting plans.</p><p>Read <a href="/methodology/">how we check sources</a> for the rules behind the records.</p>'
    write(Path('about/index.html'),page('About | HostDealRadar','What HostDealRadar records and how the site is maintained.',domain+'/about/',prose('About HostDealRadar',about),{'@context':'https://schema.org','@type':'AboutPage','name':'About HostDealRadar'}))
    contact='<p class="lead">Contact HostDealRadar about a source record, a correction, or a change on a provider page.</p><p>Email <a href="mailto:contact@hostdealradar.com">contact@hostdealradar.com</a>.</p><p>This address reaches the person who maintains HostDealRadar. For a record correction, include the page URL and the specific term that has changed so it can be checked against the official source.</p>'
    write(Path('contact/index.html'),page('Contact | HostDealRadar','Contact HostDealRadar about source records and corrections.',domain+'/contact/',prose('Contact',contact),{'@context':'https://schema.org','@type':'ContactPage','name':'Contact HostDealRadar'}))
    write(Path('disclosure/index.html'),page('Affiliate disclosure | HostDealRadar','Affiliate disclosure for HostDealRadar.',domain+'/disclosure/',prose('Affiliate disclosure','<p class="lead">HostDealRadar currently links to official provider pages and does not use affiliate links.</p><p>If we later use an approved affiliate link, the link and relevant page will say so clearly. We will not use cookie injection, self-referrals, brand-keyword ads, or links that break an affiliate program’s terms.</p>'),{'@context':'https://schema.org','@type':'WebPage','name':'Affiliate disclosure'}))
    write(Path('privacy/index.html'),page('Privacy | HostDealRadar','Privacy information for HostDealRadar.',domain+'/privacy/',prose('Privacy','<p class="lead">This static site does not require accounts or collect purchase details.</p><p>Provider links open their own sites, where their privacy policies apply. We do not use affiliate-cookie injection or sell visitor information.</p><p>HostDealRadar currently does not display third-party advertising or use affiliate links. If either is introduced, we will disclose it here and update this page.</p>'),{'@context':'https://schema.org','@type':'WebPage','name':'Privacy'}))
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

`agent_auth` metadata is published only to describe the future contract. The planned registration endpoint is `/agent-auth/register`, but agent registration is unavailable: it creates no account, issues no credential, and accepts no identity data. The sole planned registration method is `planned_contract_only`; it is not an available enrollment method. Until a real implementation exists, agents use the public read-only lookup without credentials.
'''
    agent_records=[agent_record(o, byid[o['provider']], states[o['slug']][0]) for o in offers]
    write(Path('agent-data.json'),json.dumps(agent_records,separators=(',',':')))
    write(Path('ai/index.md'),agent_markdown)
    # The discovery digest must describe the exact bytes a client receives;
    # write this generated Markdown without platform newline conversion.
    write_bytes(Path('ai/skills/site-lookup/SKILL.md'),skill_markdown.encode('utf-8'))
    write(Path('auth.md'),auth_markdown)
    write(Path('openapi.json'),json.dumps({'openapi':'3.1.0','info':{'title':'HostDealRadar public record lookup','version':'1.0.0','description':'Read-only lookup of public source records. It does not fetch third-party URLs, test checkout, or perform write operations.'},'servers':[{'url':domain}],'paths':{'/api/agent/lookup':{'get':{'summary':'Look up public records by exact provider ID or record slug','parameters':[{'name':'provider','in':'query','required':False,'schema':{'type':'string'},'description':'Exact provider ID or name; cannot be combined with slug.'},{'name':'slug','in':'query','required':False,'schema':{'type':'string'},'description':'Exact public record ID; cannot be combined with provider.'}],'responses':{'200':{'description':'One or more bounded public records.'},'400':{'description':'Missing or ambiguous query.'},'404':{'description':'No exact public record found.'},'405':{'description':'GET only.'}}}}}},separators=(',',':')))
    write(Path('.well-known/api-catalog.json'),json.dumps({'linkset':[{'anchor':domain+'/api/agent/lookup','service-desc':[{'href':domain+'/openapi.json','type':'application/json'}],'service-doc':[{'href':domain+'/ai/','type':'text/markdown'}]}]},separators=(',',':')))
    write(Path('.well-known/agent-skills/index.json'),json.dumps({'$schema':'https://schemas.agentskills.io/discovery/0.2.0/schema.json','skills':[{'name':'site-lookup','type':'skill-md','description':'Retrieve actual public HostDealRadar records by exact provider identifier or record slug.','url':domain+'/ai/skills/site-lookup/SKILL.md','digest':'sha256:'+hashlib.sha256(skill_markdown.encode('utf-8')).hexdigest()}]},separators=(',',':')))
    write(Path('.well-known/ai-catalog.json'),json.dumps({'specVersion':'1.0','host':{'displayName':cfg['site']['brand'],'identifier':'did:web:'+urlsplit(domain).hostname},'entries':[{'identifier':'urn:air:'+urlsplit(domain).hostname+':api:public-record-lookup','displayName':'HostDealRadar public record lookup','type':'application/vnd.oai.openapi+json','url':domain+'/openapi.json','representativeQueries':['look up a published HostDealRadar provider record','find the official source URL for a HostDealRadar record','retrieve a hosting price record by slug']},{'identifier':'urn:air:'+urlsplit(domain).hostname+':skill:site-lookup','displayName':'HostDealRadar site lookup skill','type':'text/markdown','url':domain+'/ai/skills/site-lookup/SKILL.md','representativeQueries':['how to query HostDealRadar public records','find records by exact hosting provider ID','understand HostDealRadar source-record limits']}]},separators=(',',':')))
    construction={'status':'under_construction','available':False,'capabilities_status':'planned_contract_only','message':'Coming soon; authentication is not available.','launch_date':None}
    write(Path('.well-known/oauth-authorization-server'),json.dumps({'issuer':domain,'authorization_endpoint':domain+'/agent-auth/authorize','token_endpoint':domain+'/agent-auth/token','jwks_uri':domain+'/.well-known/jwks.json','response_types_supported':['code'],'grant_types_supported':['authorization_code'],'agent_auth':{'skill':domain+'/auth.md','register_uri':domain+'/agent-auth/register','methods':[{'type':'planned_contract_only','available':False,'description':'Registration is not available.'}]},**construction},separators=(',',':')))
    write(Path('.well-known/oauth-protected-resource'),json.dumps({'resource':domain,'authorization_servers':[domain],'scopes_supported':['public:records:read'],'bearer_methods_supported':['header'],'planned_resource_endpoint':domain+'/api/agent/lookup',**construction},separators=(',',':')))
    write(Path('.well-known/jwks.json'),json.dumps({'keys':[],'status':'under_construction','message':'No token validation keys are active.'},separators=(',',':')))
    write(Path('_worker.js'),agent_worker())
    write(Path('robots.txt'),'User-agent: *\nAllow: /\nContent-Signal: ai-train=no, search=yes, ai-input=no\nAgentmap: '+domain+'/.well-known/ai-catalog.json\nSitemap: '+domain+'/sitemap.xml\n')
    write(Path('404.html'),page('Page not found | HostDealRadar','This page does not exist.',domain+'/404.html',prose('Page not found','<p><a href="/">Return to current offers</a></p>'),{'@context':'https://schema.org','@type':'WebPage','name':'Page not found'}))
    routes=['/','/providers/','/compare/','/methodology/','/about/','/contact/','/disclosure/','/privacy/',guide_route,namecheap_guide_route,cloudways_guide_route]
    routes+=[f'/providers/{p["id"]}/' for p in providers]
    routes+=[f'/deals/{o["slug"]}/' for o in offers]
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
    DATA.write_text(json.dumps(payload,indent=2)+'\n',encoding='utf-8')
    write(Path('sitemap.xml'),'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'+''.join('<url><loc>'+e(domain+path)+'</loc><lastmod>'+e(lastmod)+'</lastmod></url>' for path,lastmod in entries)+'</urlset>')
    return {'current':len(current),'history':len(history)}
if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--config'); parser.add_argument('--output'); args=parser.parse_args(); build(args.config,args.output); print('Static site built.')
