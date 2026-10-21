"""Evidence-first GPU monitor: one collector process; no paid API calls."""
import asyncio
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import xml.etree.ElementTree as ET
from contextlib import contextmanager, asynccontextmanager
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

BASE = pathlib.Path(__file__).parent
DB = os.getenv('DB_PATH', str(BASE / 'monitor-v2.db'))
DATABASE_URL = os.getenv('DATABASE_URL', '')
INTERVAL = max(10, int(os.getenv('CHECK_MINUTES', '30'))) * 60
STALE_SECONDS = INTERVAL * 3
UA = 'Global5090Monitor/2.0 (+https://chile-5090-monitor.onrender.com)'
CATALOG = json.loads((BASE / 'sources.json').read_text(encoding='utf-8'))
SOURCES = CATALOG['sources']
SOCIAL = [
    {'id': 'reddit-public', 'platform': 'Reddit', 'url': 'https://www.reddit.com/search.rss?q=RTX%205090&sort=new', 'mode': 'public_feed'},
    {'id': 'x-auth', 'platform': 'X', 'url': 'https://x.com/search?q=RTX%205090&f=live', 'mode': 'authorization_required'},
    {'id': 'instagram-auth', 'platform': 'Instagram', 'url': 'https://www.instagram.com/explore/tags/rtx5090/', 'mode': 'authorization_required'},
    {'id': 'facebook-auth', 'platform': 'Facebook', 'url': 'https://www.facebook.com/search/posts/?q=RTX%205090', 'mode': 'authorization_required'},
]
RUN = {'started_at': None, 'finished_at': None, 'running': False, 'error': None}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


@contextmanager
def connection():
    if DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row
        c = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)
    else:
        c = sqlite3.connect(DB, timeout=15)
        c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def sql(c, statement, values=()):
    return c.execute(statement.replace('?', '%s') if DATABASE_URL else statement, values)


def init():
    pk = 'BIGSERIAL PRIMARY KEY' if DATABASE_URL else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    with connection() as c:
        for statement in [
            'CREATE TABLE IF NOT EXISTS monitor_state (source_id TEXT PRIMARY KEY, payload TEXT NOT NULL)',
            f'CREATE TABLE IF NOT EXISTS observations (id {pk}, source_id TEXT NOT NULL, at TEXT NOT NULL, price TEXT NOT NULL, currency TEXT NOT NULL, stock TEXT NOT NULL, evidence TEXT NOT NULL)',
            f'CREATE TABLE IF NOT EXISTS alerts (id {pk}, source_id TEXT NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS posts (id TEXT PRIMARY KEY, platform TEXT NOT NULL, published_at TEXT, collected_at TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL, evidence TEXT NOT NULL)',
            'CREATE INDEX IF NOT EXISTS observation_source_time ON observations(source_id, at)',
        ]:
            sql(c, statement)


def safe_url(value):
    p = urlparse(str(value))
    return str(value) if p.scheme == 'https' and p.hostname and not p.username else None


def product_nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from product_nodes(item)
    elif isinstance(value, dict):
        typ = value.get('@type', [])
        if typ == 'Product' or isinstance(typ, list) and 'Product' in typ:
            yield value
        if '@graph' in value:
            yield from product_nodes(value['@graph'])


def parse_product(html, source):
    """Never infer prices from search pages, cart buttons or recommendations."""
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    if re.search(r'captcha|just a moment|access denied|robot check', title, re.I):
        raise ValueError('访问验证或反爬页面')
    result = {'status': 'public_clue', 'evidence': 'public_page', 'stock': 'unknown', 'price': None,
              'detail': '页面可访问；列表或非结构化内容不能确认单卡价格与库存', 'confidence': 'low'}
    if source['type'] != 'product':
        return result
    products = []
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            products.extend(product_nodes(json.loads(script.string or script.get_text())))
        except (ValueError, TypeError):
            continue
    candidates = []
    for p in products:
        name = str(p.get('name', ''))
        if not re.search(r'RTX\s*[- ]?5090\b', name, re.I):
            continue
        if re.search(r'5090\s*D\b|laptop|notebook|gaming\s*pc|desktop|整机|bundle|\bPSU\b|AI\s*BOX', name, re.I):
            continue
        # Bind the structured object to the primary page, not a recommendation.
        heading = soup.find('h1')
        primary = heading.get_text(' ', strip=True) if heading else title
        normalize = lambda v: re.sub(r'[^\w]', '', v).casefold()
        if normalize(name) not in normalize(primary):
            continue
        offers = p.get('offers', [])
        offers = offers if isinstance(offers, list) else [offers]
        if len(offers) != 1 or not isinstance(offers[0], dict):
            continue
        o = offers[0]
        if o.get('@type') == 'AggregateOffer' or o.get('priceCurrency') != source['currency']:
            continue
        try:
            amount = Decimal(str(o['price']))
            if not amount.is_finite() or amount <= 0:
                continue
        except (KeyError, InvalidOperation):
            continue
        availability = str(o.get('availability', '')).rsplit('/', 1)[-1]
        stock = {'InStock': 'possible_stock', 'OutOfStock': 'out_of_stock', 'SoldOut': 'out_of_stock',
                 'PreOrder': 'preorder', 'BackOrder': 'backorder'}.get(availability, 'unknown')
        candidates.append((name, str(amount), stock))
    if len(candidates) == 1:
        name, price, stock = candidates[0]
        result.update(price=price, stock=stock, confidence='medium', product_name=name,
                      detail='商家页面结构化 Offer；仍需核实结账、税费、数量和单卡购买条件')
    return result


