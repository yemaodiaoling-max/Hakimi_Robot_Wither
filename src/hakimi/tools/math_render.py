"""On-demand local Markdown/TeX -> paginated PNGs. No network or resident process."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import uuid

from hakimi.core.tool_log import log_tool_call  # noqa: F401  (historical import location)
from hakimi.chat.reply_format import text_bubbles

WIDTH = 1100
DPI = 144
LITERAL_FONT='Cambria Math' if os.name=='nt' else 'DejaVu Serif'


_MATH_RUN=r"[A-Za-z0-9\u0370-\u03ff\u2070-\u209f\\{}()\[\]_^+\-−*/·×÷.,'′″]"
_RELATION=re.compile(
    r"(?<![A-Za-z0-9\u0370-\u03ff\u2070-\u209f])"+_MATH_RUN+r"+(?:[ \t]+"+_MATH_RUN+r"+)*"
    r"\s*(?:=|≈|≤|≥|≠|∝|→)\s*"+_MATH_RUN+r"+(?:[ \t]+"+_MATH_RUN+r"+)*")
_TEX=re.compile(r'\\[A-Za-z]{2,}(?:\s*\{[^{}\n]*\})*')
_SCRIPT=re.compile(
    r"(?<![A-Za-z0-9\u0370-\u03ff\u2070-\u209f])[A-Za-z\u0370-\u03ff][A-Za-z0-9\u0370-\u03ff]*"
    r"(?:\^\{[^{}\n]+\}|\^[A-Za-z0-9\u0370-\u03ff]|_\{[^{}\n]+\}|_[0-9\u0370-\u03ff])"
    r"[A-Za-z0-9\u0370-\u03ff\u2070-\u209f]*")
_UNICODE_SCRIPT=re.compile(
    r"(?<![A-Za-z0-9\u0370-\u03ff\u2070-\u209f])"
    r"[A-Za-z\u0370-\u03ff][A-Za-z0-9\u0370-\u03ff]*[\u2070-\u209f]+[A-Za-z0-9\u0370-\u03ff]*")
_GREEK_OP=re.compile(
    r"(?<![A-Za-z0-9\u0370-\u03ff])(?:[A-Za-z0-9\u0370-\u03ff]*[\u0370-\u03ff][A-Za-z0-9\u0370-\u03ff]*)"
    r"(?:\s*[+\-−*/·×÷]\s*[A-Za-z0-9\u0370-\u03ff]+)+")
_ARITH=re.compile(r'(?<![\d.])\d+(?:\.\d+)?(?:\s*[+*/×÷·]\s*\d+(?:\.\d+)?)+(?![\d.])')
_EDGE_PUNCT=" \t\r\n,.;:!?，。；：！？、"


def _relation_is_math(value):
    left,right=re.split(r'[=≈≤≥≠∝→]',value,maxsplit=1)
    if not left.strip() or not right.strip():return False
    if re.search(r'[\u0370-\u03ff\u2070-\u209f\\{}()\[\]^_*/×÷·]',value):return True
    if re.search(r'(?<![A-Za-z0-9])[A-Za-z](?![A-Za-z0-9])',value):return True
    if re.search(r'\d',value) and re.search(r'[+\-−*/×÷·^]',value):return True
    return bool(re.fullmatch(r'[\d\s.,+\-−*/×÷·=≈≤≥≠∝→]+',value))


def _bare_math_spans(value):
    spans=[]
    for pattern,check in ((_RELATION,_relation_is_math),(_TEX,None),(_SCRIPT,None),
                          (_UNICODE_SCRIPT,None),(_GREEK_OP,None),(_ARITH,None)):
        for match in pattern.finditer(value):
            start,end=match.span()
            while start<end and value[start] in _EDGE_PUNCT:start+=1
            while end>start and value[end-1] in _EDGE_PUNCT:end-=1
            if start>=end:continue
            if check and not check(value[start:end]):continue
            spans.append((start,end))
    spans.sort()
    merged=[]
    for start,end in spans:
        if merged and start<merged[-1][1]:merged[-1]=(merged[-1][0],max(merged[-1][1],end))
        else:merged.append((start,end))
    return merged


def _prepare_bare_text(text):
    """Find explicit or clear bare math before QQ splitting can consume modulus bars."""
    if not isinstance(text,str):return None
    opaque=r'```[^\n]*\n.*?```|~~~[^\n]*\n.*?~~~|`[^`\n]+`|(?m:^(?: {4}|\t)[^\n]*(?:\n|$))|https?://[^\s<>]+|\[CQ:[^\]]*\]'
    marked=r'(?<!\\)\$\$(.*?)\$\$|(?<![\\$])\$(?!\$)([^\n$]+?)(?<!\\)\$|\\\[(.*?)\\\]|\\\((.*?)\\\)'
    pattern=re.compile('(?P<opaque>'+opaque+')|(?P<marked>'+marked+')',re.S)
    originals={};math_keys=set();prefix='QM'+uuid.uuid4().hex
    def protect(match):
        raw=match.group();key=prefix+str(len(originals))+'Z';originals[key]=raw
        is_math=bool(match.group('marked')) or bool(re.match(r'(?:```|~~~)(?:math|latex|tex)\s*\n',raw))
        # Two dollar prices in one sentence are not a TeX inline equation.
        if (raw.startswith('$') and not raw.startswith('$$')
                and re.match(r'^\d+(?:\.\d+)?\s+\S',raw[1:-1].strip())
                and not _bare_math_spans(raw[1:-1])):
            is_math=False
        if is_math:math_keys.add(key)
        return key
    protected=pattern.sub(protect,text)
    # URLs, CQ codes, dates, times, versions and prices are ordinary data, even
    # when they happen to contain '=' or arithmetic-looking fragments.
    plain=re.compile(r'https?://[^\s<>\]\)]+|\[CQ:[^\]]*\]|(?<!\d)\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?!\d)'
                     r'|(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)|(?<!\d)\d+\.\d+\.\d+(?!\d)'
                     r'|[$￥¥]\s?\d+(?:\.\d+)?(?:\s*/\s*(?:月|年|天|小时|次))?'
                     r'|(?<!\d)\d+(?:\.\d+)?\s*(?:元|块|美元|USD|CNY|RMB|%|％|折)')
    def protect_plain(match):
        raw=match.group();key=prefix+str(len(originals))+'Z';originals[key]=raw;return key
    protected=plain.sub(protect_plain,protected)
    key_pattern=re.compile('('+re.escape(prefix)+r'\d+Z)')
    def protect_bare(chunk):
        spans=_bare_math_spans(chunk)
        if not spans:return chunk
        output=[];cursor=0
        for start,end in spans:
            raw=chunk[start:end];key=prefix+str(len(originals))+'Z'
            originals[key]=raw;math_keys.add(key)
            output.append(chunk[cursor:start]);output.append(key);cursor=end
        output.append(chunk[cursor:])
        return ''.join(output)
    pieces=[];cursor=0
    for match in key_pattern.finditer(protected):
        pieces.append(protect_bare(protected[cursor:match.start()]))
        pieces.append(match.group());cursor=match.end()
    pieces.append(protect_bare(protected[cursor:]))
    protected=''.join(pieces)
    if not math_keys:return None
    matches=list(key_pattern.finditer(protected))
    first=next(match for match in matches if match.group() in math_keys)
    last=next(match for match in reversed(matches) if match.group() in math_keys)
    def restore(value):
        for key,raw in originals.items():value=value.replace(key,raw)
        return value.strip()
    start,end=first.start(),last.end()
    # Keep explanatory conditions attached to the formula rather than emitting
    # fragments such as "当" / "时相消" as separate chat bubbles.
    tail=protected[end:]
    if re.match(r'^\s*(?:时|则|后|成立|可得|说明|相消|对应|为|分别|，|；)',tail):
        stop=re.search(r'[。！？!?\n]',tail)
        end += stop.end() if stop else len(tail)
    head=protected[:start]
    paragraph=max(head.rfind('\n'),head.rfind('。'),head.rfind('！'),head.rfind('？'))+1
    if re.search(r'当|若|设|对于|因为|其中|基元|恢复力',head[paragraph:]):
        start=paragraph
    return {'markdown':restore(protected[start:end]),
            'before':restore(protected[:start]),'after':restore(protected[end:])}


def prepare_text(text, *, bare=False):
    if bare:
        return _prepare_bare_text(text)
    if not isinstance(text,str):return None
    pattern=re.compile(r'```[^\n]*\n.*?```|`[^`\n]+`|(?<!\\)\$\$(.*?)\$\$|(?<![\\$])\$(?!\$)([^\n$]+?)(?<!\\)\$|\\\[(.*?)\\\]|\\\((.*?)\\\)',re.S)
    originals={};math_keys=set();prefix='QM'+uuid.uuid4().hex
    def protect(match):
        raw=match.group();key=prefix+str(len(originals))+'Z';originals[key]=raw
        if not raw.startswith('`') or re.match(r'```(?:math|latex|tex)\s*\n',raw):math_keys.add(key)
        return key
    protected=pattern.sub(protect,text)
    if not math_keys:return None
    parts=[p.strip() for p in re.split(r'\n+|\||(?<=[。！？!?])',protected) if p.strip()]
    indexes=[i for i,p in enumerate(parts) if any(key in p for key in math_keys)]
    def restore(chunks,separator):
        value=separator.join(chunks)
        for key,raw in originals.items():value=value.replace(key,raw)
        return value
    first,last=indexes[0],indexes[-1]
    return {'markdown':restore(parts[first:last+1],'\n\n'),
            'before':restore(parts[:first],'\n'),'after':restore(parts[last+1:],'\n')}


def submit_text(coordinator,token,provider,text,base,*,extra=(),files=None,reply_to=None,interaction=None):
    parts=prepare_text(text)
    if not parts:raise ValueError('没有可排版的公式')
    log_tool_call(base,'math.send','auto_route',provider=provider)
    return submit(coordinator,token,provider,{'markdown':parts['markdown']},base,
                  prefix=text_bubbles(parts['before']),suffix=text_bubbles(parts['after'])+list(extra),
                  files=files,reply_to=reply_to,interaction=interaction)


def validate(spec):
    if not isinstance(spec, dict) or set(spec)-{'markdown','font_size','page_height','before','after'}:
        raise ValueError('公式排版参数无效')
    result = dict(spec)
    body = result.get('markdown')
    if not isinstance(body,str) or not 1 <= len(body.strip()) <= 24000:
        raise ValueError('Markdown正文须为1至24000字')
    for key, default, low, high in [('font_size',16,12,22),('page_height',1600,900,2400)]:
        value=result.get(key,default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{key}须为{low}至{high}的整数')
        result[key]=value
    for key in ('before','after'):
        if not isinstance(result.get(key,''),str) or len(result.get(key,''))>1000:
            raise ValueError('图前图后的短话各不超过1000字')
    return result


def escape(text):
    chars={'\\':r'\textbackslash{}','{':r'\{','}':r'\}','$':r'\$',
           '&':r'\&','#':r'\#','%':r'\%','_':r'\_','^':r'\textasciicircum{}','~':r'\textasciitilde{}'}
    return ''.join(chars.get(c,c) for c in text)


def prefer_display(math):
    # Single symbols/indices belong to the sentence; equations and operations
    # get their own line. This affects typesetting only, never the source MD.
    atom=r'(?:[A-Za-z\u0370-\u03ff]|\\[A-Za-z]+)'
    index=r'(?:[_^](?:[A-Za-z0-9]|\{(?:[A-Za-z0-9]|\\[A-Za-z]+){1,6}\}))'
    return not re.fullmatch(r'(?:'+atom+index+r'{0,2}|[+-]?\d+(?:\.\d+)?)',re.sub(r'\s+','',math))


def markdown_tex(markdown):
    from markdown_it import MarkdownIt
    # Math is the only raw TeX input. Restrict it to math commands; ordinary
    # Markdown is always escaped, and the compiler also has shell escape off.
    equations=[]
    prefix='QZMATH'+uuid.uuid4().hex.upper()
    pattern=re.compile(r'```[^\n]*\n.*?```|`[^`\n]+`|(?<!\\)\$\$(.*?)\$\$|(?<![\\$])\$(?!\$)([^\n$]+?)(?<!\\)\$|\\\[(.*?)\\\]|\\\((.*?)\\\)',re.S)
    def protect(match):
        raw=match.group()
        if raw.startswith('`'):
            fence=re.fullmatch(r'```(?:math|latex|tex)\s*\n(.*?)```',raw,re.S)
            if not fence:return raw
            math,display=fence[1],True
        else:
            math=next(group for group in match.groups() if group is not None)
            display=raw.startswith(('$$',r'\['))
        commands=re.findall(r'\\([A-Za-z]+|.)',math)
        blocked={'input','include','includegraphics','openin','openout','read','readline','write','write18',
                 'catcode','csname','endcsname','def','edef','gdef','xdef','let','futurelet','loop','repeat',
                 'special','usepackage','documentclass','directlua','pdfobj','pdffiledump','scantokens',
                 'newcommand','renewcommand','newenvironment','everyjob','everymath','everydisplay'}
        if any(command.lower() in blocked for command in commands) or '^^' in math:
            raise ValueError('公式含非排版命令')
        if any(env not in {'aligned','alignedat','gathered','split','cases','matrix','pmatrix','bmatrix','Bmatrix',
                          'vmatrix','Vmatrix','smallmatrix','array','align','align*','gather','gather*'}
               for env in re.findall(r'\\(?:begin|end)\s*\{([^}]+)\}',math)):
            raise ValueError('公式环境不支持')
        key=prefix+str(len(equations))+'Z'
        equations.append((math,display,False,raw))
        return key
    protected=pattern.sub(protect,markdown.replace('\r\n','\n'))
    # Older replies can contain Unicode equations without $ delimiters. Move
    # clear relations to their own line without interpreting/recalculating them.
    raw_pattern=re.compile(r'```[^\n]*\n.*?```|`[^`\n]+`|https?://[^\s<>\]]+|[+\-−]?[A-Za-z\u0370-\u03ff][A-Za-z0-9\u0370-\u03ff\u2070-\u209f ℏ∫∑∂∇√∞²³½_{}()\[\]^+*/=<>!|·×−∝≈≤≥±→.,\'\-\t]*',re.S)
    def literal(match):
        raw=match.group()
        if (raw.startswith(('`','http://','https://')) or prefix in raw or not re.search(r'=|≈|≤|≥|∝',raw)
                or any(len(word)>8 for word in re.findall(r'[A-Za-z]+',raw))):
            return raw
        end=len(raw.rstrip(' \t.,'))
        key=prefix+str(len(equations))+'Z'
        equations.append((raw[:end],True,True,raw[:end]))
        return key+raw[end:]
    protected=raw_pattern.sub(literal,protected)
    math_pattern=re.compile('('+prefix+r'(\d+)Z)')
    def text(value,allow_display):
        output=[]
        cursor=0
        for match in math_pattern.finditer(value):
            output.append(escape(value[cursor:match.start()]))
            math,explicit,literal,_=equations[int(match[2])]
            display=allow_display and (explicit or prefer_display(math))
            cursor=match.end()
            punctuation=re.match(r'[，。；：！？,;:!?]+|\.(?!\d)',value[cursor:]) if display else None
            suffix=r'\text{'+escape(punctuation[0])+'}' if punctuation else ''
            if punctuation:cursor+=len(punctuation[0])
            aligned=re.fullmatch(r'\s*\\begin\{aligned\}(.*?)\\end\{aligned\}\s*',math,re.S) if display else None
            if literal:
                content=r'{\fontspec{'+LITERAL_FONT+'}'+escape(math)+(escape(punctuation[0]) if punctuation else '')+'}'
                output.append(r'\begin{center}'+content+r'\end{center}' if display else content)
            elif aligned:
                output.append(r'\begin{align*}'+aligned[1]+suffix+r'\end{align*}')
            elif display and re.match(r'\s*\\begin\{(?:align\*?|gather\*?)\}',math):
                output.append(re.sub(r'(\\end\{[^}]+\}\s*)$',lambda m:suffix+m[1],math))
            elif display:output.append(r'\['+math+suffix+r'\]')
            else:output.append('$'+(r'\displaystyle ' if prefer_display(math) else '')+math+'$')
        output.append(escape(value[cursor:]))
        return ''.join(output)
    def code_text(value):
        return escape(math_pattern.sub(lambda m:equations[int(m[2])][3],value))
    def inline(children,allow_display=True):
        codes={'strong_open':r'{\bfseries ','strong_close':'}','em_open':r'{\itshape ','em_close':'}',
               's_open':r'\sout{','s_close':'}','softbreak':' ','hardbreak':r'\\ '}
        return ''.join(text(t.content,allow_display) if t.type=='text' else
                       r'{\ttfamily '+code_text(t.content)+'}' if t.type=='code_inline' else
                       escape(t.content) if t.type=='image' else codes.get(t.type,'') for t in children or [])
    tokens=MarkdownIt('commonmark',{'html':False}).enable('table').parse(protected)
    output=[]
    mapping={'paragraph_open':'','paragraph_close':'\n\\par\\medskip\n',
             'bullet_list_open':r'\begin{itemize}','bullet_list_close':r'\end{itemize}',
             'ordered_list_open':r'\begin{enumerate}','ordered_list_close':r'\end{enumerate}',
             'list_item_open':r'\item ','list_item_close':'\n',
             'blockquote_open':r'\begin{quote}','blockquote_close':r'\end{quote}',
             'hr':r'\par\noindent\rule{\linewidth}{.4pt}\par',
             'table_close':r'\end{xltabular}\par\medskip','tr_close':r'\\ ', 'thead_close':r'\endhead ',
             'th_close':' & ','td_close':' & '}
    for i,token in enumerate(tokens):
        if token.type=='inline':
            output.append(inline(token.children,tokens[i-1].type not in {'th_open','td_open','heading_open'}))
        elif token.type=='heading_open':output.append(r'\par\medskip{\bfseries ')
        elif token.type=='heading_close':output.append('}\\par\\medskip\n')
        elif token.type in {'fence','code_block'}:
            output.append(r'\par{\ttfamily '+r'\\ '.join(code_text(line) for line in token.content.splitlines())+r'}\par')
        elif token.type=='table_open':
            end=next(j for j in range(i+1,len(tokens)) if tokens[j].type=='tr_close')
            columns=sum(t.type=='th_open' for t in tokens[i:end])
            output.append(r'\par\begin{xltabular}{\linewidth}{'+('X'*columns)+'}')
        elif token.type in {'th_close','td_close'} and tokens[i+1].type=='tr_close':pass
        else:output.append(mapping.get(token.type,''))
    return '\n'.join(output)


def render(spec,session,base):
    import pymupdf as fitz
    from hakimi.chat.file_delivery import directory, resolve
    from urllib.parse import quote
    spec=validate(spec)
    from hakimi.chat.interaction_policy import blocked_outgoing_topic
    if blocked_outgoing_topic(spec['markdown']):raise ValueError('ignored_topic')
    compiler=shutil.which('xelatex')
    if not compiler:raise ValueError('本机缺少XeLaTeX')
    body=markdown_tex(spec['markdown'])
    root=directory(session,base)
    root.mkdir(parents=True,exist_ok=True)
    for path in root.glob('_math_render_*'):
        if path.is_file() and time.time()-path.stat().st_mtime>7*86400:path.unlink()
    name='_math_render_'+uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='math_',dir=Path(base)/'state') as work:
        work=Path(work)
        sizes=list(range(spec['font_size'],11,-2))
        if sizes[-1]!=12:sizes.append(12)
        for size in sizes:
            fontset='windows' if os.name=='nt' else 'fandol'
            tex=(rf'\documentclass[UTF8,fontset={fontset}]{{ctexart}}'+'\n'
                 r'\usepackage{amsmath,amssymb,geometry,xltabular,enumitem}'+'\n'
                 rf'\geometry{{paperwidth={WIDTH/2}bp,paperheight={spec["page_height"]/2}bp,margin=24pt}}'+'\n'
                 r'\pagestyle{empty}\setlength{\parindent}{0pt}\setlist{nosep,leftmargin=1.4em}'+'\n'
                 r'\allowdisplaybreaks[4]\raggedbottom\sloppy'+'\n'
                 r'\begin{document}'+rf'\fontsize{{{size}}}{{{size*1.6}}}\selectfont'+'\n'
                 r'\abovedisplayskip=8pt\belowdisplayskip=8pt\abovedisplayshortskip=6pt\belowdisplayshortskip=6pt'+'\n'
                 +body+'\n'+r'\end{document}')
            (work/'page.tex').write_text(tex,encoding='utf-8')
            env=dict(os.environ,openin_any='p',openout_any='p')
            try:
                result=subprocess.run([compiler,'-no-shell-escape','-interaction=nonstopmode','-halt-on-error',
                                       '-file-line-error','page.tex'],cwd=work,env=env,stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT,timeout=35,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            except subprocess.TimeoutExpired as exc:raise ValueError('公式编译超时') from exc
            log=result.stdout.decode('utf-8',errors='replace')
            if result.returncode:
                error=next((line for line in log.splitlines() if 'page.tex:' in line or line.startswith('!')),'公式语法错误')
                raise ValueError(error[:180])
            overflow=max([float(v) for v in re.findall(r'Overfull \\[hv]box \(([\d.]+)pt too',log)],default=0)
            if overflow<=2:break
        if overflow>2:raise ValueError('公式过宽或过高，请分段或用aligned分行；未缩成小字图片')
        segments=[]
        with fitz.open(work/'page.pdf') as doc:
            for i,page in enumerate(doc):
                blocks=page.get_text('blocks')
                bottom=max((b[3] for b in blocks),default=80)
                clip=fitz.Rect(0,0,page.rect.width,min(page.rect.height,max(100,bottom+24)))
                path=root/f'{name}_{i+1:02}.png'
                page.get_pixmap(dpi=DPI,clip=clip,alpha=False).save(path)
                _,_,stamp=resolve(path.name,session,base)
                segments.append(f'[CQ:file,name={quote(path.name,safe="")},stamp={stamp},mode=auto]')
    (root/f'{name}.md').write_text(spec['markdown'],encoding='utf-8')
    return text_bubbles(spec.get('before',''))+segments+text_bubbles(spec.get('after',''))


def _audit_submit(base,status,provider,trigger_id=None,error_type=None):
    fields={'provider':provider}
    if trigger_id is not None:fields['trigger_id']=trigger_id
    if error_type is not None:fields['error_type']=error_type
    log_tool_call(base,'math.submit',status,**fields)


def submit(coordinator,token,provider,spec,base,*,prefix=(),suffix=(),reply_to=None,files=None,interaction=None):
    trigger_id=None
    try:trigger_id=coordinator.snapshot().get('trigger_id')
    except Exception:pass
    if not coordinator.check_token(token,provider):
        _audit_submit(base,'stale',provider,trigger_id)
        return {'accepted':False,'reason':'stale_owner'}
    try:
        state=coordinator.snapshot()
        coordinator.record_progress(token)
        segments=list(prefix)+render(spec,state['payload']['session_key'],base)+list(suffix)
        result=coordinator.submit_reply(token,segments,provider,reply_to=reply_to,files=files,rendered_math=True,interaction=interaction)
        from hakimi.core.runtime_io import read_json_cached
        policy=read_json_cached(Path(base)/'config.json',{}).get('delivery_policy',{})
        result['wait_seconds']=max(policy.get('max_queue_age_seconds',600),
            len(segments)*(policy.get('session_min_interval_seconds',15)+policy.get('ack_timeout_seconds',12))+90)+30
    except Exception as exc:
        _audit_submit(base,'error',provider,trigger_id,type(exc).__name__)
        raise
    if result.get('accepted'):_audit_submit(base,'success',provider,trigger_id)
    elif result.get('reason')=='stale_owner':_audit_submit(base,'stale',provider,trigger_id)
    else:_audit_submit(base,'error',provider,trigger_id,'rejected')
    return result


def wait_sent(base,reply_id,timeout=660):
    path=Path(base)/'state/math_receipts'/f'{reply_id}.json'
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        if path.exists():
            receipt=json.loads(path.read_text(encoding='utf-8'))
            if receipt['status']=='delivered':return
            raise ValueError('公式图片未确认发出：'+receipt['status']+'；不要重复发送')
        time.sleep(.5)
    raise ValueError('发送结果待核查；不要重复发送')
