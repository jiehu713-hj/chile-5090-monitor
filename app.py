import asyncio, os, re, sqlite3, json, time, pathlib, datetime
from contextlib import asynccontextmanager
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

BASE=pathlib.Path(__file__).parent
DB=os.environ.get('DB_PATH',str(BASE/'monitor.db'))
INTERVAL=max(10,int(os.environ.get('CHECK_MINUTES','30')))*60
SOURCES=[
 {'id':'spd-giga','store':'SP Digital','model':'Gigabyte RTX 5090 WINDFORCE OC','url':'https://www.spdigital.cl/tarjeta-de-video-gigabyte-gv-n5090wf3oc-32gd/','type':'product'},
 {'id':'spd-msi','store':'SP Digital','model':'MSI RTX 5090 GAMING TRIO OC','url':'https://www.spdigital.cl/tarjeta-de-video-msi-geforce-rtx-5090-gaming-trio-oc-32gb-gddr7-512-bit-pci-e-gen-5/','type':'product'},
 {'id':'solo-msi','store':'SoloTodo / 多商家','model':'MSI RTX 5090 GAMING TRIO OC','url':'https://www.solotodo.cl/products/265691-msi-geforce-rtx-5090-32g-gaming-trio-oc-g5090-32gtc','type':'aggregator'},
 {'id':'solo-ventus','store':'SoloTodo / 多商家','model':'MSI RTX 5090 VENTUS 3X OC','url':'https://www.solotodo.cl/products/268124-msi-geforce-rtx-5090-32g-ventus-3x-oc-g5090-32v3c','type':'aggregator'},
 {'id':'solo-lightning','store':'SoloTodo / 多商家','model':'MSI RTX 5090 LIGHTNING Z','url':'https://www.solotodo.cl/products/385566-msi-geforce-rtx-5090-32g-lightning-z-g5090-32lz','type':'aggregator'},
 {'id':'solo-asus','store':'SoloTodo / 多商家','model':'ASUS RTX 5090 32GB - 品类','url':'https://www.solotodo.cl/tarjetas_de_video?gpus=2017220','type':'aggregator'},
 {'id': 'tw-autobuy', 'store': 'AUTOBUY', 'model': '台灣 RTX 5090 顯卡分類', 'url': 'https://www.autobuy.tw/3c/cate_17491', 'country': 'TW', 'currency': 'TWD', 'type': 'aggregator'},
 {'id': 'tw-zotac', 'store': 'AUTOBUY', 'model': 'ZOTAC RTX 5090 SOLID OC White（需核实整机限制）', 'url': 'https://www.autobuy.tw/3c/prod_398719', 'country': 'TW', 'currency': 'TWD', 'type': 'restricted'},
 {'id': 'my-ideal-amd', 'store': 'IDEALTECH', 'model': 'RTX 5090 AMD 整机（非单卡）', 'url': 'https://idealtech.com.my/product/amd-rtx5090-gamingpc/', 'country': 'MY', 'currency': 'MYR', 'type': 'bundle'},
 {'id': 'my-ideal-intel', 'store': 'IDEALTECH', 'model': 'RTX 5090 Intel 整机（非单卡）', 'url': 'https://idealtech.com.my/product/intel-rtx5090-gamingpc/', 'country': 'MY', 'currency': 'MYR', 'type': 'bundle'},
 {'id': 'ca-cc', 'store': 'Canada Computers', 'model': 'RTX 5090 搜索/商品列表', 'url': 'https://www.canadacomputers.com/en/search?s=5090', 'country': 'CA', 'currency': 'CAD', 'type': 'aggregator'},
 {'id': 'ca-me-msi', 'store': 'Memory Express', 'model': 'MSI RTX 5090 SUPRIM SOC 32GB', 'url': 'https://www.memoryexpress.com/Products/MX00132637', 'country': 'CA', 'currency': 'CAD', 'type': 'product'},
 {'id': 'ca-me-aorus', 'store': 'Memory Express', 'model': 'Gigabyte RTX 5090 WATERFORCE 32GB', 'url': 'https://www.memoryexpress.com/Products/MX00132565', 'country': 'CA', 'currency': 'CAD', 'type': 'product'},
]
for _source in SOURCES:
 _source.setdefault("country","CL");_source.setdefault("currency","CLP")

def conn():
 c=sqlite3.connect(DB,check_same_thread=False);c.row_factory=sqlite3.Row;return c

def init():
 with conn() as c:
  c.execute('CREATE TABLE IF NOT EXISTS states(id TEXT PRIMARY KEY, checked_at TEXT, status TEXT, price INTEGER, detail TEXT, http_code INTEGER)')
  c.execute('CREATE TABLE IF NOT EXISTS events(n INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, source_id TEXT, kind TEXT, message TEXT)')
  c.execute('CREATE TABLE IF NOT EXISTS history(n INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, source_id TEXT, price INTEGER)')

