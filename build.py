import argparse, html, json, shutil
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from config import ROOT, load_config

TEMPLATES=ROOT/'templates'; OUT=ROOT/'site'; DATA=ROOT/'data/offers.json'; ASSETS=ROOT/'assets'
def e(value): return html.escape(str(value or ''), quote=True)
def money(value, currency='USD'): return f'${float(value):,.2f}' if currency=='USD' else f'{currency} {float(value):,.2f}'
def date_text(value):
    try: return datetime.fromisoformat(value.replace('Z','+00:00')).strftime('%b %-d, %Y')
    except ValueError: return value
def write(path, text):
    target=OUT/path; target.parent.mkdir(parents=True,exist_ok=True); target.write_text(text,encoding='utf-8')
def template(name, **fields): return Template((TEMPLATES/name).read_text(encoding='utf-8')).safe_substitute(**fields)
def price(offer): return money(offer['price'], offer.get('currency','USD')) if offer.get('price') is not None else 'Price not captured'
def schema_offer(offer, canonical):
    item={'@type':'Offer','name':offer['title'],'url':canonical}
    if offer.get('price') is not None: item.update({'price':str(offer['price']),'priceCurrency':offer.get('currency','USD')})
    if offer.get('valid_until'): item['priceValidUntil']=offer['valid_until']
    return item
