"""Daily originals, a compact catalogue and on-demand PDF delivery; no resident loop."""
from hakimi import paths
from datetime import date,datetime
import hashlib,json,os,re,shutil,tempfile,time,uuid
from pathlib import Path
from urllib.parse import unquote
from zoneinfo import ZoneInfo

ZONE=ZoneInfo('America/New_York')

def project(base):
    base=Path(base).resolve()
    return base.parent if base.name=='official_test_runtime' else base

def folder(base):return project(base)/'state/diary'

def day_value(value=None):
    value=value or datetime.now(ZONE).date().isoformat()
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):raise ValueError('日期使用YYYY-MM-DD')
    date.fromisoformat(value)
    return value

def load(path,default=None):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError):return default

def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(tmp,path)

def entries(base,until=None,since=None):
    until=day_value(until);since=day_value(since) if since else '0001-01-01'
    if since>until:raise ValueError('开始日期晚于截止日期')
    return [row for path in sorted(folder(base).glob('????-??-??/entry.json'))
            if since<=path.parent.name<=until and (row:=load(path))]

def read_entry(base,day=None):
    if day is None:
        rows=entries(base)
        if not rows:raise ValueError('还没有日记')
        return rows[-1]
    row=load(folder(base)/day_value(day)/'entry.json')
    if not row:raise ValueError('这一天还没有日记；已有日期：'+', '.join(r['day'] for r in entries(base)[-7:]))
    return row

def rebuild(base,day):
    root=folder(base);rows=entries(base,until='9999-12-31')
    catalogue=[{'day':r['day'],'title':r['title'],'preview':r['text'].replace('\n',' ')[:160],
                'status':r['status']} for r in rows]
    target=root/'catalogue.jsonl';tmp=target.with_suffix('.tmp')
    tmp.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in catalogue),encoding='utf-8');os.replace(tmp,target)
    month=day[:7];out=root/'monthly'/f'{month}.md';out.parent.mkdir(exist_ok=True)
    out.write_text(f'# 哈基米日记 · {month}\n\n'+ '\n\n'.join(
        f"## {r['day']} · {r['title']}\n\n{r['text']}\n\n配图描述：{r['image_prompt']}" for r in rows if r['day'].startswith(month))+'\n',encoding='utf-8')

def save(base,day,title,text,image_prompt,provider):
    day=day_value(day)
    if not all(isinstance(x,str) and x.strip() for x in (title,text,image_prompt)):raise ValueError('日记字段不能为空')
    if len(title)>80 or len(text)>6000 or len(image_prompt)>400:raise ValueError('日记或配图描述过长')
    from hakimi.bridge.interaction_policy import blocked_outgoing_topic
    if blocked_outgoing_topic(title+text+image_prompt):raise ValueError('ignored_topic')
    root=folder(base)/day;root.mkdir(parents=True,exist_ok=True)
    if (root/'entry.json').exists():return read_entry(base,day)
    row={'day':day,'title':title,'text':text,'image_prompt':image_prompt,'provider':provider,
         'timezone':str(ZONE),'created_at':time.time(),'status':'awaiting_art'}
    # The original is immutable and remains byte-for-byte intact through typesetting.
    (root/'original.md').write_bytes(text.encode('utf-8'))
    write(root/'entry.json',row);rebuild(base,day)
    return row

def memory(base,query):
    """Reuse local sparse vectors, returning at most two short references, never all originals."""
    path=folder(base)/'catalogue.jsonl'
    if not path.exists():return []
    from hakimi.memory.text import terms
    from hakimi.memory.store_base import vectorize, cosine
    query_terms=terms(query)
    if not query_terms:return []
    import struct
    q=dict(struct.iter_unpack('<Hf',vectorize(query_terms)))
    with path.open('rb') as stream:
        stream.seek(max(0,path.stat().st_size-65536));lines=stream.read().splitlines()
    scored=[]
    for line in lines:
        try:row=json.loads(line)
        except ValueError:continue
        score=cosine(vectorize(terms(row['title']+' '+row['preview'])),q)
        if '日记' in query or score>=.16:scored.append((score,row['day'],row))
    return [{'day':r['day'],'title':r['title'],'excerpt':r['preview'][:100],
             'source':'哈基米自己的日记，不是群聊事实证据；diary.read可读原文'}
            for _,_,r in sorted(scored,key=lambda v:(v[0],v[1]),reverse=True)[:2]]

