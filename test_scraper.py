"""Regression checks for missing prices, source failures and robots restrictions."""
import json
import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
import build
import scraper
from config import load_config

class SourceSafety(unittest.TestCase):
    def setUp(self):
        self.provider = dict(id='sample', name='Sample', source_url='https://example.com/pricing', affiliate_url='')
        self.rule = dict(provider='sample', mode='anchored_text', anchor='Basic plan', window_chars=100, title='Basic', currency='USD', billing_period='month', field_patterns={'price':r'USD (?P<value>[0-9.]+) / month'}, structure='Plan card')

    def test_explicit_annual_and_monthly_wording_establishes_price_unit(self):
        cases = [('year', 'Billed annually. Pay USD 120 today.', r'Billed annually\. Pay USD (?P<value>[0-9.]+) today\.'),
                 ('month', 'Monthly starting at EUR 5', r'Monthly starting at EUR (?P<value>[0-9.]+)')]
        for period, quote, pattern in cases:
            with self.subTest(period=period):
                rule = dict(self.rule, billing_period=period, currency='EUR' if period == 'month' else 'USD', field_patterns={'price':pattern})
                offer = scraper.offer_from_rule(self.provider, rule, 'Basic plan ' + quote)
                self.assertIsNotNone(offer)
                self.assertEqual(offer['field_evidence']['price'], quote)
        self.assertIsNone(scraper.offer_from_rule(self.provider, self.rule,
            'Basic plan USD 9 / year. Another plan USD 7 / month', ['Another plan']))

    def test_targeted_refresh_keeps_other_providers_and_original_dates(self):
        other = dict(self.provider, id='other', name='Other')
        cfg = {'providers':[self.provider, other], 'extractors':[self.rule], 'settings':{}}
        old = {'provider':'other', 'slug':'other-basic', 'title':'Other Basic', 'price':5,
               'fetched_at':'2026-01-01T00:00:00Z'}
        prior_status = {'status':'unreadable', 'checked_at':'2026-01-02T00:00:00Z'}
        manual = {'checked_on':'2026-01-01', 'records':[{'plan':'Dated manual example'}]}
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'offers.json'
            data.write_text(json.dumps({'offers':[old], 'source_status':{'other':prior_status},
                                        'browser_term_observations':manual}), encoding='utf-8')
            with patch.object(scraper,'DATA',data), patch.object(scraper,'load_config',return_value=cfg), patch.object(scraper,'fetch_source',return_value=(200,'Basic plan USD 9 / month',self.provider['source_url'])) as fetch:
                result = scraper.run(provider_ids=['sample'])
        fetch.assert_called_once()
        self.assertIn(old, result['offers'])
        self.assertEqual(result['source_status']['other'], prior_status)
        self.assertEqual(result['browser_term_observations'], manual)
        self.assertEqual(result['refresh_scope'], ['sample'])

    def test_unknown_target_stops_before_network_or_data_write(self):
        cfg = {'providers':[self.provider], 'extractors':[self.rule], 'settings':{}}
        with patch.object(scraper,'load_config',return_value=cfg), patch.object(scraper,'fetch_source') as fetch:
            with self.assertRaisesRegex(ValueError, 'Unknown provider'):
                scraper.run(provider_ids=['typo'])
        fetch.assert_not_called()

    def test_refresh_report_distinguishes_price_text_and_retained_records(self):
        payload = {'offers':[{'slug':'price','price':9},{'slug':'text','price_text':'First month free'},{'slug':'old','price':4}],
                   'source_status':{'sample':{'status':'evidenced','capture_status':'matched',
                       'captured_slugs':['price','text'],'retained_count':1,'unmatched_rules':['Old'],
                       'rule_failures':{'Old':'The configured plan anchor was not found.'}}}}
        report, warnings = scraper.refresh_report(payload)
        self.assertIn('**1 numeric price records refreshed**', report)
        self.assertIn('1 text-only records captured, 1 earlier records retained', report)
        self.assertEqual(len(warnings), 1)
        self.assertIn('anchor was not found', warnings[0][1])

    def test_unmatched_price_reports_missing_unit_without_weakening_guard(self):
        rule = dict(self.rule, field_patterns={'price':r'USD (?P<value>[0-9.]+)'})
        diagnostics = []
        self.assertIsNone(scraper.offer_from_rule(self.provider, rule,
            'Basic plan USD 9; another item costs USD 1 / month', diagnostics=diagnostics))
        self.assertIn('billing unit', diagnostics[0])

    def test_table_price_uses_the_requested_row_and_monthly_column(self):
        raw = ('<table><tr><th>Plan</th><th>Per hour</th><th>Per month</th></tr>'
               '<tr><td>Basic</td><td>$0.005</td><td>$4.00</td></tr>'
               '<tr><td>Pro</td><td>$0.01</td><td>$9.00</td></tr></table>')
        rule = dict(self.rule, mode='table_cell', row_match='^Basic$', column_match='Per month',
                    field_patterns={'price':r'\$(?P<value>[0-9.]+)'})
        offer = scraper.offer_from_rule(self.provider, rule, raw)
        self.assertEqual(offer['price'], 4)
        self.assertEqual(offer['field_evidence']['price'], '$4.00')
        self.assertEqual(offer['field_evidence']['billing_period'], 'Per month')
        self.assertEqual(offer['table_evidence']['row_quote'], 'Basic')
        conflicting = dict(rule, field_patterns={'price':r'\$(?P<value>[0-9.]+)/year'})
        self.assertIsNone(scraper.offer_from_rule(self.provider, conflicting, raw.replace('$4.00', '$4.00/year')))

    def test_duplicate_table_titles_require_matching_plan_attributes(self):
        raw = ('<table><tr><th>Business plans</th><th data-boundary="bandwidth">WP 20</th>'
               '<th data-boundary="visits">WP 20</th></tr><tr><td>Price</td>'
               '<td data-boundary="bandwidth">$340 USD month $3,400 billed annually</td>'
               '<td data-boundary="visits">$400 USD month $4,000 billed annually</td></tr></table>')
        rule = dict(self.rule, mode='table_cell', row_match='^Price$', column_match='WP 20',
                    column_attributes={'data-boundary':'bandwidth'}, billing_period='year',
                    field_patterns={'price':r'\$(?P<value>[0-9,]+) billed annually'})
        offer = scraper.offer_from_rule(self.provider, rule, raw)
        self.assertEqual(offer['price'], 3400)
        self.assertIsNone(scraper.offer_from_rule(self.provider, dict(rule,column_attributes={}),raw))
        self.assertIsNone(scraper.offer_from_rule(self.provider, rule, raw.replace('<td data-boundary="bandwidth">','<td data-boundary="visits">')))

    def test_price_metadata_binds_billing_period_to_the_same_element(self):
        rule = dict(self.rule, mode='anchored_html', anchor='Basic',
                    field_patterns={'price':r'<div data-on-value="monthly">US\$(?P<value>[0-9.]+)</div>'})
        offer = scraper.offer_from_rule(self.provider, rule, 'Basic<div data-on-value="monthly">US$9</div>')
        self.assertEqual(offer['price'],9)
        self.assertEqual(offer['field_evidence']['billing_period'],'data-on-value="monthly"')
        self.assertIsNone(scraper.offer_from_rule(self.provider,rule,'Basic<div data-on-value="yearly">US$9</div>'))

    def test_advertised_plan_price_does_not_claim_promotion_validity(self):
        offer = {'provider':'sample','slug':'sample-basic','kind':'plan_price',
                 'fetched_at':datetime.now(timezone.utc).isoformat(),'price':9,'discount_percent':50}
        status = {'sample':{'status':'evidenced','capture_status':'matched','http_status':200,
                            'visible_excerpt':'Basic $9 per month','captured_slugs':['sample-basic']}}
        self.assertEqual(build.record_state(offer,status,{'fresh_hours':18})[0],'current')
        self.assertEqual(build.record_state(dict(offer,kind='promotion'),status,{'fresh_hours':18})[0],'unverified')

    def test_supplementary_source_denial_retains_dates_and_locks_provider(self):
        resource = 'https://example.com/pricing-data.js'
        cfg = {'providers':[self.provider], 'extractors':[self.rule], 'settings':{},
               'source_resources':{'sample':[resource]}}
        old = {'provider':'sample','slug':'sample-basic','title':'Basic','price':5,
               'fetched_at':'2026-01-01T00:00:00Z'}
        denial = scraper.SourceProbeError('unreadable','Official source returned HTTP 403.',resource,403,'Access denied')
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'offers.json'
            data.write_text(json.dumps({'offers':[old]}),encoding='utf-8')
            primary = 'Basic plan USD 9 / month<script src="'+resource+'"></script>'
            with patch.object(scraper,'DATA',data), patch.object(scraper,'load_config',return_value=cfg), patch.object(scraper,'fetch_source',side_effect=[(200,primary,self.provider['source_url']),denial]):
                first = scraper.run()
            with patch.object(scraper,'DATA',data), patch.object(scraper,'load_config',return_value=cfg), patch.object(scraper,'fetch_source') as fetch:
                second = scraper.run()
        self.assertEqual(first['offers'],[old])
        self.assertEqual(first['source_403_locks']['sample']['request_url'],resource)
        self.assertEqual(second['offers'],[old])
        fetch.assert_not_called()

    def test_parallel_prices_require_same_index_resources_and_explicit_unit(self):
        raw = 'preset: { price: [14, 28], ram: ["1GB", "2GB"] }\nformatter("USD per month");'
        rule = dict(self.rule, mode='parallel_arrays', anchor='preset:', window_chars=100,
                    array_index=1, array_patterns={'price':r'price: (?P<value>\[[^\]]+\])',
                                                   'ram':r'ram: (?P<value>\[[^\]]+\])'},
                    array_match={'ram':'2GB'}, billing_unit_pattern=r'formatter\("(?P<value>USD per month)"\);')
        rule.pop('field_patterns')
        offer = scraper.offer_from_rule(self.provider,rule,raw)
        self.assertEqual(offer['price'],28)
        self.assertEqual(offer['field_evidence']['price'],'28')
        self.assertEqual(offer['field_evidence']['price_currency'],'USD per month')
        self.assertIsNone(scraper.offer_from_rule(self.provider,rule,raw.replace('"2GB"','"4GB"')))
        self.assertIsNone(scraper.offer_from_rule(self.provider,rule,raw.replace('[14, 28]','[14]')))
        self.assertIsNone(scraper.offer_from_rule(self.provider,rule,raw.replace('USD per month','EUR per month')))

    def test_sourced_total_must_agree_with_published_monthly_equivalent(self):
        offer = {'first_term_total':119.52, 'price':2.49, 'commitment_months':48,
                 'currency':'USD','billing_period':'month','renewal_price':10.99,
                 'source_url':self.provider['source_url'],'fetched_at':'2026-10-24T00:00:00Z',
                 'field_evidence':{'first_term_total':'Get 48 months for $119.52',
                     'price':'$2.49/mo','commitment_months':'Get 48 months',
                     'renewal_price':'Renews at $10.99/mo'}}
        self.assertTrue(build.complete_three_terms(offer))
        self.assertFalse(build.complete_three_terms(dict(offer,first_term_total=143.52)))

    def test_no_record_when_price_is_missing(self):
        self.assertIsNone(scraper.offer_from_rule(self.provider,self.rule,'<p>Basic plan: Contact sales</p>'))

    def test_knownhost_mixed_static_term_prices_cannot_reappear(self):
        cfg = load_config()
        provider = next(p for p in cfg['providers'] if p['id'] == 'knownhost')
        rules = [r for r in cfg['extractors'] if r['provider'] == 'knownhost']
        raw = ('<button class="pkg-active">3 Year</button>'
               '<meta itemprop="priceCurrency" content="USD">'
               '<strong class="price-basic">6.71</strong>/mo '
               'Renews at <span class="price-basic-original">8.95</span>/mo')
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'offers.json'
            archive = [{'record': {'slug': 'withdrawn-basic', 'price': 6.71}}]
            data.write_text(json.dumps({'offers': [], 'withdrawn_automated_records': archive}), encoding='utf-8')
            scoped = {**cfg, 'providers': [provider], 'extractors': rules}
            with patch.object(scraper, 'DATA', data), patch.object(scraper, 'load_config', return_value=scoped), patch.object(scraper, 'fetch_source', return_value=(200, raw, provider['source_url'])):
                result = scraper.run()
        self.assertEqual(result['offers'], [])
        self.assertEqual(result['withdrawn_automated_records'], archive)
        self.assertEqual(result['source_status']['knownhost']['capture_status'], 'no_price_rule')
        self.assertIn('billing term', build.BLOCKER_TEXT[result['source_status']['knownhost']['blocker']])

    def test_voog_and_portfoliobox_price_quotes_include_official_currency(self):
        cfg = load_config()
        examples = {
            ('voog', 'Starter'): ('Starter €1.25/mo For one-pager websites.', '€1.25/mo'),
            ('voog', 'Standard'): ('Standard €11/mo Good for simple websites.', '€11/mo'),
            ('voog', 'Plus'): ('Plus €17/mo Save €60 annually.', '€17/mo'),
            ('voog', 'Premium'): ('Premium €39/mo Save €132 annually.', '€39/mo'),
            ('portfoliobox', 'Professional'): ('Professional $15.9 /month Everything you need for your portfolio.', '$15.9 /month'),
            ('portfoliobox', 'Personal'): ('Personal $8.9 /month A great personal portfolio.', '$8.9 /month'),
        }
        providers = {provider['id']: provider for provider in cfg['providers']}
        for (provider_id, title), (source_text, quote) in examples.items():
            with self.subTest(provider=provider_id, title=title):
                rule = next(rule for rule in cfg['extractors'] if rule['provider'] == provider_id and rule['title'] == title)
                offer = scraper.offer_from_rule(providers[provider_id], rule, source_text)
                self.assertIsNotNone(offer)
                self.assertEqual(offer['field_evidence']['price'], quote)
                self.assertIn(quote, offer['claim_evidence']['price']['visible_excerpt'])

    def test_end_boundary_does_not_take_next_plan_price(self):
        rule=dict(self.rule, end_anchor='Pro plan')
        self.assertIsNone(scraper.offer_from_rule(self.provider,rule,'Basic plan Contact us Pro plan USD 19 / month'))

    def test_sibling_plan_boundary_does_not_take_next_plan_price(self):
        text='Basic plan Contact sales Pro plan USD 19 / month'
        self.assertIsNone(scraper.offer_from_rule(self.provider,self.rule,text,['Pro plan']))

    def test_run_passes_sibling_plan_boundaries(self):
        pro_rule=dict(self.rule, title='Pro', anchor='Pro plan')
        cfg={'providers':[self.provider], 'extractors':[self.rule, pro_rule], 'settings':{}}
        with tempfile.TemporaryDirectory() as folder:
            data=Path(folder)/'offers.json'
            data.write_text(json.dumps({'offers':[]}),encoding='utf-8')
            with patch.object(scraper,'DATA',data),patch.object(scraper,'load_config',return_value=cfg),patch.object(scraper,'fetch_source',return_value=(200,'Basic plan Contact sales Pro plan USD 19 / month',self.provider['source_url'])):
                result=scraper.run()
        self.assertEqual([offer['slug'] for offer in result['offers']], ['sample-pro'])
        self.assertEqual(result['source_status']['sample']['status'], 'evidenced')
        self.assertEqual(result['source_status']['sample']['http_status'], 200)
        self.assertIn('Basic plan Contact sales', result['source_status']['sample']['visible_excerpt'])

    def test_currency_guard_and_annual_period(self):
        rule=dict(self.rule,raw_checks=['USD'],billing_period='year',field_patterns={'price':r'USD (?P<value>[0-9.]+) / year'})
        value=scraper.offer_from_rule(self.provider,rule,'Basic plan USD 15 / year')
        self.assertEqual((value['price'],value['billing_period'],value['kind']),(15,'year','regular_price'))
        self.assertIsNone(scraper.offer_from_rule(self.provider,rule,'Basic plan EUR 15 / year'))

    def test_claim_excerpt_covers_each_extracted_field(self):
        rule=dict(self.rule,field_patterns={
            'price':r'USD (?P<value>[0-9.]+) / month',
            'renewal_price':r'Renews USD (?P<value>[0-9.]+) / month',
        })
        offer=scraper.offer_from_rule(self.provider,rule,
            'Basic plan. Intro USD 9 / month. Extra terms. Renews USD 19 / month.')
        for field, quote in offer['field_evidence'].items():
            self.assertIn(quote, offer['claim_evidence'][field]['excerpt'])
            self.assertTrue(offer['claim_evidence'][field]['location'])

    def test_configured_source_evidence_keeps_counter_statement(self):
        raw=('prefix <div id="counter">Sept 16, 2026 14:00:00 '
             '<strong>0 Days 0 Hours 0 Mins 0 Sec</strong></div> suffix')
        rule={'source_evidence_patterns':{'countdown':r'(?s)Sept 16, 2026 14:00:00.*?0 Sec'}}
        evidence=scraper.configured_source_evidence([rule],raw)['countdown']
        self.assertIn('Sept 16, 2026 14:00:00', evidence['quote'])
        self.assertIn('0 Days 0 Hours 0 Mins 0 Sec', evidence['excerpt'])

    def test_raw_field_match_is_not_relabelled_as_visible_copy(self):
        rule=dict(self.rule,mode='presence',pattern='coupon=ABC',
                  field_patterns={'coupon_code':r'coupon=(?P<value>[A-Z]+)'})
        offer=scraper.offer_from_rule(self.provider,rule,
            '<a href="https://example.com/signup?coupon=ABC">Claim offer</a>')
        self.assertEqual(offer['claim_evidence']['coupon_code']['surface'], 'source response markup')

    def test_explicit_currency_and_period_cannot_be_relabelled(self):
        currency_rule=dict(self.rule,field_patterns={'price':r'\$\s?(?P<value>[0-9.]+)\s*/ month'})
        period_rule=dict(self.rule,field_patterns={'price':r'\$\s?(?P<value>[0-9.]+)\s*/\s*\w+'})
        self.assertIsNone(scraper.offer_from_rule(self.provider,currency_rule,'Basic plan CAD $4 / month'))
        self.assertIsNone(scraper.offer_from_rule(self.provider,period_rule,'Basic plan $4 / site'))

    def test_failed_state_only_source_never_claims_checked(self):
        heading, detail, tile = build.state_only_display({'status':'unreadable','reason':'Official source returned HTTP 429.'}, 'price_rendered_by_js')
        self.assertEqual(heading, 'Latest source check did not complete.')
        self.assertIn('HTTP 429', detail)
        self.assertEqual(tile, 'Source check did not complete')
        heading, _, tile = build.state_only_display({'status':'evidenced','capture_status':'no_price_rule','http_status':200,'visible_excerpt':'Official pricing'}, 'no_public_price')
        self.assertEqual(heading, build.STATE_ONLY_LEAD)
        self.assertEqual(tile, 'Official page checked · no deterministic price rule')

    def test_blank_lines_and_wildcards_do_not_bypass_robots(self):
        robots='User-agent: *\n\nDisallow: /promo/*\nAllow: /promo/public$\n'
        self.assertFalse(scraper.robots_decision(robots,'https://example.com/promo/private')[0])
        self.assertTrue(scraper.robots_decision(robots,'https://example.com/promo/public')[0])
        self.assertFalse(scraper.robots_decision(robots,'https://example.com/promo/public/extra')[0])

    def test_specific_agent_group_takes_precedence(self):
        robots='User-agent: *\nDisallow: /\nUser-agent: HostDealRadar\nAllow: /pricing\n'
        self.assertTrue(scraper.robots_decision(robots,self.provider['source_url'])[0])

    def test_failed_or_unmatched_source_keeps_capture_date(self):
        cfg={'providers':[self.provider], 'extractors':[self.rule], 'settings':{}}
        old={'provider':'sample','slug':'sample-basic','title':'Basic','source_url':self.provider['source_url'],'fetched_at':'2026-01-01T01:02:03Z','price':9,'currency':'USD','billing_period':'month'}
        with tempfile.TemporaryDirectory() as folder:
            data=Path(folder)/'offers.json'
            for side_effect in (ValueError('robots.txt disallow: /pricing'),TimeoutError('source timeout'),None):
                data.write_text(json.dumps({'offers':[old]}),encoding='utf-8')
                with patch.object(scraper,'DATA',data),patch.object(scraper,'load_config',return_value=cfg),patch.object(scraper,'fetch_source',return_value=(200,'Basic plan Contact sales',self.provider['source_url']),side_effect=side_effect):
                    result=scraper.run()
                self.assertEqual(result['offers'],[old])
                self.assertEqual(result['source_status']['sample'].get('captured_count',0),0)

    def test_refresh_keeps_build_owned_state_keys(self):
        cfg={'providers':[self.provider], 'extractors':[self.rule], 'settings':{}}
        state={'/':{'hash':'abc','lastmod':'2026-01-01T00:00:00Z'}}
        with tempfile.TemporaryDirectory() as folder:
            data=Path(folder)/'offers.json'
            data.write_text(json.dumps({'offers':[],'page_lastmod':state}),encoding='utf-8')
            with patch.object(scraper,'DATA',data),patch.object(scraper,'load_config',return_value=cfg),patch.object(scraper,'fetch_source',return_value=(200,'Basic plan Contact sales',self.provider['source_url'])):
                result=scraper.run()
            self.assertEqual(result['page_lastmod'],state)
            self.assertEqual(json.loads(data.read_text(encoding='utf-8'))['page_lastmod'],state)

    def test_403_challenge_stores_status_and_visible_response_without_capture(self):
        cfg={'providers':[self.provider], 'extractors':[self.rule], 'settings':{}}
        with tempfile.TemporaryDirectory() as folder:
            data=Path(folder)/'offers.json'
            data.write_text(json.dumps({'offers':[]}),encoding='utf-8')
            challenge=scraper.SourceProbeError('challenge','Official source returned HTTP 403.',self.provider['source_url'],403,'Just a moment...')
            with patch.object(scraper,'DATA',data),patch.object(scraper,'load_config',return_value=cfg),patch.object(scraper,'fetch_source',side_effect=challenge):
                result=scraper.run()
        status=result['source_status']['sample']
        self.assertEqual((status['status'],status['http_status'],status['visible_excerpt']),('challenge',403,'Just a moment...'))
        self.assertEqual(status['captured_count'],0)
        self.assertEqual(result['offers'],[])

    def test_fetch_source_classifies_an_actual_challenge_body(self):
        url=self.provider['source_url']
        cfg={'settings':{'max_response_bytes':1000,'timeout_seconds':3,'user_agent':'test'}}
        failure=HTTPError(url,403,'Forbidden',{},io.BytesIO(b'<title>Just a moment...</title>'))
        with patch.object(scraper,'permitted'),patch.object(scraper,'fetch',side_effect=failure):
            with self.assertRaises(scraper.SourceProbeError) as caught:
                scraper.fetch_source(url,cfg)
        self.assertEqual((caught.exception.state,caught.exception.http_status,caught.exception.visible_excerpt),('challenge',403,'Just a moment...'))

    def test_legacy_checked_without_http_evidence_is_not_current(self):
        offer=dict(provider='sample',slug='sample-basic',kind='regular_price',fetched_at=datetime.now(timezone.utc).isoformat())
        legacy={'sample':{'status':'checked','captured_slugs':['sample-basic']}}
        evidenced={'sample':{'status':'evidenced','capture_status':'matched','http_status':200,'visible_excerpt':'Basic plan USD 19 / month','captured_slugs':['sample-basic']}}
        self.assertNotEqual(build.record_state(offer,legacy,{'fresh_hours':18})[0],build.CURRENT)
        self.assertEqual(build.record_state(offer,evidenced,{'fresh_hours':18})[0],build.CURRENT)

if __name__=='__main__': unittest.main()
