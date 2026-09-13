"""Regression checks for missing prices, source failures and robots restrictions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import build
import scraper

class SourceSafety(unittest.TestCase):
    def setUp(self):
        self.provider = dict(id='sample', name='Sample', source_url='https://example.com/pricing', affiliate_url='')
        self.rule = dict(provider='sample', mode='anchored_text', anchor='Basic plan', window_chars=100, title='Basic', currency='USD', billing_period='month', field_patterns={'price':r'USD (?P<value>[0-9.]+) / month'}, structure='Plan card')

    def test_no_record_when_price_is_missing(self):
        self.assertIsNone(scraper.offer_from_rule(self.provider,self.rule,'<p>Basic plan: Contact sales</p>'))

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
            with patch.object(scraper,'DATA',data),patch.object(scraper,'load_config',return_value=cfg),patch.object(scraper,'fetch_source',return_value=(200,'Basic plan Contact sales Pro plan USD 19 / month')):
                result=scraper.run()
        self.assertEqual([offer['slug'] for offer in result['offers']], ['sample-pro'])

    def test_currency_guard_and_annual_period(self):
        rule=dict(self.rule,raw_checks=['USD'],billing_period='year',field_patterns={'price':r'USD (?P<value>[0-9.]+) / year'})
        value=scraper.offer_from_rule(self.provider,rule,'Basic plan USD 15 / year')
        self.assertEqual((value['price'],value['billing_period'],value['kind']),(15,'year','regular_price'))
        self.assertIsNone(scraper.offer_from_rule(self.provider,rule,'Basic plan EUR 15 / year'))

    def test_explicit_currency_and_period_cannot_be_relabelled(self):
        currency_rule=dict(self.rule,field_patterns={'price':r'\$\s?(?P<value>[0-9.]+)\s*/ month'})
        period_rule=dict(self.rule,field_patterns={'price':r'\$\s?(?P<value>[0-9.]+)\s*/\s*\w+'})
        self.assertIsNone(scraper.offer_from_rule(self.provider,currency_rule,'Basic plan CAD $4 / month'))
        self.assertIsNone(scraper.offer_from_rule(self.provider,period_rule,'Basic plan $4 / site'))

    def test_failed_state_only_source_never_claims_checked(self):
        heading, detail, tile = build.state_only_display({'status':'unavailable','reason':'Official source returned HTTP 429.'}, 'price_rendered_by_js')
        self.assertEqual(heading, 'Latest source check did not complete.')
        self.assertIn('HTTP 429', detail)
        self.assertEqual(tile, 'Source check did not complete')
        heading, _, tile = build.state_only_display({'status':'available_no_price_rule'}, 'no_public_price')
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
                with patch.object(scraper,'DATA',data),patch.object(scraper,'load_config',return_value=cfg),patch.object(scraper,'fetch_source',return_value=(200,'Basic plan Contact sales'),side_effect=side_effect):
                    result=scraper.run()
                self.assertEqual(result['offers'],[old])
                self.assertEqual(result['source_status']['sample'].get('captured_count',0),0)

    def test_refresh_keeps_build_owned_state_keys(self):
        cfg={'providers':[self.provider], 'extractors':[self.rule], 'settings':{}}
        state={'/':{'hash':'abc','lastmod':'2026-01-01T00:00:00Z'}}
        with tempfile.TemporaryDirectory() as folder:
            data=Path(folder)/'offers.json'
            data.write_text(json.dumps({'offers':[],'page_lastmod':state}),encoding='utf-8')
            with patch.object(scraper,'DATA',data),patch.object(scraper,'load_config',return_value=cfg),patch.object(scraper,'fetch_source',return_value=(200,'Basic plan Contact sales')):
                result=scraper.run()
            self.assertEqual(result['page_lastmod'],state)
            self.assertEqual(json.loads(data.read_text(encoding='utf-8'))['page_lastmod'],state)

if __name__=='__main__': unittest.main()
