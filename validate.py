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

def compact(value):
    return re.sub(r'\s+', ' ', value or '').strip()

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
    assert f'href="/providers/{victim["provider"]}/#record-{victim["slug"]}"' not in home, 'A retained record still appears in the current offers list'
    item_list=next(entry for schema in schemas(home) for entry in schema.get('@graph',[schema]) if entry.get('@type')=='ItemList')
    assert victim['slug'] not in json.dumps(item_list), 'A retained record still appears in homepage price data'
    assert not (out/'deals'/victim['slug']/'index.html').exists(), 'A retained record still receives a deal page'
    provider_page=(out/'providers'/victim['provider']/'index.html').read_text(encoding='utf-8')
    assert f'#record-{victim["slug"]}' not in provider_page and f'id="record-{victim["slug"]}"' not in provider_page, 'A retained record is still repeated on its provider page'
    compare=(out/'compare/index.html').read_text(encoding='utf-8')
    marker='Unverified or earlier records, not current offers'
    assert marker in compare, 'Comparison page does not separate earlier records'
    current_table, history_table = compare.split(marker,1)
    names={p['id']:p['name'] for p in cfg['providers']}
    cell=f'<strong>{build.e(names[victim["provider"]])}</strong><span>{build.e(victim["title"])}</span>'
    assert cell in history_table, 'The retained record is missing from the earlier-records table'
    assert cell not in current_table, 'The retained record still appears in the current comparison table'
    victim_row=history_table.split(cell,1)[1].split('</tr>',1)[0]
    assert 'EUR' not in victim_row and '/year' not in victim_row, 'Comparison leaked unsupported normalized currency or billing period'
    assert build.e(build.captured_field_evidence(victim,'price')) in victim_row, 'Comparison did not preserve exact official price wording'
    shutil.rmtree(tmp,ignore_errors=True)

