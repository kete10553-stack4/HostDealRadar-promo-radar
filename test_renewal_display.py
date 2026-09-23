import copy
import unittest

import build


class RenewalDisplay(unittest.TestCase):
    def record(self, **changes):
        record = dict(provider='test', title='One', slug='one', category='Web hosting',
                      kind='promotion', price=1, renewal_price=10, currency='GBP',
                      billing_period='month', source_url='https://example.com/pricing',
                      fetched_at='2026-09-13T00:00:00Z', condition='First month at the advertised amount.',
                      field_evidence={'price': '£1/mo', 'renewal_price': 'Then only £10/mo'})
        record.update(changes)
        return record

    def test_pair_uses_own_source_and_date_without_mutating_data(self):
        record = self.record()
        before = copy.deepcopy(record)
        text = build.rate_pair(record)
        self.assertIn('£1', text)
        self.assertIn('Then only £10/mo', text)
        self.assertEqual(text.count(record['source_url']), 2)
        self.assertEqual(text.count(record['fetched_at']), 2)
        self.assertNotIn('%', text)
        self.assertEqual(record, before)

    def test_missing_initial_price_and_missing_renewal_are_not_filled(self):
        only_renewal = build.rate_pair(self.record(price=None))
        self.assertIn('Unknown', only_renewal)
        self.assertIn('Then only £10/mo', only_renewal)
        no_renewal = build.rate_pair(self.record(renewal_price=None))
        self.assertIn('Unknown', no_renewal)
        self.assertNotIn('Then only £10/mo', no_renewal)

    def test_alternative_billing_and_strikethrough_are_not_renewal(self):
        for evidence in ('billed annually or $16', 'only 12.71', '$11.64 Save', '8.95'):
            with self.subTest(evidence=evidence):
                record = self.record(field_evidence={'renewal_price': evidence})
                self.assertFalse(build.renewal_supported(record))
                self.assertNotIn('Then only £10/mo', build.rate_pair(record))

    def test_conflicting_currency_or_period_is_not_paired(self):
        for changes in ({'renewal_currency': 'USD'}, {'renewal_billing_period': 'year'},
                        {'field_evidence': {'renewal_price': 'Renews at EUR 10/mo'}},
                        {'field_evidence': {'renewal_price': 'Renews at £10/year'}},
                        {'currency': None}, {'billing_period': None}):
            with self.subTest(changes=changes):
                self.assertFalse(build.renewal_supported(self.record(**changes)))

    def test_rule_with_explicit_renewal_check_can_support_listprice(self):
        record = self.record(field_evidence={'renewal_price': 'ListPrice: 10'})
        self.assertFalse(build.renewal_supported(record))
        self.assertTrue(build.renewal_supported(record, {'checks': ['per month on renewal']}))

    def test_selection_does_not_mix_currencies_or_units(self):
        a = self.record(slug='a', renewal_price=20)
        b = self.record(slug='b', renewal_price=30)
        c = self.record(slug='c', currency='USD', renewal_price=1000,
                        field_evidence={'renewal_price': 'Then $1000/mo'})
        d = self.record(slug='d', billing_period='year', renewal_price=2000,
                        field_evidence={'renewal_price': 'Then £2000/year'})
        self.assertEqual([o['slug'] for o in build.featured_renewals([a,b,c,d], {})], ['b','a'])
        self.assertEqual(build.featured_renewals([self.record(renewal_price=1)], {}), [])

    def test_percentage_omission_preserves_tax_terms(self):
        self.assertEqual(build.public_terms('Entry rate; the discount is up to 80%.'), 'Entry rate')
        self.assertEqual(build.public_terms('Monthly rate including 19% VAT.'), 'Monthly rate including 19% VAT.')
        self.assertEqual(build.public_terms('Monthly rate; the page states the yearly saving.'), 'Monthly rate')


if __name__ == '__main__':
    unittest.main()
