import os, re, json, shutil, subprocess, tempfile, requests
from pathlib import Path
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor

BASE='https://api.nextory.com'

def _plain(x):
 if isinstance(x,dict):
  if 'data' in x and isinstance(x['data'],dict):return x['data']
  return x
 return {}
def _fetch(url,h):
 r=requests.get(url,headers=h,timeout=60);r.raise_for_status();return r.content
def _parse(text,base):
 key=None;iv=None
 for line in text.splitlines():
  line=line.strip()
  if line.startswith('#EXT-X-KEY:'):
   m=re.search(r'URI="([^"]+)"',line);key=urljoin(base,m.group(1)) if m else None
   m=re.search(r'IV=0x([0-9a-fA-F]+)',line);iv=m.group(1) if m else None
  elif line and not line.startswith('#'):yield urljoin(base,line),key,iv

def _title(x):return re.sub(r'[\\/:*?"<>|]+','_',str(x or 'Untitled')).strip() or 'Untitled'

def get_book(book_id,token):
 h={'Authorization':f'Bearer {token}','Accept':'application/json'}
 for u in (f'{BASE}/v1/books/{book_id}',f'{BASE}/books/{book_id}'):
  r=requests.get(u,headers=h,timeout=30)
  if r.ok:
   d=_plain(r.json());d['cover_url']=next((f.get('img_url') for f in d.get('formats',[]) if f.get('img_url')),'');return d
 r.raise_for_status()

def download_book(book_id,token,output_dir,progress=lambda *a,**k:None):
 h={'Authorization':f'Bearer {token}','Accept':'application/json'};p=get_book(book_id,token);formats=p.get('formats') or []
 if not formats:raise ValueError('No audio formats found')
 f=formats[0];source=f.get('url') or f.get('download_url') or f.get('manifest_url')
 if not source:raise ValueError('No audio manifest found')
 if source.startswith('/'):source=BASE+source
 r=requests.get(source,headers=h,timeout=30);r.raise_for_status();text=r.text
 files=[];duration=0
 for line in text.splitlines():
  if line.startswith('#EXTINF:'):
   try:duration=float(line.split(':',1)[1].split(',',1)[0])
   except Exception:duration=0
  elif line and not line.startswith('#'):
   files.append({'uri':urljoin(source,line),'duration':int(duration*1000),'start_at':sum(x['duration'] for x in files)})
 if not files:raise ValueError('No audio segments found')
 title=_title(p.get('title') or p.get('name') or book_id);out=Path(output_dir);out.mkdir(parents=True,exist_ok=True);target=out/f'{title}.m4b'
 work=Path(tempfile.mkdtemp(prefix='nextory_'));listing=work/'files.txt';meta=work/'metadata.txt';ff=[';FFMETADATA1']
 for k,v in [('title',p.get('title') or title),('artist',p.get('author') or p.get('artist') or ''),('album',p.get('title') or title)]:
  if v:ff.append(f'{k}={str(v).replace("\\\\", "\\\\\\\\").replace("=", "\\\\=").replace(";", "\\\\;").replace("#", "\\\\#").replace("\n", " ")}')
 names=[x.get('title') or f'Chapter {i+1}' for i,x in enumerate(files)]
 def download_segment(i,j,segment):
  url,key,iv=segment;b=_fetch(url,h)
  if key:
   k=_fetch(key,h);dest=work/f'{i:04}_{j:05}.aac';subprocess.run(['openssl','enc','-d','-aes-128-cbc','-K',k.hex(),'-iv',(iv or '0').zfill(32),'-in','-','-out',str(dest)],input=b,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
  else:dest=work/f'{i:04}_{j:05}.aac';dest.write_bytes(b)
  return dest
 try:
  with listing.open('w') as lst, ThreadPoolExecutor(max_workers=max(1,int(os.environ.get('NEXTORY_WORKERS','8')))) as executor:
   for i,f in enumerate(files):
    playlist=f['uri'].replace('/master_playlist','/media_playlist.m3u8');segments=_parse(requests.get(playlist,headers=h,timeout=60).text,playlist);futures=[executor.submit(download_segment,i,j,segment) for j,segment in enumerate(segments)]
    for future in futures:
     dest=future.result();lst.write(f"file '{dest.as_posix()}'\n")
    ff+=['','[CHAPTER]','TIMEBASE=1/1000',f"START={f['start_at']}",f"END={f['start_at']+f['duration']}",f'title={names[i]}'];progress(f'Chapter {i+1}/{len(files)}',int((i+1)*85/max(1,len(files))))
  meta.write_text('\n'.join(ff)+'\n')
  cover_url=data.get('cover_url') or next((x.get('img_url') for x in p.get('formats',[]) if x.get('img_url')),'')
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
  last_status=0.0;last_status_pct=15;stats={}
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
