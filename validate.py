import json, re, shutil, tempfile, xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlsplit, unquote
from html.parser import HTMLParser
from config import ROOT, load_config
import build

NS={'sm':'http://www.sitemaps.org/schemas/sitemap/0.9'}
# Approved reasons a configured source can carry no deterministic price rule.
# A robots failure is kept separate from failures after a page was fetched.
BLOCKERS={'price_rendered_by_js','unstable_field_structure','no_public_price','login_or_region_gated','source_page_forbidden','robots_check_failed'}

class Links(HTMLParser):
    def __init__(self): super().__init__(); self.urls=[]
    def handle_starttag(self,tag,attrs):
        self.urls += [v for k,v in attrs if k in ('href','src') and v and v.startswith('/')]

def schemas(text):
    return [json.loads(raw) for raw in re.findall(r'<script type="application/ld\+json">(.*?)</script>', text)]

def sitemap_lastmods(path=None):
    root=ET.parse(path or (ROOT/'site/sitemap.xml')).getroot()
    return {e.findtext('sm:loc',default='',namespaces=NS): e.findtext('sm:lastmod',default='',namespaces=NS) for e in root.findall('sm:url',NS)}

def check_earlier_record_path(payload, cfg):
    """Exercise the earlier-record path deterministically.

    When every source happens to be healthy there is no retained record, so the
    release blockers about retained/stale rendering would otherwise go untested.
    This marks one captured slug as retained in an isolated copy, switches that
    record to a non-USD annual currency to prove the comparison view no longer
    hard-codes USD/month, and asserts the record leaves the current lists.
    """
    probe=json.loads(json.dumps(payload))
    settings=cfg['settings']
    current=[o for o in probe['offers'] if build.record_state(o,probe['source_status'],settings)[0]==build.CURRENT]
    assert current, 'Need at least one current record to exercise the earlier-record path'
    victim=current[0]
    for status in probe['source_status'].values():
        if victim['slug'] in status.get('captured_slugs',[]):
            status['captured_slugs'].remove(victim['slug']); status['retained_slugs'].append(victim['slug'])
            status['captured_count']=len(status['captured_slugs']); status['retained_count']=len(status['retained_slugs'])
    victim['currency']='EUR'; victim['billing_period']='year'; victim['renewal_price']=42.5
    tmp=Path(tempfile.mkdtemp()); data=tmp/'offers.json'
    data.write_text(json.dumps(probe,indent=2)+'\n',encoding='utf-8')
    out=tmp/'site'; saved=(build.DATA,build.OUT); build.DATA=data
    try:
        build.build(None,out)
    finally:
        build.DATA,build.OUT=saved
    home=(out/'index.html').read_text(encoding='utf-8')
    assert f'/deals/{victim["slug"]}/' not in home, 'A retained record still appears in the current offers list'
    item_list=[s for s in schemas(home) if s.get('@type')=='ItemList'][0]
    assert victim['slug'] not in json.dumps(item_list), 'A retained record still appears in homepage price data'
    deal=(out/'deals'/victim['slug']/'index.html').read_text(encoding='utf-8')
    assert not [s for s in schemas(deal) if s.get('@type')=='Product'], 'A retained record still publishes Product structured data'
    assert [s for s in schemas(deal) if s.get('@type')=='WebPage'], 'A retained record has no WebPage structured data'
    assert 'Not reconfirmed' in deal, 'A retained record is not labelled with its actual capture state'
    provider_page=(out/'providers'/victim['provider']/'index.html').read_text(encoding='utf-8')
    history_block=provider_page.split('<article class="card history">')[-1]
    assert f'/deals/{victim["slug"]}/' in history_block, 'A retained record is missing from the earlier-records block on its provider page'
    compare=(out/'compare/index.html').read_text(encoding='utf-8')
    marker='Earlier records, not current offers'
    assert marker in compare, 'Comparison page does not separate earlier records'
    current_table, history_table = compare.split(marker,1)
    names={p['id']:p['name'] for p in cfg['providers']}
    cell=f'<strong>{build.e(names[victim["provider"]])}</strong><span>{build.e(victim["title"])}</span>'
    assert cell in history_table, 'The retained record is missing from the earlier-records table'
    assert cell not in current_table, 'The retained record still appears in the current comparison table'
    assert 'EUR' in history_table and '/year' in history_table, 'Comparison renewal still ignores the record currency and billing period'
    shutil.rmtree(tmp,ignore_errors=True)