def save_state(source_id, result):
    stamp = now()
    result = {**result, 'checked_at': stamp}
    with connection() as c:
        old_row = sql(c, 'SELECT payload FROM monitor_state WHERE source_id=?', (source_id,)).fetchone()
        old = json.loads(old_row['payload']) if old_row else {}
        if result.get('price') is not None:
            result.update(last_price=result['price'], last_price_at=stamp)
            sql(c, 'INSERT INTO observations(source_id,at,price,currency,stock,evidence) VALUES(?,?,?,?,?,?)',
                (source_id, stamp, result['price'], result['currency'], result['stock'], result['evidence']))
            if old.get('price') is not None and old.get('status') == 'public_clue':
                if Decimal(result['price']) < Decimal(old['price']):
                    sql(c, 'INSERT INTO alerts(source_id,at,kind,message) VALUES(?,?,?,?)',
                        (source_id, stamp, 'price_drop', f"网页参考价下降：{old['price']} → {result['price']} {result['currency']}（需核实）"))
                if result['stock'] == 'possible_stock' and old.get('stock') != 'possible_stock':
                    sql(c, 'INSERT INTO alerts(source_id,at,kind,message) VALUES(?,?,?,?)',
                        (source_id, stamp, 'stock_clue', '页面出现 InStock 线索，不代表可批量采购'))
        else:
            result['last_price'] = old.get('last_price')
            result['last_price_at'] = old.get('last_price_at')
        sql(c, 'INSERT INTO monitor_state(source_id,payload) VALUES(?,?) ON CONFLICT(source_id) DO UPDATE SET payload=excluded.payload',
            (source_id, json.dumps(result, ensure_ascii=False)))
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)).isoformat(timespec='seconds')
        for table, field in [('observations', 'at'), ('alerts', 'at'), ('posts', 'collected_at')]:
            sql(c, f'DELETE FROM {table} WHERE {field} < ?', (cutoff,))


class Fetcher:
    def __init__(self, client):
        self.client = client
        self.robots = {}

    async def get(self, url):
        """Fail closed on robots errors; do not bypass auth, CAPTCHA or redirects."""
        origin = 'https://' + urlparse(url).netloc
        if origin not in self.robots:
            r = await self.client.get(origin + '/robots.txt')
            parser = RobotFileParser()
            if r.status_code == 404:
                parser.parse([])
            elif r.status_code == 200:
                parser.parse(r.text.splitlines())
            else:
                raise ValueError(f'robots.txt 无法确认（HTTP {r.status_code}），未抓取')
            self.robots[origin] = parser
        if not self.robots[origin].can_fetch(UA, url):
            raise ValueError('robots.txt 禁止访问，未抓取')
        delay = self.robots[origin].crawl_delay(UA) or 1
        if delay > 60:
            raise ValueError('站点要求较长采集间隔，等待专用适配器')
        await asyncio.sleep(max(1, delay))
        async with self.client.stream('GET', url) as r:
            if r.status_code != 200:
                raise ValueError(f'HTTP {r.status_code}（未绕过限制或重定向）')
            chunks, length = [], 0
            async for chunk in r.aiter_bytes():
                length += len(chunk)
                if length > 3_000_000:
                    raise ValueError('页面超过采集大小限制')
                chunks.append(chunk)
            return b''.join(chunks).decode(r.encoding or 'utf-8', errors='replace')


async def collect_retail(fetcher, source):
    try:
        result = parse_product(await fetcher.get(source['url']), source)
    except (httpx.HTTPError, ValueError) as ex:
        detail = str(ex) if isinstance(ex, ValueError) else type(ex).__name__
        result = {'status': 'fetch_failed', 'evidence': 'public_page', 'stock': 'unknown',
                  'price': None, 'confidence': 'none', 'detail': detail[:180]}
    save_state(source['id'], {**result, 'currency': source['currency']})


