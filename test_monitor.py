"""Offline fixtures only. They never enter the production database."""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
sys.path.insert(0, str(pathlib.Path(__file__).parent))
os.environ['COLLECTOR_ENABLED'] = '0'
import app as monitor
import httpx
from fastapi.testclient import TestClient

SOURCE = {'id': 'test', 'country': 'US', 'currency': 'USD', 'type': 'product', 'url': 'https://example.com/product'}


def page(name='MSI RTX 5090 GAMING TRIO', price='2999.95', currency='USD', stock='InStock', extra=None):
    product = {'@type': 'Product', 'name': name, 'offers': {'@type': 'Offer', 'price': price,
               'priceCurrency': currency, 'availability': 'https://schema.org/' + stock}}
    return f'<title>{name}</title><h1>{name}</h1><script type="application/ld+json">{json.dumps(extra or product)}</script>'


class ParserTests(unittest.TestCase):
    def test_exact_decimal_and_unverified_stock(self):
        r = monitor.parse_product(page(), SOURCE)
        self.assertEqual(r['price'], '2999.95')
        self.assertEqual(r['stock'], 'possible_stock')
        self.assertEqual(r['evidence'], 'public_page')
        self.assertNotEqual(r['status'], 'verified_api')

    def test_reject_non_single_cards(self):
        for name in ['MSI RTX 5090 D', 'RTX 5090 Laptop', 'RTX 5090 Gaming PC', 'RTX 5090 AI BOX', 'RTX 5090 PSU Bundle']:
            with self.subTest(name=name):
                self.assertIsNone(monitor.parse_product(page(name=name), SOURCE)['price'])

    def test_no_prices_from_listings_bundles_or_restricted_pages(self):
        for typ in ['listing', 'bundle', 'restricted', 'aggregator']:
            self.assertIsNone(monitor.parse_product(page(), {**SOURCE, 'type': typ})['price'])

    def test_reject_wrong_currency_or_nonfinite_prices(self):
        self.assertIsNone(monitor.parse_product(page(currency='CAD'), SOURCE)['price'])
        for price in ['NaN', 'Infinity', '-1', '0', '2,999.99']:
            self.assertIsNone(monitor.parse_product(page(price=price), SOURCE)['price'])

    def test_cart_button_is_not_stock_or_price_evidence(self):
        r = monitor.parse_product('<h1>RTX 5090</h1><p>$2999 add to cart</p>', SOURCE)
        self.assertIsNone(r['price'])
        self.assertEqual(r['stock'], 'unknown')

    def test_recommendation_not_primary_product(self):
        html = page().replace('<h1>MSI RTX 5090 GAMING TRIO</h1>', '<h1>MSI RTX 5080</h1>')
        self.assertIsNone(monitor.parse_product(html, SOURCE)['price'])

    def test_graph_and_out_of_stock(self):
        p = {'@type': 'Product', 'name': 'MSI RTX 5090 GAMING TRIO', 'offers': {'price': '2999', 'priceCurrency': 'USD', 'availability': 'https://schema.org/OutOfStock'}}
        self.assertEqual(monitor.parse_product(page(extra={'@graph': [p]}), SOURCE)['stock'], 'out_of_stock')
        self.assertIsNone(monitor.parse_product(page(extra=[p, p]), SOURCE)['price'])

    def test_captcha_not_empty_inventory(self):
        with self.assertRaises(ValueError):
            monitor.parse_product('<title>Just a moment</title>', SOURCE)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(monitor, 'DB', str(pathlib.Path(self.temp.name) / 'test.db'))
        self.db_patch.start()
        self.url_patch = patch.object(monitor, 'DATABASE_URL', '')
        self.url_patch.start()
        monitor.init()

    def tearDown(self):
        self.db_patch.stop()
        self.url_patch.stop()
        self.temp.cleanup()

    def save(self, price='3000', stock='out_of_stock'):
        monitor.save_state('test', {'status': 'public_clue', 'evidence': 'public_page', 'price': price, 'currency': 'USD', 'stock': stock})

    def test_failure_does_not_replay_price_or_history(self):
        self.save()
        monitor.save_state('test', {'status': 'fetch_failed', 'price': None, 'stock': 'unknown'})
        with monitor.connection() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 1)
            state = json.loads(c.execute('SELECT payload FROM monitor_state').fetchone()[0])
            self.assertIsNone(state['price'])
            self.assertEqual(state['last_price'], '3000')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM alerts').fetchone()[0], 0)

    def test_initial_baseline_and_deduplicated_changes(self):
        self.save()
        with monitor.connection() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM alerts').fetchone()[0], 0)
        self.save('2900', 'possible_stock')
        self.save('2900', 'possible_stock')
        with monitor.connection() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM alerts').fetchone()[0], 2)

    def test_api_and_filter_and_asset_allowlist(self):
        with TestClient(monitor.app) as client:
            self.assertEqual(client.get('/api/health').json()['version'], '2.0.0')
            result = client.get('/api/data?country=CL').json()
            self.assertTrue(result['sources'])
            self.assertTrue(all(s['country'] == 'CL' for s in result['sources']))
            self.assertEqual(result['verified_api_count'], 0)
            self.assertEqual(len(result['social']), 4)
            self.assertTrue(all(s['status'] == 'authorization_required' for s in result['social'][1:]))
            self.assertEqual(client.get('/assets/app.py').status_code, 404)
            self.assertEqual(client.get('/api/history/unknown').status_code, 404)
            self.assertEqual(client.get('/api/history/spd-giga?days=0').status_code, 422)
            self.assertEqual(client.get('/assets/app.js').status_code, 200)
            self.assertIn('5090 ATLAS', client.get('/').text)

    def test_reddit_dedup_and_title_escape_is_client_responsibility(self):
        feed = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>RTX 5090 restock?</title><updated>2026-10-21T00:00:00Z</updated><link href="https://www.reddit.com/r/nvidia/comments/test"/></entry></feed>'
        fetcher = AsyncMock()
        fetcher.get.return_value = feed
        asyncio.run(monitor.collect_reddit(fetcher))
        asyncio.run(monitor.collect_reddit(fetcher))
        with monitor.connection() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 1)

    def test_stale(self):
        r = monitor.state_for(SOURCE, {'test': {'checked_at': '2020-01-01T00:00:00+00:00'}})
        self.assertTrue(r['stale'])


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_robots_deny_stops_product_request(self):
        requests = []
        async def transport(req):
            requests.append(str(req.url))
            return httpx.Response(200, text='User-agent: *\nDisallow: /')
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            with self.assertRaises(ValueError):
                await monitor.Fetcher(client).get('https://example.com/product')
        self.assertEqual(requests, ['https://example.com/robots.txt'])

    async def test_http_failure_is_not_out_of_stock(self):
        fake = AsyncMock()
        fake.get.side_effect = httpx.ConnectTimeout('redacted')
        with patch.object(monitor, 'save_state') as save:
            await monitor.collect_retail(fake, SOURCE)
            r = save.call_args.args[1]
            self.assertEqual(r['status'], 'fetch_failed')
            self.assertIsNone(r['price'])
            self.assertEqual(r['stock'], 'unknown')


if __name__ == '__main__':
    unittest.main(verbosity=2)
