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
]

def conn():
 c=sqlite3.connect(DB,check_same_thread=False);c.row_factory=sqlite3.Row;return c

def init():
 with conn() as c:
  c.execute('CREATE TABLE IF NOT EXISTS states(id TEXT PRIMARY KEY, checked_at TEXT, status TEXT, price INTEGER, detail TEXT, http_code INTEGER)')
  c.execute('CREATE TABLE IF NOT EXISTS events(n INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, source_id TEXT, kind TEXT, message TEXT)')
  c.execute('CREATE TABLE IF NOT EXISTS history(n INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, source_id TEXT, price INTEGER)')

def price_from(soup,text,kind):
 # Chilean pesos are usually formatted $3.799.990; reject low incidental amounts
 samples=[]
 if kind=='product':
  for x in soup.select('meta[property="product:price:amount"], meta[property="og:price:amount"], [itemprop="price"]'):
   v=x.get('content') or x.get('value') or x.get_text(' ',strip=True)
   m=re.search(r'\d[\d.,]*',v or '')
   if m:
    try:
     n=int(re.sub(r'[^0-9]','',m.group()));
     if 1000000 <=n<=30000000:samples.append(n)
    except:pass
 for m in re.finditer(r'\$\s*([1-9]\d{0,2}(?:[.,]\d{3}){2,3})',text):
  n=int(re.sub(r'\D','',m.group(1)))
  if 1000000<=n<=30000000:samples.append(n)
 return min(samples) if samples else None

def inventory(text,kind):
 if kind=='aggregator':return 'listing_only','比价/价格线索，不代表单家现货'
 t=' '.join(text.lower().split())
 # Only explicit unavailable is safe to infer; anti-bot is handled separately
 neg=['sin stock','agotado','no disponible','producto agotado','fuera de stock','stock: 0']
 pos=['añadir al carrito','agregar al carro','agregar al carrito','comprar ahora','disponible para despacho']
 if any(x in t for x in neg):return 'out_of_stock','页面出现缺货词语（仍需核实）'
 if any(x in t for x in pos):return 'possible_stock','页面显示购买/配送入口，需人工核实实际数量'
 return 'unknown','网页没有可明确判定库存的信号'

async def fetch_one(client,s):
 stamp=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
 try:
  r=await client.get(s['url'],follow_redirects=True,timeout=22)
  if r.status_code!=200:raise ValueError('HTTP '+str(r.status_code))
  soup=BeautifulSoup(r.text,'html.parser')
  for e in soup(['script','style','nav','footer']):e.decompose()
  t=soup.get_text(' ',strip=True)
  if len(t)<150 or ('captcha' in t[:800].lower()) or ('cloudflare' in t[:400].lower() and len(t)<1300):raise ValueError('访问受限或内容不足')
  p=price_from(soup,t,s['type']);status,detail=inventory(t,s['type']);http=200
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
    c.execute('INSERT INTO events(at,source_id,kind,message) VALUES(?,?,?,?)',(stamp,s['id'],'price',f'页面参考价下降：CLP {old["price"]:,} → {p:,}'))
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
