"""Nextory account and audiobook integration service."""
import asyncio, dataclasses, json, os, re, shutil, subprocess, tempfile, urllib.request, threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from enum import Enum
from nextory.client import NextoryClient
from nextory.models import LibraryListType

try:
 import requests
 from requests.adapters import HTTPAdapter
 _HTTP_SESSION=requests.Session()
 _HTTP_SESSION.mount('http://',HTTPAdapter(pool_connections=16,pool_maxsize=16))
 _HTTP_SESSION.mount('https://',HTTPAdapter(pool_connections=16,pool_maxsize=16))
except ImportError:
 _HTTP_SESSION=None
CONFIG=Path(os.environ.get('DOWNLOAD_FOLDER','/downloads'))/'.nextory_config.json'
TMP=Path(os.environ.get('DOWNLOAD_FOLDER','/downloads'))/'.tmp_work'
def _load():
 try:return json.loads(CONFIG.read_text())
 except (OSError,ValueError):return {}
def _save(d):
 CONFIG.parent.mkdir(parents=True,exist_ok=True); p=CONFIG.with_suffix('.tmp');p.write_text(json.dumps(d));os.chmod(p,0o600);p.replace(CONFIG)
def _plain(v):
 if dataclasses.is_dataclass(v):return {k:_plain(x) for k,x in dataclasses.asdict(v).items()}
 if isinstance(v,Enum):return v.value
 if isinstance(v,list):return [_plain(x) for x in v]
 if isinstance(v,dict):return {k:_plain(x) for k,x in v.items()}
 if hasattr(v,'isoformat'):return v.isoformat()
 return v
def _client(c):
 def updated(a,b):
  n=_load();n.update({k:v for k,v in c.items() if k not in ('login_token','profile_token')});n['login_token']=a or n.get('login_token');n['profile_token']=b or n.get('profile_token');_save(n)
 return NextoryClient(login_token=c.get('login_token'),profile_token=c.get('profile_token'),login_key=c.get('login_key'),username=c.get('username'),country=c.get('country'),device_id=c.get('device_id'),on_tokens_updated=updated)
def _run(f):return asyncio.run(f)
def status():
 c=_load();return {'logged_in':bool(c.get('login_token')),'profile_selected':bool(c.get('profile_token')),'username':c.get('username',''),'country':c.get('country','DE')}
def login(username,password,country='DE'):
 if not username or not password:raise ValueError('Email and password required')
 c={'username':username,'country':country.upper()}
 async def go():
  async with _client(c) as x:
   c['device_id']=x.device_id;c['login_token']=await x.login(username,password);return _plain((await x.get_profiles()).profiles)
 profiles=_run(go());c['profiles']=profiles;_save(c)
 return select_profile(profiles[0]['login_key']) if len(profiles)==1 else {'profiles':profiles,'profile_required':True}
def select_profile(login_key):
 c=_load()
 if not c.get('login_token'):raise ValueError('Login required')
 async def go():
  async with _client(c) as x:
   c['profile_token']=await x.select_profile(login_key);c['login_key']=login_key;return _plain((await x.get_profiles()).profiles)
 profiles=_run(go());c['profiles']=profiles;_save(c);return {'logged_in':True,'profile_selected':True,'profiles':profiles}
def logout():
 c=_load()
 if c.get('login_token'):
  async def go():
   async with _client(c) as x:await x.logout()
  _run(go())
 CONFIG.unlink(missing_ok=True)
def _auth():
 c=_load()
 if not c.get('profile_token'):raise ValueError('Choose Nextory profile first')
 return c
def _product(p):
 d=_plain(p);d['cover_url']=next((f.get('img_url') for f in d.get('formats',[]) if f.get('img_url')),'');return d
def search(phrase):
 c=_auth()
 async def go():
  async with _client(c) as x:return _plain((await x.search_books(phrase,per=30)).products)
 return {'products':[_product(x) for x in _run(go())]}
def library():
 c=_auth()
 async def go():
  async with _client(c) as x:
   lists=(await x.get_libraries()).product_lists;out=[]
   for typ in (LibraryListType.ONGOING,LibraryListType.WANT_TO_READ):
    item=next((z for z in lists if z.type==typ),None)
    if item:out.extend(_plain((await x.get_library(typ,item.id,per=50)).products))
   return out
 return {'products':[_product(x) for x in _run(go())]}
def _details(book_id):
 c=_auth()
 async def go():
  async with _client(c) as x:
   p=await x.get_product_details(book_id);f=next((f for f in p.formats if str(f.type).lower().endswith('hls')),None)
   if f is None:raise ValueError('No HLS audio format')
   return _plain(p),_plain(await x.get_audio_package(f.identifier)),x.auth_headers,f.identifier
 return _run(go())
