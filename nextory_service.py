"""Nextory account and audiobook integration service."""
import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from enum import Enum
from urllib.parse import urljoin

from nextory.client import NextoryClient
from nextory.models import LibraryListType

try:
 import requests
 from requests.adapters import HTTPAdapter
 _HTTP_SESSION=requests.Session()
 _HTTP_SESSION.mount('https://',HTTPAdapter(pool_connections=16,pool_maxsize=16))
 _HTTP_SESSION.mount('http://',HTTPAdapter(pool_connections=16,pool_maxsize=16))
except ImportError:
 _HTTP_SESSION=None

CONFIG=Path(os.environ.get('NEXTORY_CONFIG',Path.home()/'.config/phantom-rail/nextory.json'))

def _load():
 try:return json.loads(CONFIG.read_text())
 except (OSError,ValueError):return {}
def _save(d):
 CONFIG.parent.mkdir(parents=True,exist_ok=True);CONFIG.write_text(json.dumps(d,ensure_ascii=False,indent=2))
def _plain(v):
 if isinstance(v,dict):return {k:_plain(x) for k,x in v.items()}
 if isinstance(v,list):return [_plain(x) for x in v]
 if isinstance(v,Enum):return v.value
 if hasattr(v,'isoformat'):return v.isoformat()
 return v
def _client(c):
 def updated(a,b):
  c.update(a);c.update(b);_save(c)
 return NextoryClient(username=c.get('username'),password=c.get('password'),country=c.get('country','DE'),login_token=c.get('login_token'),login_key=c.get('login_key'),on_auth_updated=updated)
def _run(f):return asyncio.run(f)
def status():
 c=_load();return {'authenticated':bool(c.get('login_token')),'username':c.get('username'),'country':c.get('country','DE'),'profiles':c.get('profiles',[]),'login_key':c.get('login_key')}
def login(username,password,country='DE'):
 c={'username':username,'password':password,'country':country.upper()}
 async def go():
  async with _client(c) as x:
   result=await x.login();return result
 profiles=_run(go());c['profiles']=_plain(profiles);_save(c);return status()
def select_profile(login_key):
 c=_load()
 async def go():
  async with _client(c) as x:
   await x.select_profile(login_key)
 _run(go());c['login_key']=login_key;_save(c);return status()
def logout():
 c=_load()
 if c.get('login_token'):
  async def go():
   async with _client(c) as x:await x.logout()
  _run(go())
 CONFIG.unlink(missing_ok=True)
def _auth():
 c=_load()
 if not c.get('login_token'):raise ValueError('Not logged in to Nextory')
 return c
def _product(p):
 d=_plain(p);d['cover_url']=next((f.get('img_url') for f in d.get('formats',[]) if f.get('img_url')),'');return d
def search(phrase):
 c=_auth()
 async def go():
  async with _client(c) as x:return await x.search(phrase)
 return {'products':[_product(x) for x in _run(go())]}
def library():
 c=_auth()
 async def go():
  async with _client(c) as x:
   lists=(await x.get_libraries()).product_lists;out=[]
   for typ in (LibraryListType.DOWNLOADED,LibraryListType.PURCHASED,LibraryListType.FAVORITES):
    item=next((z for z in lists if z.type==typ),None)
    if item:out.extend(item.products)
   return out
 return {'products':[_product(x) for x in _run(go())]}
def _details(book_id):
 c=_auth()
 async def go():
  async with _client(c) as x:
   p=await x.get_product(book_id);f=next((z for z in p.formats if getattr(z,'type',None) and 'hls' in str(z.type).lower()),None)
   if f is None:raise ValueError('No HLS audio format')
   return p,f
 return _run(go())
def book(book_id):
 p,_=_details(book_id);return _product(p)
def _fetch(url,h):
 if _HTTP_SESSION is not None:
  r=_HTTP_SESSION.get(url,headers=h,timeout=60);r.raise_for_status();return r.content
 r=requests.get(url,headers=h,timeout=60);r.raise_for_status();return r.content
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
    m=re.search(r'URI="([^"]+)"',line);key=urljoin(base,m.group(1)) if m else None
    m=re.search(r'IV=0x([0-9a-fA-F]+)',line);iv=m.group(1) if m else None
  elif line and not line.startswith('#'):
   out.append((urljoin(base,line),enc,key,iv));seq+=1
 return out