def build(config_path=None, output=None):
    global OUT
    if output: OUT=Path(output)
    cfg=load_config(config_path); domain=cfg['site']['domain'].rstrip('/'); payload=json.loads(DATA.read_text(encoding='utf-8'))
    byid={p['id']:p for p in cfg['providers']}; statuses=payload.get('source_status',{})
    offers=[o for o in payload.get('offers',[]) if o.get('provider') in byid and (not o.get('valid_until') or o['valid_until']>=datetime.now().date().isoformat())]
    if OUT.exists(): shutil.rmtree(OUT)
    shutil.copytree(ASSETS, OUT/'assets')
    def page(title, description, canonical, content, schema):
        return template('base.html',title=e(title),description=e(description),canonical=e(canonical),brand=e(cfg['site']['brand']),tagline=e(cfg['settings']['tagline']),repo=e(cfg['settings']['repo_url']),social_image='',footer_status=e('Data source checks are automated.'),content=content,schema=json.dumps(schema,separators=(',',':')))
    def card(o):
        p=byid[o['provider']]; terms=[]
        if o.get('commitment_months'): terms.append(f"{o['commitment_months']}-month term")
        if o.get('renewal_price') is not None: terms.append(f"Renews at {money(o['renewal_price'])}/mo")
        if o.get('discount_percent') is not None: terms.append(f"{o['discount_percent']}% off shown")
        return f'''<article class="card"><div class="card-top"><span class="provider-name">{e(p['name'])}</span><span class="tag">Official source</span></div><h3><a href="/deals/{e(o['slug'])}/">{e(o['title'])}</a></h3><p class="price">{price(o)} <span class="period">/ {e(o.get('billing_period','month'))}</span></p><p class="summary">{e(o.get('category','Hosting'))}</p><dl>{''.join(f'<div><dt>{e(x.split(" ")[0])}</dt><dd>{e(x)}</dd></div>' for x in terms) or '<div><dt>Terms</dt><dd>See source</dd></div>'}</dl><a class="button" href="/deals/{e(o['slug'])}/">View terms</a><p class="capture">Captured {e(date_text(o['fetched_at']))}</p></article>'''
    shown=offers[:9]; provider_tiles=''.join(f'<a class="provider-tile" href="/providers/{e(p["id"])}/"><strong>{e(p["name"])}</strong><p>{e(cfg["notes"].get(p["id"],"Official source"))}</p><span>{sum(o["provider"]==p["id"] for o in offers)} current offers →</span></a>' for p in cfg['providers'])
    home=template('index.html',month=datetime.now().strftime('%B %Y'),deal_count=len(offers),provider_count=len(cfg['providers']),updated=e('Last source snapshot: '+date_text(payload.get('generated_at','Unknown'))),offers='<div class="cards">'+''.join(card(o) for o in shown)+'</div>' if shown else '<div class="empty"><h3>No current offers are published</h3><p>We only show terms that were captured from an official source. Check back after the next source run.</p></div>',providers=provider_tiles)
    home_schema={'@context':'https://schema.org','@type':'ItemList','name':'HostDealRadar official hosting offers','itemListElement':[{'@type':'ListItem','position':i+1,'item':schema_offer(o,domain+'/deals/'+o['slug']+'/')} for i,o in enumerate(offers)]}
    write(Path('index.html'),page('HostDealRadar | Official hosting offers', 'Official hosting offers with terms and renewal prices in view.',domain+'/',home,home_schema))
    provider_listing='<section class="wrap section"><div class="eyebrow">OFFICIAL SOURCES</div><h1>Providers we check</h1><p class="lead">We include only providers whose public pages can be checked without bypassing restrictions.</p><div class="provider-grid">'+provider_tiles+'</div></section>'
    write(Path('providers/index.html'),page('Providers | HostDealRadar','Official hosting providers checked by HostDealRadar.',domain+'/providers/',provider_listing,{'@context':'https://schema.org','@type':'CollectionPage','name':'Providers'}))
    for p in cfg['providers']:
        po=[o for o in offers if o['provider']==p['id']]; status=statuses.get(p['id'],{'status':'not checked','reason':'No source check has run yet.'})
        status_text='Source checked successfully.' if status['status']=='checked' else e(status['reason'])
        content=template('provider.html',provider=e(p['name']),note=e(cfg['notes'].get(p['id'],'')),source=e(p['source_url']),source_status=e(status_text),offers='<div class="cards">'+''.join(card(o) for o in po)+'</div>' if po else '<div class="empty"><h3>No offer published for this source</h3><p>'+e(status['reason'])+'</p></div>')
        write(Path('providers')/p['id']/'index.html',page(f'{p["name"]} offers | HostDealRadar',f'Official {p["name"]} hosting terms captured by HostDealRadar.',domain+'/providers/'+p['id']+'/',content,{'@context':'https://schema.org','@type':'CollectionPage','name':p['name']+' offers'}))
    for o in offers:
        p=byid[o['provider']]; terms=[('Advertised price',price(o)+' / '+o.get('billing_period','month')),('Commitment',str(o['commitment_months'])+' months' if o.get('commitment_months') else 'Not captured'),('Renewal price',money(o['renewal_price'])+'/mo' if o.get('renewal_price') is not None else 'Not captured'),('Discount shown',str(o['discount_percent'])+'%' if o.get('discount_percent') is not None else 'Not captured'),('Coupon code',o.get('coupon_code','Not captured')),('Valid until',o.get('valid_until','Not captured'))]
        terms_html=''.join(f'<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>' for k,v in terms)
        rel='rel="noopener noreferrer"' if not p['affiliate_url'] else 'rel="sponsored noopener noreferrer"'
        disclosure='This is an official link; no affiliate relationship is active.' if not p['affiliate_url'] else 'This may be an affiliate link; we may earn a commission at no extra cost to you.'
        content=template('deal.html',provider=e(p['name']),provider_id=e(p['id']),category=e(o.get('category','Hosting')),offer_title=e(o['title']),summary=e(o.get('condition') or o.get('evidence','Terms were captured from the official source.')),terms=terms_html,source_note=e(o['evidence']),source_url=e(o['source_url']),price=e(price(o)),billing=e('Billed under the provider terms.'),status='',outbound=e(o['offer_url']),rel=rel,disclosure=e(disclosure))
        canonical=domain+'/deals/'+o['slug']+'/'
        write(Path('deals')/o['slug']/'index.html',page(f'{o["title"]} | HostDealRadar',f'Official terms for {o["title"]}.',canonical,content,{'@context':'https://schema.org','@type':'Product','name':o['title'],'offers':schema_offer(o,canonical)}))
    rows=''.join(f'<tr><td><strong>{e(byid[o["provider"]]["name"])}</strong><span>{e(o["title"])}</span></td><td>{e(price(o))}<span>per {e(o.get("billing_period","month"))}</span></td><td>{e(str(o.get("commitment_months","Unknown")))}</td><td>{e(money(o["renewal_price"])+"/mo" if o.get("renewal_price") is not None else "Unknown")}</td><td><a href="{e(o["source_url"])}" rel="noopener noreferrer">Official page ↗</a></td></tr>' for o in offers)
    compare=template('compare.html',rows=rows,empty='' if offers else '<div class="empty"><h3>No offers available</h3><p>Current sources did not yield publishable terms.</p></div>')
    write(Path('compare/index.html'),page('Compare terms | HostDealRadar','Compare hosting terms captured from official sources.',domain+'/compare/',compare,{'@context':'https://schema.org','@type':'WebPage','name':'Compare hosting terms'}))
    prose=lambda heading,body: f'<section class="wrap section prose"><div class="eyebrow">HOSTDEALRADAR</div><h1>{heading}</h1>{body}</section>'
    write(Path('methodology/index.html'),page('How we check | HostDealRadar','How HostDealRadar checks official source pages.',domain+'/methodology/',prose('How we check offers','<p class="lead">Every listed term comes from an official public provider page. We do not estimate missing prices or invent promotions.</p><h2>What is included</h2><ul><li>We retrieve public pages only when robots.txt allows it.</li><li>We record the source URL and capture time with each offer.</li><li>When a source is blocked, challenged, expired, or unclear, we publish no offer.</li></ul><h2>What to verify before purchase</h2><p>Confirm checkout total, tax, eligibility, billing term, and renewal amount with the provider. A captured offer is not a checkout test or a performance review.</p>'),{'@context':'https://schema.org','@type':'WebPage','name':'Methodology'}))
    write(Path('disclosure/index.html'),page('Affiliate disclosure | HostDealRadar','Affiliate disclosure for HostDealRadar.',domain+'/disclosure/',prose('Affiliate disclosure','<p class="lead">HostDealRadar currently links to official provider pages and does not use affiliate links.</p><p>If we later use an approved affiliate link, the link and relevant page will say so clearly. We will not use cookie injection, self-referrals, brand-keyword ads, or links that break an affiliate program’s terms.</p>'),{'@context':'https://schema.org','@type':'WebPage','name':'Affiliate disclosure'}))
    write(Path('privacy/index.html'),page('Privacy | HostDealRadar','Privacy information for HostDealRadar.',domain+'/privacy/',prose('Privacy','<p class="lead">This static site does not require accounts or collect purchase details.</p><p>Provider links open their own sites, where their privacy policies apply. We do not use affiliate-cookie injection or sell visitor information.</p>'),{'@context':'https://schema.org','@type':'WebPage','name':'Privacy'}))
    write(Path('robots.txt'),'User-agent: *\nAllow: /\nSitemap: '+domain+'/sitemap.xml\n')
    paths=['/','/providers/','/compare/','/methodology/','/disclosure/','/privacy/']+[f'/providers/{p["id"]}/' for p in cfg['providers']]+[f'/deals/{o["slug"]}/' for o in offers]
    write(Path('sitemap.xml'),'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'+''.join('<url><loc>'+e(domain+x)+'</loc></url>' for x in paths)+'</urlset>')
    write(Path('404.html'),page('Page not found | HostDealRadar','This page does not exist.',domain+'/404.html',prose('Page not found','<p><a href="/">Return to current offers</a></p>'),{'@context':'https://schema.org','@type':'WebPage','name':'Page not found'}))
if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--config'); parser.add_argument('--output'); args=parser.parse_args(); build(args.config,args.output); print('Static site built.')