def archive(base,day,image):
    from PIL import Image
    from hakimi.features.diary_latex import make_tex,compile_pdf
    row=read_entry(base,day);root=folder(base)/day
    image=Path(image).resolve()
    with Image.open(image) as picture:
        if abs(picture.width/picture.height-16/9)>.05:raise ValueError('配图须为16:9横图')
        if picture.format not in ('PNG','JPEG'):raise ValueError('配图使用PNG或JPEG')
    digest=hashlib.sha256(image.read_bytes()).hexdigest()
    if any(r.get('image_sha256')==digest for r in entries(base,until='9999-12-31') if r['day']!=day):raise ValueError('不能复用旧配图')
    target=root/('image'+image.suffix.lower())
    if image!=target:shutil.copy2(image,target)
    row.update(image=str(target),image_sha256=digest)
    # Keep sources and image assets. Verification PDFs and compiler debris are temporary.
    make_tex([row],root/'latex')
    with tempfile.TemporaryDirectory(prefix='diary_verify_',dir=folder(base)) as work:
        pdf=compile_pdf([row],Path(work))
        import pymupdf
        with pymupdf.open(pdf) as doc:
            if not len(doc) or any(p.rect.width<=0 for p in doc):raise ValueError('PDF验收失败')
            qa=root/'qa';qa.mkdir(exist_ok=True)
            for i,page in enumerate(doc):page.get_pixmap(matrix=pymupdf.Matrix(1,1)).save(qa/f'page-{i+1}.png')
            row['qa_pages']=len(doc)
    row.update(status='archived',archived_at=time.time(),tex=str(root/'latex/diary.tex'))
    write(root/'entry.json',row);rebuild(base,day)
    return row

def export_spec(spec):
    if not isinstance(spec,dict) or set(spec)-{'until','since'}:raise ValueError('日记导出参数无效')
    result={'until':day_value(spec.get('until'))}
    if spec.get('since'):result['since']=day_value(spec['since'])
    if result.get('since','0001-01-01')>result['until']:raise ValueError('开始日期晚于截止日期')
    return result

def requested_export(payload,base):
    """Attach the diary to an explicit request even when the model only reads it."""
    if payload.get('internal_review') or payload.get('listening_window') or payload.get('is_proactive'):return None
    from hakimi.core.context_window import select_context
    text=str(select_context(payload)[1].get('text',''))
    if '日记' not in text or re.search(r'(?:不要|不用|不想|别).{0,6}(?:看|发|传|导出).{0,8}日记',text):return None
    if not (re.search(r'(?:想看|看看|看下|看一[下眼]|发.{0,5}我|给我.{0,5}发|导出).{0,16}日记',text)
            or re.search(r'日记.{0,10}(?:发[来我]|发给我|给我|导出|PDF|pdf)',text)):return None
    if not re.search(r'你|哈基米|猫猫|本喵|发.{0,5}我|给我|日记.{0,10}发来',text):return None
    rows=entries(base)
    ordinal=re.search(r'第([一二三四五六七八九十1-9])(?:天|篇)',text)
    if ordinal:
        n=int(ordinal[1]) if ordinal[1].isdigit() else '一二三四五六七八九十'.index(ordinal[1])+1
        return {'since':rows[n-1]['day'],'until':rows[n-1]['day']} if n<=len(rows) else None
    dates=re.findall(r'\d{4}-\d{2}-\d{2}',text)
    if dates:
        return {'until':dates[-1]} if re.search(r'截至|截止|到',text) else {'since':dates[0],'until':dates[-1]}
    if '昨天' in text:
        from datetime import timedelta
        day=(datetime.now(ZONE).date()-timedelta(days=1)).isoformat()
        return {'since':day,'until':day} if any(r['day']==day for r in rows) else None
    return {}

