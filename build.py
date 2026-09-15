import argparse, hashlib, html, json, re, shutil, math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from config import ROOT, load_config

TEMPLATES=ROOT/'templates'; OUT=ROOT/'site'; DATA=ROOT/'data/offers.json'; ASSETS=ROOT/'assets'
# Sitemap lastmod state lives inside the payload the refresh workflow already
# commits, so persisting it needs no change to the workflow or extra token scope.
STATE_KEY='page_lastmod'
CURRENT='current'; HISTORY=('retained','stale','expired')
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
    if status.get('status') == 'available_no_price_rule':
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
def schema_offer(offer, canonical):
    item={'@type':'Offer','name':offer['title'],'url':canonical}
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
    status = statuses.get(offer['provider'], {}) or {}
    captured_slugs = status.get('captured_slugs')
    if captured_slugs is not None and offer['slug'] not in captured_slugs:
        return 'retained', 'Not reconfirmed in the latest source check. Last captured ' + offer['fetched_at'] + '.'
    if status.get('status') != 'checked':
        return 'stale', 'Latest source check did not complete. Last captured ' + offer['fetched_at'] + '.'
    try:
        captured = datetime.fromisoformat(offer['fetched_at'].replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return 'stale', 'Capture time could not be read.'
    if (datetime.now(timezone.utc) - captured).total_seconds() > settings['fresh_hours'] * 3600:
        return 'stale', 'Needs recheck. Last captured ' + offer['fetched_at'] + '.'
    return 'current', 'Captured ' + offer['fetched_at'] + '. Confirm current terms with the provider.'

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
    def page(title, description, canonical, content, schema):
        return template('base.html',title=e(title),description=e(description),canonical=e(canonical),brand=e(cfg['site']['brand']),tagline=e(cfg['settings']['tagline']),repo=e(cfg['settings']['repo_url']),social_image='',footer_status=e('Data source checks are automated.'),content=content,schema=json.dumps(schema,separators=(',',':')))
    def card(o, historical=False):
        p=byid[o['provider']]; state, message=states[o['slug']]; terms=[]
        rule=rules.get((o['provider'], o['title']), {})
        if o.get('commitment_months'): terms.append(f"{o['commitment_months']}-month term")
        label='Official price' if o.get('kind')=='regular_price' else 'Promotion'
        if historical: label={'retained':'Earlier record','stale':'Needs recheck','expired':'Expired'}.get(state,state.title())
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
        elif status.get('status') == 'checked':
            label=f'{count} current listings →'
        elif status.get('status') == 'unmatched':
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
    home_schema={'@context':'https://schema.org','@type':'ItemList','name':'HostDealRadar official hosting offers','itemListElement':[{'@type':'ListItem','position':i+1,'item':schema_offer(o,domain+'/deals/'+o['slug']+'/')} for i,o in enumerate(current)]}
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
            status_text='Source checked successfully.' if status['status']=='checked' else e(status['reason'])
            current_html='<div class="cards">'+''.join(card(o) for o in po)+'</div>' if po else '<div class="empty"><h3>No current offer is published for this source</h3><p>'+e(status['reason'])+'</p></div>'
        history_html=''
        if ph:
            history_html='<section class="history-block"><h2>Earlier records kept for reference</h2><p class="muted">These records were captured on the dates shown and were not reconfirmed in the latest source check. They are not current offers and carry no current price data.</p><div class="cards">'+''.join(card(o,historical=True) for o in ph)+'</div></section>'
        note=provider_summary(po, ph)+' The summary uses the first current record, or the first earlier record if none is current. Cards in each section follow stored record order; this is not a recommendation or a ranking of price, quality, or value.'
        guide_templates={'godaddy':'provider-guide.html','namecheap':'namecheap-provider-guide.html'}
        related_guide=template(guide_templates[p['id']]) if p['id'] in guide_templates else ''
        content=template('provider.html',provider=e(p['name']),note=e(note),source=e(p['source_url']),source_status=e(status_text),related_guide=related_guide,offers=current_html+history_html)
        write(Path('providers')/p['id']/'index.html',page(f'{p["name"]} offers | HostDealRadar',f'Official {p["name"]} hosting terms captured by HostDealRadar.',domain+'/providers/'+p['id']+'/',content,{'@context':'https://schema.org','@type':'CollectionPage','name':p['name']+' offers'}))
    guide_route='/guides/godaddy-renewal-coupon/'
    guide=template('godaddy-renewal-coupon.html')
    guide_schema={'@context':'https://schema.org','@type':'Article','headline':'GoDaddy renewal coupon: do renewal promo codes work?','datePublished':'2026-09-15','dateModified':'2026-09-15','author':{'@type':'Organization','name':'HostDealRadar'},'publisher':{'@type':'Organization','name':'HostDealRadar'},'mainEntityOfPage':domain+guide_route}
    write(Path('guides/godaddy-renewal-coupon/index.html'),page('GoDaddy renewal coupon: do renewal promo codes work? | HostDealRadar','GoDaddy renewal coupons, customer-specific renewal codes, current .com renewal terms, and three linked user reports.',domain+guide_route,guide,guide_schema))
    namecheap_guide_route='/guides/namecheap-domain-renewal-coupon/'
    namecheap_guide=template('namecheap-domain-renewal-coupon.html')
    namecheap_guide_schema={'@context':'https://schema.org','@type':'Article','headline':'Namecheap domain renewal coupon: what works at renewal?','datePublished':'2026-09-15','dateModified':'2026-09-15','author':{'@type':'Organization','name':'HostDealRadar'},'publisher':{'@type':'Organization','name':'HostDealRadar'},'mainEntityOfPage':domain+namecheap_guide_route}
    write(Path('guides/namecheap-domain-renewal-coupon/index.html'),page('Namecheap domain renewal coupon: what works at renewal? | HostDealRadar','Namecheap renewal coupons, current .com renewal pricing, official terms, and three linked user reports.',domain+namecheap_guide_route,namecheap_guide,namecheap_guide_schema))
    for o in offers:
        state, message=states[o['slug']]
        rule=rules.get((o['provider'], o['title']), {})
        advertised = price(o) + (' / ' + period_text(o) if o.get('price') is not None else '')
        p=byid[o['provider']]; terms=[('Listing type','Regular price; no discount claimed' if o.get('kind')=='regular_price' else 'Promotion'),('Advertised price',advertised),('Commitment',str(o['commitment_months'])+' months' if o.get('commitment_months') else 'Unknown'),('Renewal price',displayed_rate(o,'renewal_price') if renewal_supported(o,rule) else 'Unknown'),('Coupon code',o.get('coupon_code') or 'Unknown'),('Valid until',o.get('valid_until') or 'Unknown'),('Captured at',o['fetched_at']),('Record state',state)]
        terms_html=''.join(f'<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>' for k,v in terms)
        rel='rel="noopener noreferrer"' if not p['affiliate_url'] else 'rel="sponsored noopener noreferrer"'
        disclosure='This is an official link; no affiliate relationship is active.' if not p['affiliate_url'] else 'This may be an affiliate link; we may earn a commission at no extra cost to you.'
        status_html=f'<p class="record-state state-{e(state)}"><strong>{e(state.title())}</strong> {e(message)}</p>'
        content=template('deal.html',provider=e(p['name']),provider_id=e(p['id']),category=e(o.get('category','Hosting')),offer_title=e(o['title']),summary=e(public_terms(o.get('condition') or 'Terms captured from the official provider page.')),rate_pair=rate_pair(o,rule),terms=terms_html,source_note=e(public_terms(o['evidence'])),source_url=e(o['source_url']),price=e(price(o)),billing=e('Billed under the provider terms.'),status=status_html,outbound=e(o['offer_url']),rel=rel,disclosure=e(disclosure))
        canonical=domain+'/deals/'+o['slug']+'/'
        # Only a current record may publish current-price structured data.
        schema={'@context':'https://schema.org','@type':'Product','name':o['title']}
        if state==CURRENT: schema['offers']=schema_offer(o,canonical)
        write(Path('deals')/o['slug']/'index.html',page(f'{o["title"]} | HostDealRadar',f'Official terms for {o["title"]}.',canonical,content,schema))
    def row(o):
        state,message=states[o['slug']]
        rule=rules.get((o['provider'], o['title']), {})
        renewal=displayed_rate(o,'renewal_price') if renewal_supported(o,rule) else 'Unknown'
        return f'<tr><td><strong>{e(byid[o["provider"]]["name"])}</strong><span>{e(o["title"])}</span></td><td>{e(displayed_rate(o,"price") if o.get("price") is not None else price(o))}<span>{e(initial_label(o,rule))}</span>{rate_source(o)}</td><td>{e(str(o.get("commitment_months") or "Unknown"))}</td><td>{e(renewal)}<br>{rate_source(o)}</td><td>{e(state.title())}<span>{e(o["fetched_at"])}</span></td><td><a href="{e(o["source_url"])}" rel="noopener noreferrer">Official page ↗</a></td></tr>'
    rows=''.join(row(o) for o in current)
    history_rows=''.join(row(o) for o in history)
    history_html=''
    if history:
        history_html='<div class="table-wrap history-block"><h2>Earlier records, not current offers</h2><p class="muted">Captured earlier and not reconfirmed in the latest source check. Shown in captured record order for reference with their own currency, billing period and capture time. This is not a ranking or recommendation.</p><table><thead><tr><th>Provider / plan</th><th>Advertised price</th><th>Commitment</th><th>Renewal</th><th>State</th><th>Source</th></tr></thead><tbody>'+history_rows+'</tbody></table></div>'
    compare=template('compare.html',rows=rows,history=history_html,empty='' if current else '<div class="empty"><h3>No current offers available</h3><p>The latest source check did not confirm any publishable terms.</p></div>')
    write(Path('compare/index.html'),page('Compare terms | HostDealRadar','Compare hosting terms captured from official sources.',domain+'/compare/',compare,{'@context':'https://schema.org','@type':'WebPage','name':'Compare hosting terms'}))
    prose=lambda heading,body: f'<section class="wrap section prose"><div class="eyebrow">HOSTDEALRADAR</div><h1>{heading}</h1>{body}</section>'
    methodology='<p class="lead">Every listed term comes from an official public provider page. We do not estimate missing prices or invent promotions.</p><h2>What is included</h2><ul><li>We retrieve public pages only when robots.txt allows it.</li><li>We record the source URL and capture time with every record.</li><li>A term is listed as current only when the latest source check reconfirmed that exact record.</li></ul><h2 id="display-order">How pages are ordered</h2><p>Provider grids follow the configured source-list order. Offer cards and comparison rows follow the captured record order in the latest dataset. Homepage examples require current records with an established renewal rate above the initial rate. We group them by currency, billing unit, and service label. If any explicitly identify a first-month rate, only those records are eligible for the example group; otherwise all eligible records are considered. We choose the group with the most eligible records; ties use alphabetical currency, unit, and label order. Up to three records from that group come first, ordered by the larger numeric change within each record; equal changes use the record identifier. The remaining positions, up to nine cards in total, follow stored record order, excluding those examples. The example count is recalculated for each published snapshot. Initial terms and plan resources can differ, so the examples do not establish equivalent plans or an amount a buyer would save. These display orders are not recommendations, quality rankings, price rankings, or value rankings.</p>'+category_guide(offers)+'<h2>What happens when a source cannot be checked</h2><ul><li>If a source is blocked, challenged, or unclear, we publish no new offer for it.</li><li>Records captured earlier are kept as clearly labelled earlier records with their original capture time.</li><li>Earlier records are not shown as current offers and are not published as current price data.</li><li>Expired promotions are labelled expired and are never shown as a current offer.</li></ul><h2>What to verify before purchase</h2><p>Confirm checkout total, tax, eligibility, billing term, and renewal amount with the provider. A captured offer is not a checkout test or a performance review.</p>'
    write(Path('methodology/index.html'),page('How we check | HostDealRadar','How HostDealRadar checks official source pages.',domain+'/methodology/',prose('How we check offers',methodology),{'@context':'https://schema.org','@type':'WebPage','name':'Methodology'}))
    about='<p class="lead">HostDealRadar publishes source-checked records of publicly available hosting terms.</p><p>Every listing links to the provider page it came from and carries its own capture time. We show a price, currency, billing unit, and renewal term only when the official page supports that field.</p><p>Source checks run every six hours. When a source cannot be checked, we do not publish a new price for it; earlier records stay labelled as earlier records instead of being presented as current.</p><p>HostDealRadar is maintained under the HostDealRadar name. It is not a hosting provider and does not sell hosting plans.</p><p>Read <a href="/methodology/">how we check sources</a> for the rules behind the records.</p>'
    write(Path('about/index.html'),page('About | HostDealRadar','What HostDealRadar records and how the site is maintained.',domain+'/about/',prose('About HostDealRadar',about),{'@context':'https://schema.org','@type':'AboutPage','name':'About HostDealRadar'}))
    contact='<p class="lead">Contact HostDealRadar about a source record, a correction, or a change on a provider page.</p><p>Email <a href="mailto:contact@hostdealradar.com">contact@hostdealradar.com</a>.</p><p>This address reaches the person who maintains HostDealRadar. For a record correction, include the page URL and the specific term that has changed so it can be checked against the official source.</p>'
    write(Path('contact/index.html'),page('Contact | HostDealRadar','Contact HostDealRadar about source records and corrections.',domain+'/contact/',prose('Contact',contact),{'@context':'https://schema.org','@type':'ContactPage','name':'Contact HostDealRadar'}))
    write(Path('disclosure/index.html'),page('Affiliate disclosure | HostDealRadar','Affiliate disclosure for HostDealRadar.',domain+'/disclosure/',prose('Affiliate disclosure','<p class="lead">HostDealRadar currently links to official provider pages and does not use affiliate links.</p><p>If we later use an approved affiliate link, the link and relevant page will say so clearly. We will not use cookie injection, self-referrals, brand-keyword ads, or links that break an affiliate program’s terms.</p>'),{'@context':'https://schema.org','@type':'WebPage','name':'Affiliate disclosure'}))
    write(Path('privacy/index.html'),page('Privacy | HostDealRadar','Privacy information for HostDealRadar.',domain+'/privacy/',prose('Privacy','<p class="lead">This static site does not require accounts or collect purchase details.</p><p>Provider links open their own sites, where their privacy policies apply. We do not use affiliate-cookie injection or sell visitor information.</p><p>HostDealRadar currently does not display third-party advertising or use affiliate links. If either is introduced, we will disclose it here and update this page.</p>'),{'@context':'https://schema.org','@type':'WebPage','name':'Privacy'}))
    write(Path('robots.txt'),'User-agent: *\nAllow: /\nSitemap: '+domain+'/sitemap.xml\n')
    write(Path('404.html'),page('Page not found | HostDealRadar','This page does not exist.',domain+'/404.html',prose('Page not found','<p><a href="/">Return to current offers</a></p>'),{'@context':'https://schema.org','@type':'WebPage','name':'Page not found'}))
    routes=['/','/providers/','/compare/','/methodology/','/about/','/contact/','/disclosure/','/privacy/',guide_route,namecheap_guide_route]
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