async def collect_reddit(fetcher):
    source = SOCIAL[0]
    try:
        root = ET.fromstring(await fetcher.get(source['url']))
        ns = {'a': 'http://www.w3.org/2005/Atom'}
        if root.tag != '{http://www.w3.org/2005/Atom}feed':
            raise ValueError('返回内容不是 Atom 消息源')
        count = 0
        with connection() as c:
            for entry in root.findall('a:entry', ns)[:30]:
                title = entry.findtext('a:title', '', ns)[:400]
                if not re.search(r'5090', title, re.I):
                    continue
                link = entry.find('a:link', ns)
                url = safe_url(link.get('href', '') if link is not None else '')
                if not url or urlparse(url).hostname not in ('www.reddit.com', 'reddit.com'):
                    continue
                identity = hashlib.sha256(url.encode()).hexdigest()
                sql(c, 'INSERT INTO posts VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                    (identity, 'Reddit', entry.findtext('a:updated', None, ns), now(), title, url, 'public_feed'))
                count += 1
        save_state(source['id'], {'status': 'public_clue', 'evidence': 'public_feed', 'price': None,
                                 'detail': f'公开 RSS 取得 {count} 条相关标题；不等于 API 验证或已确认库存', 'confidence': 'low'})
    except (httpx.HTTPError, ValueError, ET.ParseError) as ex:
        save_state(source['id'], {'status': 'fetch_failed', 'evidence': 'public_feed', 'price': None,
                                 'detail': (str(ex) if isinstance(ex, ValueError) else type(ex).__name__)[:180], 'confidence': 'none'})


async def run_checks():
    RUN.update(started_at=now(), running=True, error=None)
    try:
        async with httpx.AsyncClient(headers={'User-Agent': UA}, timeout=20, follow_redirects=False) as client:
            fetcher = Fetcher(client)
            for source in SOURCES:
                await collect_retail(fetcher, source)
            await collect_reddit(fetcher)
        RUN['finished_at'] = now()
    except Exception as ex:
        RUN['error'] = type(ex).__name__
    finally:
        RUN['running'] = False


async def watch():
    while True:
        await run_checks()
        await asyncio.sleep(INTERVAL)


@asynccontextmanager
async def lifespan(app):
    init()
    task = asyncio.create_task(watch()) if os.getenv('COLLECTOR_ENABLED', '1') == '1' else None
    yield
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title='Global RTX 5090 Monitor', lifespan=lifespan)


@app.get('/')
async def home():
    return FileResponse(BASE / 'index.html')


@app.get('/assets/{name}')
async def assets(name: str):
    if name not in ('app.js', 'style.css'):
        raise HTTPException(404)
    return FileResponse(BASE / 'static' / name)


@app.get('/api/health')
async def health():
    with connection() as c:
        sql(c, 'SELECT 1')
    return {'ok': True, 'version': '2.0.0', 'sources': len(SOURCES), 'collector': RUN,
            'storage': 'postgres' if DATABASE_URL else 'ephemeral_sqlite'}


def state_for(source, states):
    result = {**source, **states.get(source['id'], {'status': 'pending', 'price': None, 'stock': 'unknown',
                                                  'confidence': 'none', 'detail': '等待首次采集', 'checked_at': None})}
    checked = result.get('checked_at')
    result['stale'] = bool(checked and (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(checked)).total_seconds() > STALE_SECONDS)
    return result


@app.get('/api/data')
async def data(country: str = '', q: str = Query('', max_length=100)):
    with connection() as c:
        states = {r['source_id']: json.loads(r['payload']) for r in sql(c, 'SELECT * FROM monitor_state')}
        selected = [s for s in SOURCES if (not country or s['country'] == country.upper()) and
                    (not q or q.casefold() in (s['store'] + ' ' + s['model']).casefold())]
        ids = {s['id'] for s in selected}
        events = [dict(r) for r in sql(c, 'SELECT * FROM alerts ORDER BY id DESC LIMIT 200') if r['source_id'] in ids]
        posts = [dict(r) for r in sql(c, 'SELECT * FROM posts ORDER BY published_at DESC LIMIT 50')]
    social = []
    for s in SOCIAL:
        default = {'status': 'authorization_required', 'detail': '未接入授权 API；仅提供公开搜索入口，不声称已采集。授权范围、平台审核与可能费用需另行确认。', 'confidence': 'none'}
        social.append({**s, **(states.get(s['id'], default) if s['mode'] == 'authorization_required' else state_for(s, states))})
    return {'version': '2.0.0', 'generated_at': now(), 'sources': [state_for(s, states) for s in selected],
            'countries': CATALOG['countries'], 'social': social, 'posts': posts, 'events': events,
            'interval_minutes': INTERVAL // 60, 'stale_minutes': STALE_SECONDS // 60, 'collector': RUN,
            'verified_api_count': 0, 'storage': 'postgres' if DATABASE_URL else 'ephemeral_sqlite',
            'limitations': ['无已接入并验证的库存 API', '网页与社交消息均为线索，不能确认批量可购数量',
                            '免费服务休眠时停止采集；本地 SQLite 在重启或部署后可能丢失',
                            '不进行跨币种排名；价格不统一含税、运费或关税']}


@app.get('/api/history/{source_id}')
async def history(source_id: str, days: int = Query(30, ge=1, le=90)):
    if source_id not in {s['id'] for s in SOURCES}:
        raise HTTPException(404, 'Unknown source')
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec='seconds')
    with connection() as c:
        rows = [dict(r) for r in sql(c, 'SELECT at,price,currency,stock,evidence FROM observations WHERE source_id=? AND at>=? ORDER BY at DESC LIMIT 2000', (source_id, cutoff))]
    return {'source_id': source_id, 'points': list(reversed(rows)), 'retention_days': 90}