def submit_export(coordinator,token,provider,spec,base,segments=()):
    from hakimi.features.diary_latex import compile_pdf
    from hakimi.transport.file_delivery import directory
    if not coordinator.check_token(token,provider):return {'accepted':False,'reason':'stale_owner'}
    spec=export_spec(spec);rows=entries(base,**spec)
    if not rows:raise ValueError('指定日期范围内还没有日记')
    coordinator.record_progress(token)
    payload=coordinator.snapshot()['payload'];out=directory(payload['session_key'],base);out.mkdir(parents=True,exist_ok=True)
    name='diary_export_'+uuid.uuid4().hex+'.pdf';target=out/name
    with tempfile.TemporaryDirectory(prefix='diary_pdf_',dir=folder(base)) as work:
        pdf=compile_pdf(rows,Path(work),title='哈基米的日记 · 截至'+spec['until'])
        shutil.copyfile(pdf,target)
    coordinator.record_progress(token)
    result=coordinator.submit_reply(token,list(segments),provider,files=[{'name':name,'mode':'file'}])
    if not result['accepted']:target.unlink(missing_ok=True)
    return result

def latest_image(base,session):
    """Render only the newest diary's body and illustration into one cached image."""
    from PIL import Image
    import pymupdf
    from hakimi.features.diary_latex import compile_pdf
    from hakimi.transport.image_permissions import register_cached_image
    row=read_entry(base)
    key=hashlib.sha256(json.dumps([row['day'],row['title'],row['text'],row.get('image_sha256','')],ensure_ascii=False).encode()).hexdigest()[:16]
    cache=Path(base)/'state/img_cache';cache.mkdir(parents=True,exist_ok=True)
    target=cache/f"img_{session.replace(':','_')}_diary_{row['day']}_{key}.jpg"
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix='diary_image_',dir=folder(base)) as work:
            pdf=compile_pdf([row],Path(work),front_matter=False)
            pages=[]
            with pymupdf.open(pdf) as doc:
                for page in doc:
                    pix=page.get_pixmap(matrix=pymupdf.Matrix(1.8,1.8),alpha=False)
                    pages.append(Image.frombytes('RGB',(pix.width,pix.height),pix.samples))
            width=max(p.width for p in pages);height=sum(p.height for p in pages)+16*(len(pages)-1)
            with Image.new('RGB',(width,height),(250,247,240)) as canvas:
                y=0
                for page in pages:
                    canvas.paste(page,((width-page.width)//2,y));y+=page.height+16;page.close()
                canvas.save(target,'JPEG',quality=92)
    register_cached_image(session,target,Path(base))
    return target


def cleanup(segments,base,reply_id,status):
    """Called only on terminal delivery states; definitely-unsent queued PDFs stay available."""
    from hakimi.transport.file_delivery import SEGMENT,directory
    found=False
    for text in segments:
        match=SEGMENT.fullmatch(text)
        if not match:continue
        name=unquote(match[1])
        if not re.fullmatch(r'diary_export_[0-9a-f]{32}\.pdf',name):continue
        # Search only fixed per-session outgoing directories; never follow a path supplied by a model.
        for path in (Path(base)/'state/outgoing_files').glob('*/'+name):
            if path.resolve()==path.absolute():path.unlink(missing_ok=True)
        found=True
    if found:
        receipt={'status':status,'at':time.time()}
        if status!='delivered':
            try:
                with (Path(base)/'state/file_send_errors.jsonl').open('rb') as log:
                    log.seek(0,2);log.seek(max(0,log.tell()-8192));lines=log.read().decode('utf-8','replace').splitlines()
                for line in reversed(lines):
                    try:error=json.loads(line)
                    except ValueError:continue
                    if error.get('reply_id')==reply_id:
                        receipt.update(code=error.get('code',''),reason=error.get('detail') or error.get('code',''))
                        break
            except OSError:pass
        write(Path(base)/'state/diary_receipts'/f'{reply_id}.json',receipt)

def wait_sent(base,reply_id,timeout=360):
    path=Path(base)/'state/diary_receipts'/f'{reply_id}.json';deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        row=load(path)
        if row:
            if row['status']=='delivered':return
            raise ValueError('日记未确认送达：'+(row.get('reason') or row['status']))
        time.sleep(.5)
    raise ValueError('发送结果待确认，请勿重复提交')

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['pending','read','archive'])
    parser.add_argument('--day');parser.add_argument('--image');args=parser.parse_args();base=paths.base()
    if args.action=='pending':result=[{k:r[k] for k in ('day','title','image_prompt','provider')} for r in entries(base) if r['status']!='archived']
    elif args.action=='read':result=read_entry(base,day_value(args.day))
    else:result=archive(base,day_value(args.day),args.image)
    print(json.dumps(result,ensure_ascii=False))