def price_from(soup,text,kind,currency):
 if kind in ('aggregator','bundle','restricted'):return None
 patterns={'CLP':r'\$\s*([1-9]\d{0,2}(?:[.,]\d{3}){2,3})','TWD':r'(?:NT\$|\$)\s*([1-9]\d{1,2}(?:,\d{3}){1,2})','MYR':r'RM\s*([1-9]\d{0,2}(?:,\d{3})*(?:\.\d{2})?)','CAD':r'\$\s*([1-9]\d{0,2}(?:,\d{3})*(?:\.\d{2})?)'}
 floors={'CLP':1000000,'TWD':70000,'MYR':12000,'CAD':2500}
 ceilings={'CLP':30000000,'TWD':350000,'MYR':100000,'CAD':20000}
 prices=[]
 for m in re.finditer(patterns[currency],text,re.I):
  raw=m.group(1)
  try:
   n=round(float(raw.replace(',',''))) if currency in ('MYR','CAD') else int(re.sub(r'\D','',raw))
   if floors[currency]<=n<=ceilings[currency]:prices.append(n)
  except ValueError:pass
 return min(prices) if prices else None

def inventory(text,kind,country):
 if kind=='aggregator':return 'listing_only','搜索/比价页面，不能确认独立显卡库存'
 if kind=='bundle':return 'bundle_only','整机页面，不代表独立显卡库存'
 if kind=='restricted':return 'restricted','可能限整机购买，请核对商品条件'
 t=' '.join(text.lower().split())
 negatives={'CL':['sin stock','agotado','no disponible','fuera de stock'],'TW':['補貨中','售完','缺貨','暫無庫存'],'MY':['out of stock','sold out'],'CA':['out of stock','not available online']}
 positives={'CL':['agregar al carro','agregar al carrito','comprar ahora'],'TW':['加入購物車','立即購買'],'MY':['add to cart','buy now'],'CA':['add to cart','in stock']}
 if any(v in t for v in negatives[country]):return 'out_of_stock','页面出现缺货词语（仍需核实）'
 if any(v in t for v in positives[country]):return 'possible_stock','检测到购买提示，需人工核实实际库存'
 return 'unknown','暂无可靠单卡库存信息'

async def fetch_one(client,s):
 stamp=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
 try:
  r=await client.get(s['url'],follow_redirects=True,timeout=22)
  if r.status_code!=200:raise ValueError('HTTP '+str(r.status_code))
  soup=BeautifulSoup(r.text,'html.parser')
  for e in soup(['script','style','nav','footer']):e.decompose()
  t=soup.get_text(' ',strip=True)
  if len(t)<150 or ('captcha' in t[:800].lower()) or ('cloudflare' in t[:400].lower() and len(t)<1300):raise ValueError('访问受限或内容不足')
  p=price_from(soup,t,s['type'],s['currency']);status,detail=inventory(t,s['type'],s['country']);http=200
 except Exception as ex:
  p=None;status='check_failed';detail=str(ex)[:150];http=0
 with conn() as c:
  old=c.execute('SELECT * FROM states WHERE id=?',(s['id'],)).fetchone()
  # On transient failures retain last verified price, but display failed check status
  if old and p is None:p=old['price']
  if old:
   if status=='possible_stock' and old['status'] not in ('possible_stock',):
    c.execute('INSERT INTO events(at,source_id,kind,message) VALUES(?,?,?,?)',(stamp,s['id'],'stock','检测到购买入口，需人工核实库存'))
   if p and old['price'] and p<old['price']:
    c.execute('INSERT INTO events(at,source_id,kind,message) VALUES(?,?,?,?)',(stamp,s['id'],'price',f'页面参考价下降：{s["currency"]} {old["price"]:,} → {p:,}'))
   if status=='out_of_stock' and old['status']=='possible_stock':
    c.execute('INSERT INTO events(at,source_id,kind,message) VALUES(?,?,?,?)',(stamp,s['id'],'stock','页面状态变为缺货'))
  c.execute('INSERT OR REPLACE INTO states VALUES(?,?,?,?,?,?)',(s['id'],stamp,status,p,detail,http))
  if p:c.execute('INSERT INTO history(at,source_id,price) VALUES(?,?,?)',(stamp,s['id'],p))

async def run_checks():
 async with httpx.AsyncClient(headers={'User-Agent':'Mozilla/5.0 (compatible; Chile5090Monitoring/1.0; contact: site owner)','Accept-Language':'es-CL,es;q=0.9'}) as client:
  for s in SOURCES:
   await fetch_one(client,s)
   await asyncio.sleep(1)

async def watch():
 while True:
  try:await run_checks()
  except Exception as ex:print('check error:',ex,flush=True)
  await asyncio.sleep(INTERVAL)

@asynccontextmanager
async def lifespan(app):
 init();task=asyncio.create_task(watch());yield;task.cancel()
 try:await task
 except asyncio.CancelledError:pass

app=FastAPI(lifespan=lifespan)
@app.get('/',response_class=HTMLResponse)
async def home():return (BASE/'index.html').read_text(encoding='utf8')
@app.get('/api/health')
async def health():return {'ok':True,'sources':len(SOURCES)}
@app.get('/api/data')
async def data():
 with conn() as c:
  states={r['id']:dict(r) for r in c.execute('SELECT * FROM states')}
  events=[dict(x) for x in c.execute('SELECT * FROM events ORDER BY n DESC LIMIT 100')]
  history=[dict(x) for x in c.execute('SELECT at,source_id,price FROM history ORDER BY n DESC LIMIT 120')]
 return {'sources':[{**s,**states.get(s['id'],{'status':'pending','price':None,'checked_at':None,'detail':'等待首次检查'})} for s in SOURCES], 'events':events,'history':history,'interval_minutes':INTERVAL//60}