def check():
    cfg=load_config(); settings=cfg['settings']; payload=json.loads((ROOT/'data/offers.json').read_text(encoding='utf-8'))
    statuses=payload.get('source_status',{})
    assert {p['id'] for p in cfg['providers']} <= {r['provider'] for r in cfg['extractors']}, 'Every configured provider needs an extraction or availability rule'
    assert {p['id'] for p in cfg['providers']} == set(statuses), 'Every configured provider needs a recorded source status'
    assert all(o.get('price') != 0 for o in payload.get('offers', [])), 'A zero price must be represented as source text, not a monthly price'
    ids={p['id'] for p in cfg['providers']}
    # The invariant is evidence, not output. Every configured provider must have
    # been opened once and must carry a specific reason. "Reachable official page,
    # but no deterministic rule can be written" is a legitimate state-only
    # landing, not a placeholder; a name that never opened a real page is.
    assert {o['provider'] for o in payload['offers']} <= ids, 'A published record references a provider outside the configured list'
    for provider_id, status in statuses.items():
        assert status.get('status'), f'{provider_id} has no recorded status'
        assert status.get('reason'), f'{provider_id} has no recorded reason'
    for rule in cfg['extractors']:
        if rule.get('mode') != 'availability_only':
            continue
        assert rule.get('blocker') in BLOCKERS, f'{rule["provider"]} state-only rule needs a blocker from {sorted(BLOCKERS)}'
        assert rule.get('blocker_evidence'), f'{rule["provider"]} state-only rule needs blocker_evidence (the URL and what was seen)'
    state_only={rule['provider'] for rule in cfg['extractors'] if rule.get('mode')=='availability_only'}
    state_only={pid for pid in state_only if not any(r.get('mode')!='availability_only' for r in cfg['extractors'] if r.get('provider')==pid)}
    for pid in sorted(state_only):
        assert pid not in {o['provider'] for o in payload['offers']}, f'A state-only provider published an offer: {pid}'
    for offer in payload['offers']:
        assert offer.get('source_url') and offer.get('evidence'), 'Missing source attribution'
        assert any(offer.get(k) for k in ('price','price_text','discount_percent','coupon_code')), 'Empty offer'
        if offer.get('price') is not None:
            assert offer['price'] > 0 and offer.get('currency') and offer.get('billing_period'), 'Incomplete price terms'
        assert datetime.fromisoformat(offer['fetched_at'].replace('Z','+00:00')) <= datetime.now(timezone.utc), 'Capture time is in the future'
    # Per-record state must be recorded for every slug, not only per provider.
    for provider_id, status in statuses.items():
        for key in ('captured_slugs','retained_slugs'):
            assert isinstance(status.get(key), list), f'{provider_id} is missing {key}'
        assert not (set(status['captured_slugs']) & set(status['retained_slugs'])), f'{provider_id} lists a slug as both captured and retained'
        assert status.get('captured_count',0) == len(status['captured_slugs']), f'{provider_id} captured_count disagrees with captured_slugs'
        assert status.get('retained_count',0) == len(status['retained_slugs']), f'{provider_id} retained_count disagrees with retained_slugs'
    known={o['slug'] for o in payload['offers']}
    for provider_id, status in statuses.items():
        assert set(status['captured_slugs']) | set(status['retained_slugs']) <= known, f'{provider_id} references an unknown slug'
    states={o['slug']: build.record_state(o,statuses,settings)[0] for o in payload['offers']}
    current=[o for o in payload['offers'] if states[o['slug']]==build.CURRENT]
    history=[o for o in payload['offers'] if states[o['slug']]!=build.CURRENT]
    build.build()
    files=list((ROOT/'site').rglob('*.html'))
    assert files, 'No HTML was built'
    for f in files:
        text=f.read_text(encoding='utf-8')
        assert '$' not in re.sub(r'\$[0-9.,]+','',text), f'Unfilled template in {f}'
        assert '<script type="application/ld+json">' in text, f'Missing JSON-LD in {f}'
        schemas(text)
        links=Links(); links.feed(text)
        for link in links.urls:
            target=ROOT/'site'/unquote(urlsplit(link).path).lstrip('/')
            assert target.is_file() or (target/'index.html').is_file(), f'Broken internal link in {f}: {link}'
    for offer in payload['offers']:
        deal=(ROOT/'site/deals'/offer['slug']/'index.html').read_text(encoding='utf-8')
        provider_name=next(p['name'] for p in cfg['providers'] if p['id']==offer['provider'])
        display_name=build.offer_name(provider_name, offer['title'])
        assert f'<title>{build.e(display_name)}: official terms | HostDealRadar</title>' in deal, f'Deal title lacks provider-specific identity: {offer["slug"]}'
        assert f'<h1>{build.e(display_name)}</h1>' in deal, f'Deal heading lacks provider-specific identity: {offer["slug"]}'
        schema=schemas(deal)[0]
        assert schema.get('name') == display_name, f'Structured-data name lacks provider-specific identity: {offer["slug"]}'
        if states[offer['slug']]==build.CURRENT and offer.get('price') is not None:
            assert schema.get('@type')=='Product', f'Priced current record lacks Product markup: {offer["slug"]}'
            assert schema.get('description') and build.e(schema['description']) in deal, f'Product description is not visible page text: {offer["slug"]}'
            structured_offer=schema.get('offers') or {}
            assert structured_offer.get('@type')=='Offer', f'Priced current Product lacks Offer: {offer["slug"]}'
            assert structured_offer.get('price') not in (None,''), f'Product Offer lacks price: {offer["slug"]}'
            assert structured_offer.get('priceCurrency')==offer.get('currency'), f'Product Offer currency mismatch: {offer["slug"]}'
            forbidden={'availability','shippingDetails','hasMerchantReturnPolicy','gtin','gtin8','gtin12','gtin13','gtin14','mpn','brand','aggregateRating','review'}
            assert not (forbidden & set(schema)), f'Unsupported Product fields published: {offer["slug"]}'
            assert not ({'availability','shippingDetails','hasMerchantReturnPolicy'} & set(structured_offer)), f'Unsupported Offer fields published: {offer["slug"]}'
        else:
            assert schema.get('@type')=='WebPage', f'Unpriced or earlier record still publishes Product markup: {offer["slug"]}'
    for provider in cfg['providers']:
        page=(ROOT/'site/providers'/provider['id']/'index.html').read_text(encoding='utf-8')
        expect_current=sum(o['provider']==provider['id'] for o in current)
        expect_history=sum(o['provider']==provider['id'] for o in history)
        assert page.count('<article class="card">') == expect_current, f'Current-offer card count wrong on provider page: {provider["id"]}'
        assert page.count('<article class="card history">') == expect_history, f'Historical card count wrong on provider page: {provider["id"]}'
        assert expect_current + expect_history > 0 or provider['id'] in state_only, f'Empty provider page: {provider["id"]}'
        assert f'<title>{build.e(provider["name"])} source-check status and terms | HostDealRadar</title>' in page, f'Provider page title lacks its source-check scope: {provider["id"]}'
    # A state-only page must say why nothing is published and must carry no figure.
    for pid in sorted(state_only):
        page=(ROOT/'site/providers'/pid/'index.html').read_text(encoding='utf-8')
        figures=re.findall(r'\$[0-9]|[0-9](?:\.[0-9]+)?\s?%', page)
        assert not figures, f'A state-only provider page shows price or discount figures: {pid} -> {figures[:5]}'
        status = statuses[pid]
        if status.get('status') == 'available_no_price_rule':
            assert 'No deterministic price rule' in page, f'A state-only provider page does not say why no price is published: {pid}'
        else:
            assert 'Latest source check did not complete.' in page, f'A failed state-only source claims a completed check: {pid}'
            assert build.e(status['reason']) in page, f'A failed state-only source omits its recorded reason: {pid}'
    home=(ROOT/'site/index.html').read_text(encoding='utf-8')
    assert f'{len(ids)} providers in our source list' in home, 'Homepage provider count mismatch'
    assert f'{len(current)} listings captured' in home, 'Homepage current-listing count mismatch'
    guide=(ROOT/'site/guides/godaddy-renewal-coupon/index.html').read_text(encoding='utf-8')
    assert 'The official answer' in guide and guide.count('class="card community-report"') == 3, 'GoDaddy guide is missing its official answer or three linked user reports'
    assert guide.count('Auto-renews Jul. 2027 at $') == 3 and 'automatically renews annually' in guide, 'GoDaddy guide omits the three displayed membership renewals or their annual recurrence'
    assert '/guides/godaddy-renewal-coupon/' in home, 'Homepage does not link the GoDaddy renewal guide'
    godaddy=(ROOT/'site/providers/godaddy/index.html').read_text(encoding='utf-8')
    assert '/guides/godaddy-renewal-coupon/' in godaddy, 'GoDaddy provider page does not link its renewal guide'
    namecheap_guide=(ROOT/'site/guides/namecheap-domain-renewal-coupon/index.html').read_text(encoding='utf-8')
    assert 'The official answer' in namecheap_guide and namecheap_guide.count('class="card community-report"') == 3, 'Namecheap guide is missing its official answer or three linked user reports'
    assert 'USD 18.48/year' in namecheap_guide and namecheap_guide.count('https://www.reddit.com/') == 3, 'Namecheap guide omits the official .com renewal figure or a linked user report'
    assert '/guides/namecheap-domain-renewal-coupon/' in home, 'Homepage does not link the Namecheap renewal guide'
    namecheap=(ROOT/'site/providers/namecheap/index.html').read_text(encoding='utf-8')
    assert '/guides/namecheap-domain-renewal-coupon/' in namecheap, 'Namecheap provider page does not link its renewal guide'
    sitemap=(ROOT/'site/sitemap.xml').read_text(encoding='utf-8')
    assert 'https://hostdealradar.com/guides/namecheap-domain-renewal-coupon/' in sitemap, 'Sitemap omits the Namecheap renewal guide'
    # No earlier record may be presented as a current offer or publish current price data.
    home_schema=[s for s in schemas(home) if s.get('@type')=='ItemList'][0]
    home_schema_json=json.dumps(home_schema,separators=(',',':'))
    assert '"@type":"Product"' not in home_schema_json and '"@type":"Offer"' not in home_schema_json, 'Homepage list publishes Product or Offer markup'
    listed={item['item']['url'] for item in home_schema['itemListElement']}
    allowed={cfg['site']['domain'].rstrip('/')+f'/deals/{o["slug"]}/' for o in current}
    assert listed <= allowed, 'Homepage structured data includes a record that is not current'
    for o in history:
        text=(ROOT/'site/deals'/o['slug']/'index.html').read_text(encoding='utf-8')
        assert not [s for s in schemas(text) if s.get('@type')=='Product'], f'History record published Product markup: {o["slug"]}'
        assert [s for s in schemas(text) if s.get('@type')=='WebPage'], f'History record lacks WebPage markup: {o["slug"]}'
        assert 'Not reconfirmed' in text or 'Needs recheck' in text or 'Expired on' in text, f'History record is not labelled: {o["slug"]}'
    for o in current:
        text=(ROOT/'site/deals'/o['slug']/'index.html').read_text(encoding='utf-8')
        products=[s for s in schemas(text) if s.get('@type')=='Product']
        if o.get('price') is not None:
            assert products and 'offers' in products[0], f'Priced current record is missing Product Offer data: {o["slug"]}'
        else:
            assert not products, f'Unpriced current record published Product markup: {o["slug"]}'
            assert [s for s in schemas(text) if s.get('@type')=='WebPage'], f'Unpriced current record lacks WebPage markup: {o["slug"]}'
    compare=(ROOT/'site/compare/index.html').read_text(encoding='utf-8')
    if history:
        # Comparison rows are labelled by provider and plan title, not by slug, and
        # titles repeat across providers ("Starter" is both an UltaHost plan and a
        # substring of an IONOS one), so match the whole provider+plan cell.
        marker='Earlier records, not current offers'
        assert marker in compare, 'Comparison page does not separate earlier records'
        current_table, history_table = compare.split(marker,1)
        names={p['id']:p['name'] for p in cfg['providers']}
        for o in history:
            cell=f'<strong>{build.e(names[o["provider"]])}</strong><span>{build.e(o["title"])}</span>'
            assert cell in history_table, f'History record missing from the earlier-records table: {o["slug"]}'
            assert cell not in current_table, f'History record is still listed as a current offer: {o["slug"]}'
    assert (ROOT/'site/robots.txt').exists() and (ROOT/'site/sitemap.xml').exists()
    robots=(ROOT/'site/robots.txt').read_text(encoding='utf-8')
    assert 'Sitemap: '+cfg['site']['domain'].rstrip('/')+'/sitemap.xml' in robots, 'robots.txt must advertise the canonical sitemap'
    entries=ET.parse(ROOT/'site/sitemap.xml').getroot().findall('sm:url',NS)
    assert entries, 'Sitemap has no URLs'
    for entry in entries:
        loc=entry.findtext('sm:loc',default='',namespaces=NS)
        lastmod=entry.findtext('sm:lastmod',default='',namespaces=NS)
        assert loc.startswith(cfg['site']['domain'].rstrip('/')+'/'), f'Non-canonical sitemap URL: {loc}'
        assert re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', lastmod), f'lastmod must be an ISO-8601 UTC stamp: {loc} -> {lastmod!r}'
        route=loc.removeprefix(cfg['site']['domain'].rstrip('/')).strip('/')
        output=ROOT/'site'/route/'index.html' if route else ROOT/'site/index.html'
        assert output.exists(), f'Sitemap URL has no generated page: {loc}'
    # lastmod must track material page changes only: rebuilding identical inputs
    # must not move it, which is what a capture-timestamp-driven lastmod would do.
    before=sitemap_lastmods()
    build.build()
    after=sitemap_lastmods()
    assert before==after, 'Sitemap lastmod moved on a rebuild with no material page change'
    # The isolated rebuild must not touch the real payload: build() persists
    # lastmod state into data/offers.json, so a trial build would otherwise
    # write trial-page hashes into the committed record file.
    published=(ROOT/'data/offers.json').read_text(encoding='utf-8')
    altered=Path(tempfile.mkdtemp())/'site.ilang'; altered.write_text((ROOT/'.ilang/site.ilang').read_text(encoding='utf-8').replace('brand:HostDealRadar','brand:HostDealRadar Proof',1),encoding='utf-8')
    trial=Path(tempfile.mkdtemp())/'site'
    probe=Path(tempfile.mkdtemp())/'offers.json'; probe.write_text(json.dumps(payload,indent=2)+'\n',encoding='utf-8')
    saved_data=build.DATA; build.DATA=probe
    try:
        build.build(altered,trial)
        assert 'HostDealRadar Proof' in (trial/'index.html').read_text(encoding='utf-8'), 'site.ilang change did not affect build'
        trial_before=sitemap_lastmods(trial/'sitemap.xml')
        build.build(altered,trial)
        assert sitemap_lastmods(trial/'sitemap.xml')==trial_before, 'Isolated rebuild moved lastmod without a change'
    finally:
        build.DATA=saved_data
    assert (ROOT/'data/offers.json').read_text(encoding='utf-8')==published, 'The isolated rebuild wrote into the published record file'
    shutil.rmtree(altered.parent,ignore_errors=True); shutil.rmtree(trial.parent,ignore_errors=True); shutil.rmtree(probe.parent,ignore_errors=True)
    check_earlier_record_path(payload, cfg)
    print(f'Validated {len(files)} HTML pages, {len(current)} current and {len(history)} earlier records, the earlier-record path, and configuration-driven rebuild.')
if __name__=='__main__': check()
