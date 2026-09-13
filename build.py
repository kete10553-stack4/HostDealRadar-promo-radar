import argparse, hashlib, html, json, re, shutil
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
              'login_or_region_gated':'This official page requires a login or is limited to certain regions.'}
STATE_ONLY_LEAD='Official page checked. No deterministic price rule is available, so no price is published.'
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
    if offer.get('discount_percent') is not None:
        return f"{offer['discount_percent']}% off"
    return 'Price not captured'
def period_text(offer):
    return offer.get('billing_period') or 'month'
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
        if o.get('commitment_months'): terms.append(f"{o['commitment_months']}-month term")
        if o.get('renewal_price') is not None: terms.append(f"Renews at {money(o['renewal_price'],o.get('currency','USD'))}/{period_text(o)}")
        if o.get('discount_percent') is not None: terms.append(f"{o['discount_percent']}% off shown")
        period = f' <span class="period">/ {e(period_text(o))}</span>' if o.get('price') is not None else ''
        label='Official price' if o.get('kind')=='regular_price' else 'Promotion'
        if historical: label={'retained':'Earlier record','stale':'Needs recheck','expired':'Expired'}.get(state,state.title())
        cls='card history' if historical else 'card'
        return f'''<article class="{cls}"><div class="card-top"><span class="provider-name">{e(p['name'])}</span><span class="tag">{label}</span></div><h3><a href="/deals/{e(o['slug'])}/">{e(o['title'])}</a></h3><p class="price">{e(price(o))}{period}</p><p class="summary">{e(o.get('category','Hosting'))}</p><dl>{''.join(f'<div><dt>{e(x.split(" ")[0])}</dt><dd>{e(x)}</dd></div>' for x in terms) or '<div><dt>Terms</dt><dd>See source</dd></div>'}</dl><a class="button" href="/deals/{e(o['slug'])}/">View terms</a><p class="capture">{e(message)}</p></article>'''
    def tile(p):
        count=sum(o['provider']==p['id'] for o in current)
        status = statuses.get(p['id'], {})
        label=state_only_display(status, blockers.get(p['id']))[2] if p['id'] in state_only else f'{count} current listings →'
        return f'<a class="provider-tile" href="/providers/{e(p["id"])}/"><strong>{e(p["name"])}</strong><p>{e(cfg["notes"].get(p["id"],"Official source"))}</p><span>{e(label)}</span></a>'
    provider_tiles=''.join(tile(p) for p in providers)
    shown=current[:9]
    home=template('index.html',month=datetime.now().strftime('%B %Y'),deal_count=len(current),provider_count=len(providers),updated=e('Last source snapshot: '+date_text(payload.get('generated_at','Unknown'))),offers='<div class="cards">'+''.join(card(o) for o in shown)+'</div>' if shown else '<div class="empty"><h3>No current offers are published</h3><p>We only show terms that were captured from an official source in the latest check. Check back after the next source run.</p></div>',providers=provider_tiles)
    home_schema={'@context':'https://schema.org','@type':'ItemList','name':'HostDealRadar official hosting offers','itemListElement':[{'@type':'ListItem','position':i+1,'item':schema_offer(o,domain+'/deals/'+o['slug']+'/')} for i,o in enumerate(current)]}
    write(Path('index.html'),page('HostDealRadar | Official hosting offers', 'Official hosting offers with terms and renewal prices in view.',domain+'/',home,home_schema))
    provider_listing='<section class="wrap section"><div class="eyebrow">OFFICIAL SOURCES</div><h1>Providers we check</h1><p class="lead">We include only providers whose public pages can be checked without bypassing restrictions.</p><div class="provider-grid">'+provider_tiles+'</div></section>'
    write(Path('providers/index.html'),page('Providers | HostDealRadar','Official hosting providers checked by HostDealRadar.',domain+'/providers/',provider_listing,{'@context':'https://schema.org','@type':'CollectionPage','name':'Providers'}))
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
        content=template('provider.html',provider=e(p['name']),note=e(cfg['notes'].get(p['id'],'')),source=e(p['source_url']),source_status=e(status_text),offers=current_html+history_html)
        write(Path('providers')/p['id']/'index.html',page(f'{p["name"]} offers | HostDealRadar',f'Official {p["name"]} hosting terms captured by HostDealRadar.',domain+'/providers/'+p['id']+'/',content,{'@context':'https://schema.org','@type':'CollectionPage','name':p['name']+' offers'}))
    for o in offers:
        state, message=states[o['slug']]
        advertised = price(o) + (' / ' + period_text(o) if o.get('price') is not None else '')
        p=byid[o['provider']]; terms=[('Listing type','Regular price; no discount claimed' if o.get('kind')=='regular_price' else 'Promotion'),('Advertised price',advertised),('Commitment',str(o['commitment_months'])+' months' if o.get('commitment_months') else 'Not captured'),('Renewal price',money(o['renewal_price'],o.get('currency','USD'))+'/'+period_text(o) if o.get('renewal_price') is not None else 'Not captured'),('Discount shown',str(o['discount_percent'])+'%' if o.get('discount_percent') is not None else 'Not captured'),('Coupon code',o.get('coupon_code','Not captured')),('Valid until',o.get('valid_until','Not captured')),('Captured at',o['fetched_at']),('Record state',state)]
        terms_html=''.join(f'<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>' for k,v in terms)
        rel='rel="noopener noreferrer"' if not p['affiliate_url'] else 'rel="sponsored noopener noreferrer"'
        disclosure='This is an official link; no affiliate relationship is active.' if not p['affiliate_url'] else 'This may be an affiliate link; we may earn a commission at no extra cost to you.'
        status_html=f'<p class="record-state state-{e(state)}"><strong>{e(state.title())}</strong> {e(message)}</p>'
        content=template('deal.html',provider=e(p['name']),provider_id=e(p['id']),category=e(o.get('category','Hosting')),offer_title=e(o['title']),summary=e(o.get('condition') or 'Terms captured from the official provider page.'),terms=terms_html,source_note=e(o['evidence']),source_url=e(o['source_url']),price=e(price(o)),billing=e('Billed under the provider terms.'),status=status_html,outbound=e(o['offer_url']),rel=rel,disclosure=e(disclosure))
        canonical=domain+'/deals/'+o['slug']+'/'
        # Only a current record may publish current-price structured data.
        schema={'@context':'https://schema.org','@type':'Product','name':o['title']}
        if state==CURRENT: schema['offers']=schema_offer(o,canonical)
        write(Path('deals')/o['slug']/'index.html',page(f'{o["title"]} | HostDealRadar',f'Official terms for {o["title"]}.',canonical,content,schema))
    def row(o):
        state,message=states[o['slug']]
        return f'<tr><td><strong>{e(byid[o["provider"]]["name"])}</strong><span>{e(o["title"])}</span></td><td>{e(price(o))}<span>{e("Regular price" if o.get("kind")=="regular_price" else "Promotion")}</span>{("<span>per "+e(period_text(o))+"</span>") if o.get("price") is not None else ""}</td><td>{e(str(o.get("commitment_months","Unknown")))}</td><td>{e(money(o["renewal_price"],o.get("currency","USD"))+"/"+period_text(o) if o.get("renewal_price") is not None else "Unknown")}</td><td>{e(state.title())}<span>{e(o["fetched_at"])}</span></td><td><a href="{e(o["source_url"])}" rel="noopener noreferrer">Official page ↗</a></td></tr>'
    rows=''.join(row(o) for o in current)
    history_rows=''.join(row(o) for o in history)
    history_html=''
    if history:
        history_html='<div class="table-wrap history-block"><h2>Earlier records, not current offers</h2><p class="muted">Captured earlier and not reconfirmed in the latest source check. Shown for reference with their own currency, billing period and capture time.</p><table><thead><tr><th>Provider / plan</th><th>Advertised price</th><th>Commitment</th><th>Renewal</th><th>State</th><th>Source</th></tr></thead><tbody>'+history_rows+'</tbody></table></div>'
    compare=template('compare.html',rows=rows,history=history_html,empty='' if current else '<div class="empty"><h3>No current offers available</h3><p>The latest source check did not confirm any publishable terms.</p></div>')
    write(Path('compare/index.html'),page('Compare terms | HostDealRadar','Compare hosting terms captured from official sources.',domain+'/compare/',compare,{'@context':'https://schema.org','@type':'WebPage','name':'Compare hosting terms'}))
    prose=lambda heading,body: f'<section class="wrap section prose"><div class="eyebrow">HOSTDEALRADAR</div><h1>{heading}</h1>{body}</section>'
    methodology='<p class="lead">Every listed term comes from an official public provider page. We do not estimate missing prices or invent promotions.</p><h2>What is included</h2><ul><li>We retrieve public pages only when robots.txt allows it.</li><li>We record the source URL and capture time with every record.</li><li>A term is listed as current only when the latest source check reconfirmed that exact record.</li></ul><h2>What happens when a source cannot be checked</h2><ul><li>If a source is blocked, challenged, or unclear, we publish no new offer for it.</li><li>Records captured earlier are kept as clearly labelled earlier records with their original capture time.</li><li>Earlier records are not shown as current offers and are not published as current price data.</li><li>Expired promotions are labelled expired and are never shown as a current offer.</li></ul><h2>What to verify before purchase</h2><p>Confirm checkout total, tax, eligibility, billing term, and renewal amount with the provider. A captured offer is not a checkout test or a performance review.</p>'
    write(Path('methodology/index.html'),page('How we check | HostDealRadar','How HostDealRadar checks official source pages.',domain+'/methodology/',prose('How we check offers',methodology),{'@context':'https://schema.org','@type':'WebPage','name':'Methodology'}))
    write(Path('disclosure/index.html'),page('Affiliate disclosure | HostDealRadar','Affiliate disclosure for HostDealRadar.',domain+'/disclosure/',prose('Affiliate disclosure','<p class="lead">HostDealRadar currently links to official provider pages and does not use affiliate links.</p><p>If we later use an approved affiliate link, the link and relevant page will say so clearly. We will not use cookie injection, self-referrals, brand-keyword ads, or links that break an affiliate program’s terms.</p>'),{'@context':'https://schema.org','@type':'WebPage','name':'Affiliate disclosure'}))
    write(Path('privacy/index.html'),page('Privacy | HostDealRadar','Privacy information for HostDealRadar.',domain+'/privacy/',prose('Privacy','<p class="lead">This static site does not require accounts or collect purchase details.</p><p>Provider links open their own sites, where their privacy policies apply. We do not use affiliate-cookie injection or sell visitor information.</p>'),{'@context':'https://schema.org','@type':'WebPage','name':'Privacy'}))
    write(Path('robots.txt'),'User-agent: *\nAllow: /\nSitemap: '+domain+'/sitemap.xml\n')
    write(Path('404.html'),page('Page not found | HostDealRadar','This page does not exist.',domain+'/404.html',prose('Page not found','<p><a href="/">Return to current offers</a></p>'),{'@context':'https://schema.org','@type':'WebPage','name':'Page not found'}))
    routes=['/','/providers/','/compare/','/methodology/','/disclosure/','/privacy/']
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