def download(data,folder,progress):
 c=_auth();p,f=_details(data['id']);h={'Authorization':f"Bearer {c.get('login_token')}",'Accept':'*/*'}
 playlist=getattr(f,'url',None) or getattr(f,'manifest_url',None)
 if not playlist:raise ValueError('No playlist URL')
 raw=_fetch(playlist,h);text=raw.decode('utf8','replace');files=[]
 if '#EXTINF' in text:
  duration=0
  for line in text.splitlines():
   if line.startswith('#EXTINF:'):
    try:duration=float(line.split(':',1)[1].split(',',1)[0])
    except ValueError:duration=0
   elif line and not line.startswith('#'):
    files.append({'uri':urljoin(playlist,line),'duration':int(duration*1000),'start_at':sum(x['duration'] for x in files)})
 if not files:raise ValueError('No audio segments found')
 title=re.sub(r'[\\/:*?"<>|]+','_',str(getattr(p,'title',None) or data.get('title') or data['id'])).strip() or 'Untitled';target=Path(folder)/f'{title}.m4b';target.parent.mkdir(parents=True,exist_ok=True)
 work=Path(tempfile.mkdtemp(prefix='nextory_'));listing=work/'files.txt';meta=work/'metadata.txt';ff=[';FFMETADATA1']
 for k,v in [('title',getattr(p,'title',None) or title),('artist',getattr(p,'author',None) or ''),('album',getattr(p,'title',None) or title)]:
  if v:ff.append(f'{k}={str(v).replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#").replace("\n", " ")}')
 names=[f'Chapter {i+1}' for i in range(len(files))]
 try:
  key_cache={};key_cache_lock=threading.Lock()
  def get_key(url):
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
    playlist=f['uri'].replace('/master_playlist','/media_playlist.m3u8');segments=_parse(_fetch(playlist,h),playlist);futures=[executor.submit(download_segment,i,j,segment) for j,segment in enumerate(segments)]
    for future in futures:
     dest=future.result();lst.write(f"file '{dest.as_posix()}'\n")
    ff+=['','[CHAPTER]','TIMEBASE=1/1000',f"START={f['start_at']}",f"END={f['start_at']+f['duration']}",f'title={names[i]}'];progress(f'Chapter {i+1}/{len(files)}',int((i+1)*100/max(1,len(files))),phase='downloading')
  meta.write_text('\n'.join(ff)+'\n')
  # Optional artwork is a distinct input. Concat audio, global metadata, and
  # chapters are mapped together directly to the M4B in one FFmpeg run.
  cover_url=data.get('cover_url') or next((x.get('img_url') for x in getattr(p,'formats',[]) if x.get('img_url')),'')
  cover_path=None
  if cover_url:
   try:
    cover_path=work/'cover';cover_path.write_bytes(_fetch(cover_url,h))
   except Exception:cover_path=None
  duration_ms=max((int(x.get('start_at',0) or 0)+int(x.get('duration',0) or 0) for x in files),default=0)
  progress('Muxe Audio & Kapitel in einer FFmpeg-Passage…',15,phase='processing')
  cmd=['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(listing),'-i',str(meta)]
  if cover_path:cmd+=['-i',str(cover_path)]
  cmd+=['-map','0:a:0','-map_metadata','1','-map_chapters','1']
  if cover_path:cmd+=['-map','2:v:0','-disposition:v:0','attached_pic','-c:v','mjpeg']
  cmd+=['-c:a','copy','-bsf:a','aac_adtstoasc','-movflags','+faststart','-threads','0','-progress','pipe:1','-nostats',str(target)]
  proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',bufsize=1)
  last_status=0.0;last_status_pct=15;last_percent=15;stats={}
  for line in iter(proc.stdout.readline,''):
   if '=' not in line:continue
   key,value=line.strip().split('=',1);stats[key]=value
   if key not in ('out_time_us','out_time_ms','speed','total_size','progress'):continue
   if key=='progress' and value=='end':last_status=1.0
   if key in ('out_time_us','out_time_ms'):
    try:last_status=min(1.0,int(value)/1000000/max(1,duration_ms/1000))
    except Exception:pass
   pct=15+int(last_status*80)
   if pct>=last_status_pct+2:progress('Erzeuge M4B…',pct,phase='processing');last_status_pct=pct
  err=proc.stderr.read();code=proc.wait()
  if code:raise RuntimeError(err or f'ffmpeg exited {code}')
  progress('M4B fertiggestellt',100,phase='processing');return str(target)
 finally:shutil.rmtree(work,ignore_errors=True)