def check():
    cfg=load_config(); settings=cfg['settings']; payload=json.loads((ROOT/'data/offers.json').read_text(encoding='utf-8'))
    rules={(rule['provider'],rule.get('title')):rule for rule in cfg['extractors'] if rule.get('title')}
    statuses=payload.get('source_status',{})
    assert {p['id'] for p in cfg['providers']} <= {r['provider'] for r in cfg['extractors']}, 'Every configured provider needs an extraction or availability rule'
    assert {p['id'] for p in cfg['providers']} == set(statuses), 'Every configured provider needs a recorded source status'
    assert all(o.get('price') != 0 for o in payload.get('offers', [])), 'A zero price must be represented as source text, not a monthly price'
    ids={p['id'] for p in cfg['providers']}
    assert set(cfg['page_focus']) <= ids, 'A page-focus entry names a provider outside the configured source list'
    # The invariant is evidence, not output. Every configured provider must have
    # been opened once and must carry a specific reason. "Reachable official page,
    # but no deterministic rule can be written" is a legitimate state-only
    # landing, not a placeholder; a name that never opened a real page is.
    assert {o['provider'] for o in payload['offers']} <= ids, 'A published record references a provider outside the configured list'
    for provider_id, status in statuses.items():
        assert status.get('status') in ('evidenced','unreadable','challenge'), f'{provider_id} has no evidence-based source status'
        assert status.get('reason'), f'{provider_id} has no recorded reason'
        assert status.get('request_url') and status.get('checked_at'), f'{provider_id} lacks probe URL or time'
        assert 'http_status' in status and 'visible_excerpt' in status, f'{provider_id} lacks response evidence fields'
        if status['status']=='evidenced':
            assert status['http_status']==200 and status['visible_excerpt'], f'{provider_id} claims evidence without HTTP 200 and visible response text'
            assert status.get('capture_status') in ('matched','unmatched','no_price_rule'), f'{provider_id} has no extraction result'
        else:
            assert status.get('capture_status')=='not_attempted' and not status.get('captured_slugs'), f'{provider_id} published a new capture from an unreadable source'
    for rule in cfg['extractors']:
        if rule.get('mode') != 'availability_only':
            continue
        assert rule.get('blocker') in BLOCKERS, f'{rule["provider"]} state-only rule needs a blocker from {sorted(BLOCKERS)}'
        assert rule.get('blocker_evidence'), f'{rule["provider"]} state-only rule needs blocker_evidence (the URL and what was seen)'
    state_only={rule['provider'] for rule in cfg['extractors'] if rule.get('mode')=='availability_only'}
    state_only={pid for pid in state_only if not any(r.get('mode')!='availability_only' for r in cfg['extractors'] if r.get('provider')==pid)}
    guide_provider_ids={'godaddy','namecheap','cloudways'}
    unpublished_source_only={pid for pid in state_only if pid not in cfg['browser_observations'] and pid not in guide_provider_ids}
    for pid in sorted(state_only):
        assert pid not in {o['provider'] for o in payload['offers']}, f'A state-only provider published an offer: {pid}'
    for offer in payload['offers']:
        assert offer.get('source_url') and offer.get('evidence'), 'Missing source attribution'
        assert any(offer.get(k) for k in ('price','price_text','discount_percent','coupon_code')), 'Empty offer'
        if offer.get('price') is not None:
            assert offer['price'] > 0 and offer.get('currency') and offer.get('billing_period'), 'Incomplete price terms'
        assert datetime.fromisoformat(offer['fetched_at'].replace('Z','+00:00')) <= datetime.now(timezone.utc), 'Capture time is in the future'
        status=statuses.get(offer['provider'], {})
        # A provider-level HTTP response is insufficient for a captured offer:
        # each field that cites source text must keep a source fragment that
        # contains its own quote.
        if status.get('status')=='evidenced' and offer['slug'] in status.get('captured_slugs',[]):
            claims=offer.get('claim_evidence', {})
            for field, quote in (offer.get('field_evidence') or {}).items():
                claim=claims.get(field, {})
                assert compact(quote) and compact(quote) in compact(claim.get('excerpt')), f'{offer["slug"]}:{field} lacks a claim-covering excerpt'
                assert claim.get('location') and claim.get('surface'), f'{offer["slug"]}:{field} lacks evidence location or surface'
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
    undated_promotions=[o for o in payload['offers'] if o.get('kind')=='promotion' and not o.get('valid_until')]
    assert all(states[o['slug']]=='unverified' for o in undated_promotions), 'An undated promotion is current or lacks its unverified state'
    if undated_promotions:
        sample=undated_promotions[0].copy()
        sample['fetched_at']=datetime.now(timezone.utc).isoformat()
        fresh_status={sample['provider']:{'status':'checked','captured_slugs':[sample['slug']]}}
        assert build.record_state(sample,fresh_status,settings)[0]=='unverified', 'A fresh successful fetch makes an undated promotion current'
    current=[o for o in payload['offers'] if states[o['slug']]==build.CURRENT]
    history=[o for o in payload['offers'] if states[o['slug']]!=build.CURRENT]
    build.build()
    files=list((ROOT/'site').rglob('*.html'))
    assert files, 'No HTML was built'
    for f in files:
        text=f.read_text(encoding='utf-8')
        assert not re.search(r'\$[A-Za-z_][A-Za-z0-9_]*',text), f'Unfilled template in {f}'
        assert '<script type="application/ld+json">' in text, f'Missing JSON-LD in {f}'
        schemas(text)
        links=Links(); links.feed(text)
        for link in links.urls:
            target=ROOT/'site'/unquote(urlsplit(link).path).lstrip('/')
            assert target.is_file() or (target/'index.html').is_file(), f'Broken internal link in {f}: {link}'
    assert not (ROOT/'site/deals').exists(), 'Per-record deal pages were generated'
    for offer in payload['offers']:
        page=(ROOT/'site/providers'/offer['provider']/'index.html').read_text(encoding='utf-8')
        detail=f'id="record-{offer["slug"]}"'
        if states[offer['slug']] != build.CURRENT:
            assert page.count(detail)==0, f'Non-current record remains on its provider page: {offer["slug"]}'
            continue
        assert page.count(detail)==1, f'Current record does not have exactly one provider detail: {offer["slug"]}'
        block=page.split(detail,1)[1].split('</section>',1)[0]
        assert build.e(offer['source_url']) in block and build.e(build.date_text(offer['fetched_at'])) in block, f'Record detail lacks its own source or check date: {offer["slug"]}'
        assert states[offer['slug']] in block, f'Record detail omits its verification state: {offer["slug"]}'
        assert 'Unknown' not in block, f'Provider record still publishes an Unknown field: {offer["slug"]}'
        price_quote=build.e((offer.get('field_evidence') or {}).get('price'))
        assert price_quote and block.count(price_quote)==1, f'Provider record does not show its official price wording exactly once: {offer["slug"]}'
        assert 'Currency wording' not in block and 'Billing wording' not in block, f'Provider record repeats normalized price wording: {offer["slug"]}'
        rule=rules.get((offer['provider'], offer['title']), {})
        optional_evidence=[
            build.captured_field_evidence(offer,'commitment_months') if offer.get('commitment_months') else '',
            build.captured_field_evidence(offer,'renewal_price') if build.renewal_supported(offer,rule) else '',
            build.captured_field_evidence(offer,'coupon_code') if offer.get('coupon_code') else '',
            build.captured_field_evidence(offer,'valid_until') if offer.get('valid_until') else '',
        ]
        missing=sum(not value for value in optional_evidence)
        assert block.count('<dt>Not stated by the source</dt>') == (1 if missing else 0), f'Unavailable official fields are not collapsed to one row: {offer["slug"]}'
        assert 'no affiliate relationship is active' not in block.lower(), f'Per-record system disclosure remains: {offer["slug"]}'
    for provider in cfg['providers']:
        page_path=ROOT/'site/providers'/provider['id']/'index.html'
        if provider['id'] in unpublished_source_only:
            assert not page_path.exists(), f'Unverified source-only provider page is still public: {provider["id"]}'
            continue
        page=page_path.read_text(encoding='utf-8')
        expect_current=sum(o['provider']==provider['id'] for o in current)
        assert page.count('<article class="card">') == 0, f'Duplicate summary cards remain on provider page: {provider["id"]}'
        assert page.count('<article class="card history">') == 0, f'Historical summary cards remain on provider page: {provider["id"]}'
        assert page.count('<section class="record-detail"') == expect_current, f'Current provider detail count wrong: {provider["id"]}'
        focus=cfg['page_focus'].get(provider['id'])
        provider_records=[o for o in current if o['provider']==provider['id']]
        has_coupon=any(o.get('coupon_code') and build.captured_field_evidence(o,'coupon_code') for o in provider_records)
        if expect_current:
            base=focus or provider['name']
            heading=base if re.search(r'\b(?:pricing|prices?|coupon)\b',base,re.I) else base+(' coupon code and pricing' if has_coupon else ' pricing')
            first_record=page.index('<section class="record-detail"')
            assert first_record < page.index('<h2>About this source check</h2>'), f'Provider process copy appears before the answer: {provider["id"]}'
        else:
            heading=provider['name']+' pricing availability'
        assert f'<title>{build.e(heading)} | HostDealRadar</title>' in page, f'Provider page title is not reader-facing: {provider["id"]}'
        assert f'<h1>{build.e(heading)}</h1>' in page, f'Provider page heading does not match its visible record state: {provider["id"]}'
        assert 'source-check status and terms | HostDealRadar' not in page, f'Internal terminology remains in the provider title: {provider["id"]}'
        assert page.lower().count('no active affiliate relationship') == 1, f'Provider page does not carry exactly one page-level relationship disclosure: {provider["id"]}'
    cloudways=(ROOT/'site/providers/cloudways/index.html').read_text(encoding='utf-8')
    assert '#record-cloudways-summer404' not in cloudways and 'id="record-cloudways-summer404"' not in cloudways, 'Undated Cloudways promotion remains on the provider page'
    assert 'No current offer is published for this source' in cloudways, 'Cloudways page does not disclose that it has no current source record'
    # A state-only page must say why nothing is published and must carry no figure.
    for pid in sorted(state_only):
        if pid in unpublished_source_only:
            continue
        page=(ROOT/'site/providers'/pid/'index.html').read_text(encoding='utf-8')
        automated_section=page.split('<section class="record-detail source-observation"',1)[0]
        figures=re.findall(r'\$[0-9]|[0-9](?:\.[0-9]+)?\s?%', automated_section)
        assert not figures, f'A state-only provider page shows price or discount figures: {pid} -> {figures[:5]}'
        status = statuses[pid]
        if status.get('status') == 'evidenced' and status.get('capture_status') == 'no_price_rule':
            assert 'No deterministic price rule' in page, f'A state-only provider page does not say why no price is published: {pid}'
        else:
            assert 'Latest source check did not complete.' in page, f'A failed state-only source claims a completed check: {pid}'
            assert build.e(status['reason']) in page, f'A failed state-only source omits its recorded reason: {pid}'
        observation=cfg['browser_observations'].get(pid)
        if observation:
            assert build.e(observation['quote']) in page and build.e(observation['url']) in page and observation['observed_at'] in page, f'Manual browser observation missing its quote, date, or source: {pid}'
            assert 'Official source record' in page and 'Not publicly disclosed on the observed page' in page, f'Observation is not rendered as a source record: {pid}'
            assert 'does not mean that no promotion exists' in page, f'Observation lacks the public-price limitation: {pid}'
            assert 'does not change the automated source-check status' in page, f'Manual observation is confused with an automated source check: {pid}'
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
    cloudways_guide=(ROOT/'site/guides/cloudways-coupon-code/index.html').read_text(encoding='utf-8')
    countdown=((statuses['cloudways'].get('source_claim_evidence') or {}).get('countdown') or {})
    countdown_visible=countdown.get('visible_excerpt') or countdown.get('quote')
    assert statuses['cloudways'].get('http_status')==200 and countdown_visible and compact(countdown_visible) in cloudways_guide, 'Cloudways guide omits the stored countdown evidence'
    assert 'September 15, 2026' in cloudways_guide and 'not supported by a retained source excerpt' in cloudways_guide and 'time zone is not stated' in cloudways_guide, 'Cloudways guide hides the unsupported-date correction or date ambiguity'
    cloudways_schema=schemas(cloudways_guide)[0]
    assert cloudways_schema.get('@type')=='FAQPage' and len(cloudways_schema.get('mainEntity',[]))==5, 'Cloudways guide must publish its visible FAQPage schema'
    # Preserve every cited archive row and the limits that keep historical
    # evidence from being presented as a current offer or a live-browser test.
    archive=cloudways_guide[cloudways_guide.index('id="archive"'):cloudways_guide.index('<h2>Before you start a paid plan</h2>')]
    for token in ('BFCM18','BFCM40','BFCM2021','BFCM4030','CC-MAIN-2018-51','CC-MAIN-2019-51',
                  'CC-MAIN-2020-50','CC-MAIN-2021-49','CC-MAIN-2022-33','CC-MAIN-2022-49',
                  '2018-12-13T05:43:57Z','2019-12-09T06:58:23Z','2020-11-25T11:26:42Z',
                  '2021-11-29T03:08:26Z','2022-08-10T05:22:59Z','2022-12-03T06:04:16Z',
                  'December 4, 2019','1st of December','Inspectlet','NotificationX','cloudways-BFCM',
                  'does not show that Cloudways had no offer that year',
                  'does not tell us how JavaScript behaved',
                  'do not establish how many coupons remained redeemable'):
        assert token in archive, f'Cloudways archive section dropped its evidence: {token}'
    assert archive.count('<tr id="archive-')==6, 'Cloudways archive table lost a year row'
    assert 'one Reddit result' in archive and 'not used as evidence of absence' in archive, 'Cloudways archive hides the one result it could not open'
    assert 'limited, dated comparison' in archive and 'unique across the web' in archive, 'Cloudways archive overstates its comparison scope'
    assert 'only valid for new customers' in archive and 'applies to users who sign up' in archive, 'Cloudways archive drops the source conflict about eligibility'
    assert 'one coupon per claimant' in archive and 'one use per order' in archive, 'Cloudways archive drops the recorded coupon-use limits'
    assert all(f'id="archive-{suffix}"' in archive for suffix in ('2018','2019','2020','2021','2022-08','2022-12')), 'Cloudways archive rows lost their evidence anchors'
    for action in ('1. Check the current official pages.','2. Check eligibility.','3. Check the whole bill.','4. Keep historical evidence in the past.'):
        assert action in cloudways_guide, f'Cloudways guide drops a pre-purchase action: {action}'
    assert '/guides/cloudways-coupon-code/' in (ROOT/'site/providers/cloudways/index.html').read_text(encoding='utf-8'), 'Cloudways provider page does not link the official promo guide'
    assert 'https://hostdealradar.com/guides/cloudways-coupon-code/' in sitemap, 'Sitemap omits the Cloudways promo guide'
    digitalocean_guide=(ROOT/'site/guides/digitalocean-promo-code/index.html').read_text(encoding='utf-8')
    assert 'No. DigitalOcean says' in digitalocean_guide and 'We did not create an account' in digitalocean_guide, 'DigitalOcean guide omits its answer or evidence limit'
    assert 'https://docs.digitalocean.com/platform/billing/signup-credit/' in digitalocean_guide and 'https://www.digitalocean.com/legal/promotional-credit-discount-terms' in digitalocean_guide, 'DigitalOcean guide omits an official source'
    assert '/guides/digitalocean-promo-code/' in home and '/guides/digitalocean-promo-code/' in (ROOT/'site/providers/digitalocean/index.html').read_text(encoding='utf-8'), 'DigitalOcean guide lacks a home or provider link'
    assert 'https://hostdealradar.com/guides/digitalocean-promo-code/' in sitemap, 'Sitemap omits the DigitalOcean guide'
    hosting_coupons=(ROOT/'site/guides/hosting-coupons/index.html').read_text(encoding='utf-8')
    assert all(source in hosting_coupons for source in ('https://www.hostinger.com/coupons','https://www.namecheap.com/hosting/19th-birthday/','https://docs.digitalocean.com/platform/billing/signup-credit/')), 'Hosting coupon guide omits an official source'
    assert 'September 25' in hosting_coupons and 'not checkout tests or a price ranking' in hosting_coupons, 'Hosting coupon guide loses its time or evidence boundary'
    assert '/guides/hosting-coupons/' in home and 'https://hostdealradar.com/guides/hosting-coupons/' in sitemap, 'Hosting coupon guide lacks a home link or sitemap entry'
    # No unverified or earlier record may be presented as current or publish current price data.
    home_schema=next(entry for schema in schemas(home) for entry in schema.get('@graph',[schema]) if entry.get('@type')=='ItemList')
    home_schema_json=json.dumps(home_schema,separators=(',',':'))
    assert '"@type":"Product"' not in home_schema_json and '"@type":"Offer"' not in home_schema_json, 'Homepage list publishes Product or Offer markup'
    listed={item['item']['url'] for item in home_schema['itemListElement']}
    allowed={cfg['site']['domain'].rstrip('/')+f'/providers/{o["provider"]}/' for o in current}
    assert listed <= allowed, 'Homepage structured data includes a record that is not current'
    compare=(ROOT/'site/compare/index.html').read_text(encoding='utf-8')
    if history:
        # Comparison rows are labelled by provider and plan title, not by slug, and
        # titles repeat across providers ("Starter" is both an UltaHost plan and a
        # substring of an IONOS one), so match the whole provider+plan cell.
        marker='Unverified or earlier records, not current offers'
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
    assert 'Content-Signal: ai-train=no, search=yes, ai-input=no' in robots, 'robots.txt is missing the configured Content-Signal'
    assert 'Agentmap: '+cfg['site']['domain'].rstrip('/')+'/.well-known/ai-catalog.json' in robots, 'robots.txt is missing the Agentmap manifest pointer'
    assert '<link rel="ai-catalog" href="/.well-known/ai-catalog.json">' in home, 'Homepage is missing the ARD link relation'
    home_schemas=schemas(home)
    graph=next((schema for schema in home_schemas if '@graph' in schema), None)
    assert graph and {entry.get('@type') for entry in graph['@graph']} >= {'WebSite','Organization','ItemList'}, 'Homepage lacks WebSite, Organization, or ItemList identity markup'
    static_paths=['agent-data.json','ai/index.md','ai/index.ilang','ai/skills/site-lookup/SKILL.md','auth.md','openapi.json','.well-known/api-catalog.json','.well-known/agent-skills/index.json','.well-known/ai-catalog.json','.well-known/oauth-authorization-server','.well-known/oauth-protected-resource','.well-known/jwks.json','.well-known/mcp/server-card.json','assets/agent-tools.js','_worker.js']
    for path in static_paths:
        assert (ROOT/'site'/path).is_file(), f'Agent-ready build output missing {path}'
    skill=(ROOT/'site/ai/skills/site-lookup/SKILL.md').read_bytes()
    skill_index=json.loads((ROOT/'site/.well-known/agent-skills/index.json').read_text(encoding='utf-8'))
    assert skill_index['skills'][0]['digest']=='sha256:'+__import__('hashlib').sha256(skill).hexdigest(), 'Agent skill digest does not match served bytes'
    catalog=json.loads((ROOT/'site/.well-known/api-catalog.json').read_text(encoding='utf-8'))
    assert catalog['linkset'][0]['anchor']==cfg['site']['domain'].rstrip('/')+'/api/agent/lookup', 'API catalog points to the wrong resource'
    assert catalog['linkset'][0]['service-doc'][0]['href']==cfg['site']['domain'].rstrip('/')+'/ai/', 'API catalog lacks the Markdown service documentation'
    records=json.loads((ROOT/'site/agent-data.json').read_text(encoding='utf-8'))
    assert len(records)==len(payload['offers']) and all({'id','provider','record_state','source_url','captured_at','record_url'} <= set(record) for record in records), 'Read-only API projection is incomplete'
    protected=json.loads((ROOT/'site/.well-known/oauth-protected-resource').read_text(encoding='utf-8'))
    assert protected['resource']==cfg['site']['domain'].rstrip('/'), 'OAuth protected resource must use the canonical origin, not an endpoint path'
    mcp_card=json.loads((ROOT/'site/.well-known/mcp/server-card.json').read_text(encoding='utf-8'))
    assert mcp_card['serverInfo']['name']=='hostdealradar-public-lookup' and mcp_card['transport']['endpoint']==cfg['site']['domain'].rstrip('/')+'/mcp', 'MCP server card must advertise the live lookup endpoint'
    assert protected['available'] is False and protected['status']=='under_construction', 'OAuth placeholder must be explicitly unavailable'
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
    sitemap_urls={entry.findtext('sm:loc',default='',namespaces=NS) for entry in entries}
    for pid in unpublished_source_only:
        assert cfg['site']['domain'].rstrip('/')+f'/providers/{pid}/' not in sitemap_urls, f'Unverified source-only provider is still in sitemap: {pid}'
    current_provider_ids={o['provider'] for o in current}
    public_provider_ids={p['id'] for p in cfg['providers']} - unpublished_source_only
    for pid in public_provider_ids:
        url=cfg['site']['domain'].rstrip('/')+f'/providers/{pid}/'
        assert (url in sitemap_urls) == (pid in current_provider_ids), f'Provider sitemap eligibility disagrees with current records: {pid}'
    worker=(ROOT/'site/_worker.js').read_text(encoding='utf-8')
    for pid in unpublished_source_only:
        assert f'/providers/{pid}/' in worker and 'status: 410' in worker, f'Withdrawn provider path lacks an explicit 410 response: {pid}'
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
