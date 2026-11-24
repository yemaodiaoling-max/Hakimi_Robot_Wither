"""Offline-host model/renderer checks. No QQ gateway or message sends."""
from hakimi import paths
import base64
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile

ROOT=paths.root()
os.environ['PATH']=str(paths.dsh_bin())+':'+os.environ['PATH']
from hakimi.core.host_secrets import crypt
data=json.loads(crypt((Path.home()/'.config/hakimi/credentials.fernet').read_bytes(),False))
provider=data['provider']
os.environ.update(DEEPSEEK_API_KEY=provider['api_key'],DEEPSEEK_BASE_URL=provider['base_url'])
from hakimi.models.fallback_runner import run_fallback
from PIL import Image,ImageDraw

with tempfile.TemporaryDirectory(dir=ROOT/'state',prefix='cloud_smoke_') as tmp:
    base=Path(tmp)
    (base/'state').mkdir()
    # Use exactly the production broker, including PowerShell and no-tool guard.
    result=run_fallback('当前是离线部署自检，没有QQ群。请仅回复：哈基米报到',base)
    print(json.dumps({'dsh_text':result['action']=='reply' and bool(result['text'])}))
    image=base/'vision.png'
    canvas=Image.new('RGB',(420,220),'white')
    draw=ImageDraw.Draw(canvas);draw.rectangle((30,30,170,190),fill='red');draw.ellipse((230,40,390,200),fill='blue')
    canvas.save(image)
    encoded=io.BytesIO();canvas.save(encoded,format='JPEG')
    # The same normalized attachment format used by fallback_media.
    result=run_fallback('当前是离线看图自检。图上左右各是什么颜色和形状？用中文简短回答',base,
        images=[{'id':'I1','media_type':'image/jpeg','data':base64.b64encode(encoded.getvalue()).decode()}])
    print(json.dumps({'dsh_vision':result['action']=='reply' and all(x in result['text'] for x in ['红','蓝'])}))
    from hakimi.tools.math_render import render
    segments=render({'markdown':'# 公式部署验证\n\n由牛顿第二定律 $F=ma$，得到\n\n$$a=\\frac{F}{m}$$\n\n恢复力 F = -(GMm / R³) · r，因此成立。','font_size':18,'page_height':1600},'group:100',base)
    print(json.dumps({'math_pages':len(segments)}))

for name in ['memory.sqlite3','official_routes.sqlite3']:
    with sqlite3.connect(ROOT/'official_test_runtime/state'/name) as db:
        print(json.dumps({'database':name,'integrity':db.execute('PRAGMA quick_check').fetchone()[0]}))