def book(book_id):
 p,a,h,f=_details(book_id);return {'book':_product(p),'format_id':f,'chapters':[{'title':x.get('title') or f'Chapter {i+1}','start_at':x['start_at'],'duration':x['duration']} for i,x in enumerate(a['files'])]}
def _fetch(url,h):
 if _HTTP_SESSION is not None:
  response=_HTTP_SESSION.get(url,headers=h,timeout=60)
  response.raise_for_status()
  return response.content
 with urllib.request.urlopen(urllib.request.Request(url,headers=h),timeout=60) as r:return r.read()
def _parse(data,base):
 seq=0;enc=False;key=None;iv=None;out=[]
 for line in data.decode('utf8','replace').splitlines():
  line=line.strip()
  if line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
   try:seq=int(line.split(':',1)[1])
   except ValueError:pass
  elif line.startswith('#EXT-X-KEY'):
   enc='METHOD=AES-128' in line
   if enc:
    m=re.search(r'URI="([^"]+)"',line);key=urllib.request.urljoin(base,m.group(1)) if m else None
    m=re.search(r'IV=0x([0-9a-fA-F]+)',line);iv=m.group(1) if m else None
  elif line and not line.startswith('#'):out.append((urllib.request.urljoin(base,line),enc,key,iv or (f'{seq:032x}' if enc else None)));seq+=1
 return out
def download(data,folder,progress):
 p,a,h,_=_details(int(data.get('book_id') or data.get('id')));files=a['files'];names=[x.get('title') or f'Chapter {i+1}' for i,x in enumerate(files)];names=[(data.get('chapters') or [{}]*len(files))[i].get('title') or names[i] for i in range(len(files))]
 title=data.get('title') or p['title'];safe=re.sub(r'[\\/:*?"<>|]+','_',title).strip() or 'Nextory';target=Path(folder)/f'{safe}.m4b';target.parent.mkdir(parents=True,exist_ok=True);TMP.mkdir(parents=True,exist_ok=True);work=Path(tempfile.mkdtemp(dir=TMP,prefix='nextory_'))
 try:
  listing=work/'files.txt';meta=work/'meta.txt';ff=[';FFMETADATA1',f'title={title}',f"artist={data.get('author') or ', '.join(x['name'] for x in p.get('authors',[]))}",'genre=Audiobook']
  key_cache={};key_cache_lock=threading.Lock()
  def get_key(url):
   # Keep one fetch in flight for each key URL, even when segments share it.
   with key_cache_lock:
    if url not in key_cache:key_cache[url]=_fetch(url,h)
    return key_cache[url]
  def download_segment(i,j,segment):
   url,enc,key,iv=segment;b=_fetch(url,h);dest=work/f'{i:04}_{j:05}.aac'
   if enc:
    k=get_key(key);subprocess.run(['openssl','enc','-d','-aes-128-cbc','-K',k.hex(),'-iv',(iv or '0').zfill(32),'-in','-','-out',str(dest)],input=b,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
   else:dest.write_bytes(b)
   return dest
  try:workers=max(1,int(os.environ.get('NEXTORY_WORKERS','8')))
  except (TypeError,ValueError):workers=8
  with listing.open('w') as lst, ThreadPoolExecutor(max_workers=workers) as executor:
   for i,f in enumerate(files):
    playlist=f['uri'].replace('/master_playlist','/media_playlist.m3u8')
    segments=_parse(_fetch(playlist,h),playlist)
    futures=[executor.submit(download_segment,i,j,segment) for j,segment in enumerate(segments)]
    # Resolve futures in playlist order to preserve exact audio concatenation.
    for future in futures:
     dest=future.result();lst.write(f"file '{dest.as_posix()}'\n")
    ff+=['','[CHAPTER]','TIMEBASE=1/1000',f"START={f['start_at']}",f"END={f['start_at']+f['duration']}",f'title={names[i]}'];progress(f'Chapter {i+1}/{len(files)}',int((i+1)*100/max(1,len(files))),phase='downloading')
  meta.write_text('\n'.join(ff)+'\n');audio=work/'audio.aac'
  progress('Füge Audiodateien zusammen…',15,phase='processing')
  subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(listing),'-c','copy','-threads','2',str(audio)],check=True,stderr=subprocess.PIPE)
  progress('Schreibe M4B & Kapitelmarken…',65,phase='processing')
  subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(audio),'-i',str(meta),'-map','0:a:0','-map_metadata','1','-map_chapters','1','-c:a','copy','-threads','2',str(target)],check=True,stderr=subprocess.PIPE)
  progress('M4B fertiggestellt',100,phase='processing');return str(target)
 finally:shutil.rmtree(work,ignore_errors=True)
