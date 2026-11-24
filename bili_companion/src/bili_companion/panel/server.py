"""Loopback provider setup panel. It only saves/probes, never switches production."""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit

from .vault import LocalVault,validate_url
from .activity import ACTIVITY_PAGE,read_activity

PAGE=r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>哈基米 · 模型接口设置</title><style nonce="NONCE">
:root{font-family:system-ui,"Microsoft YaHei",sans-serif;color:#253247;background:#f4f6fa}body{max-width:680px;margin:40px auto;padding:0 22px}main{background:white;border:1px solid #e1e6ee;border-radius:18px;padding:28px;box-shadow:0 8px 32px #22334d08}h1{font-size:25px;margin:0 0 12px}p{line-height:1.7;color:#566279}label{display:block;font-weight:600;margin:22px 0 8px}input,select,button{box-sizing:border-box;width:100%;font:inherit;border:1px solid #cbd3df;border-radius:9px;padding:12px}button{margin-top:24px;background:#245fbe;color:white;border:0;cursor:pointer}button:disabled{opacity:.5}small{display:block;color:#66758b;margin-top:8px;line-height:1.6}.notice{padding:12px 15px;background:#f0f5fd;border-radius:9px;font-size:14px;line-height:1.7}.warning{background:#fff4db;color:#795700}#httpConfirm input{width:auto;margin-right:8px}#httpConfirm{font-size:14px;font-weight:400}#status{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.8;margin-top:25px;border-top:1px solid #e6eaf0;padding-top:20px}#models{max-height:260px;overflow:auto;white-space:pre-wrap;font-size:13px;color:#52627a}footer{font-size:13px;text-align:center;color:#7a8799;margin:20px} [hidden]{display:none!important}</style>
<main><h1>哈基米 · 模型接口设置</h1><p>填写新的 API 地址和密钥，先检查协议与可用模型。验证完成后再切换观看机器人。</p>
<div class="notice">当前月预算以本页状态显示为准。接口、模型、音画能力与计费验证成功后，再完成接口与预算切换。</div>
<form id="setup" autocomplete="off"><label for="url">API 地址</label><input id="url" type="url" required maxlength="2048" placeholder="https://api.example.com" autocomplete="off" spellcheck="false"><small>可填写兼容服务的根地址或 /v1 地址；请勿在地址中加入密钥。</small>
<div id="tlsWarning" class="notice warning" hidden>此地址使用 HTTP，连接没有 TLS 加密。远程服务器会以明文接收密钥。</div>
<label id="httpConfirm" hidden><input id="confirm" type="checkbox">我允许向这个 HTTP 地址以明文发送密钥</label>
<label for="key">API 密钥</label><input id="key" type="password" required maxlength="8192" autocomplete="new-password" spellcheck="false"><small>密钥仅提交至此电脑上的面板，不会显示在结果中；Windows 使用当前用户加密存储。</small>
<label for="protocol">协议</label><select id="protocol"><option value="auto">自动识别</option><option value="gemini">Gemini 原生</option><option value="openai">OpenAI 兼容</option></select>
<button id="save" type="submit">保存并检测</button><div id="submitFeedback" role="alert" class="notice warning" hidden></div></form><div id="status" role="status">正在读取状态…</div><div id="models"></div></main>
<footer>仅监听本机 127.0.0.1 · 保存不会直接修改线上配置或预算</footer>
<script nonce="NONCE">let token='TOKEN';const url=document.getElementById('url'), key=document.getElementById('key'),confirm=document.getElementById('confirm');
function tls(){let u;try{u=new URL(url.value)}catch(e){}const http=u&&u.protocol==='http:';const local=u&&(u.hostname==='localhost'||u.hostname==='[::1]'||u.hostname.startsWith('127.'));document.getElementById('tlsWarning').hidden=!http;document.getElementById('httpConfirm').hidden=!(http&&!local);return {http,local};}url.addEventListener('input',()=>{confirm.checked=false;tls()});
const labels={empty:'等待填写新的接口信息',pending:'已保存，等待检测',probing:'已保存，正在获取可用模型…',models_ready:'已获取可用模型；接下来评估模型、音画能力与计费。当前接口和预算仍未切换。',probe_failed:'已保存，但接口检测未通过。请检查地址、密钥或协议后重新提交。',testing:'正在验证模型、音画能力与计费…',verified:'验证通过，等待完成接口切换。',active:'新接口已启用。'};
async function refreshStatus(){try{const r=await fetch('/status',{cache:'no-store'});if(!r.ok)throw Error();const s=await r.json();document.getElementById('status').textContent=(labels[s.phase]||'等待检查')+(s.message_code?'\n检测结果：'+s.message_code:'')+'\n当前月预算：'+(s.budget_usd||10)+' 美元';document.getElementById('models').textContent=s.model_ids&&s.model_ids.length?'接口提供的可用模型\n'+s.model_ids.join('\n'):'';}catch(e){document.getElementById('status').textContent='面板暂时不可连接，请确认本机服务仍在运行。';}}
function submitFeedback(message){const box=document.getElementById('submitFeedback');box.textContent=message;box.hidden=!message;}
document.getElementById('setup').addEventListener('submit',async e=>{e.preventDefault();submitFeedback('');const t=tls();if(t.http&&!t.local&&!confirm.checked){submitFeedback('请先确认该 HTTP 地址的明文连接提示。');return}const button=document.getElementById('save');button.disabled=true;try{const session=await fetch('/session-token',{cache:'no-store',credentials:'same-origin'});if(!session.ok)throw Error();const fresh=await session.json();if(typeof fresh.token!=='string'||!fresh.token)throw Error();token=fresh.token;const r=await fetch('/submit',{method:'POST',headers:{'Content-Type':'application/json','X-Panel-Token':token},body:JSON.stringify({base_url:url.value.trim(),api_key:key.value.trim(),protocol:document.getElementById('protocol').value,insecure_http_confirmed:confirm.checked})});const data=await r.json();if(!r.ok){submitFeedback('未能保存：'+(data.error||'请重新检查输入')+'。输入已保留，可直接重试。');return}key.value='';submitFeedback('已保存。检测结果请见下方状态。');await refreshStatus();}catch(e){submitFeedback('本机提交失败，输入已保留。请确认服务仍在运行后重试。');}finally{button.disabled=false;}});refreshStatus();setInterval(refreshStatus,3000);</script></html>'''


def loopback_host(host):
    if host=='localhost':return True
    try:return ipaddress.ip_address(host).is_loopback
    except ValueError:return False


class PanelServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,port,vault,probe_function=None,activity_file=None):
        super().__init__(('127.0.0.1',port),PanelHandler)
        self.vault=vault;self.token=secrets.token_urlsafe(32)
        self.origin='http://127.0.0.1:'+str(self.server_port)
        self.probe_function=probe_function
        self.activity_file=Path(activity_file) if activity_file is not None else Path(__file__).resolve().parents[3]/'state/activity.json'

    def handle_error(self,request,client_address):
        pass  # No exception text, submitted values, request paths or headers in logs.

    def probe(self,submission):
        identity=submission['submission_id']
        try:
            if not self.vault.write_status(identity,'probing'):return
            function=self.probe_function
            if function is None:
                from bili_companion.cognition.provider_probe import probe_models
                function=probe_models
            result=function(submission['base_url'],submission['api_key'],submission['protocol'],
                allow_insecure_http=submission['insecure_http_confirmed'] or loopback_host(urlsplit(submission['base_url']).hostname))
            ready=result.get('state')=='models_ready'
            fields=dict(protocol=result.get('protocol','auto'),pricing_required=True)
            if ready:fields['model_ids']=result.get('model_ids',[])
            else:fields['message_code']=result.get('error') or 'probe_failed'
            self.vault.write_status(identity,'models_ready' if ready else 'probe_failed',**fields)
        except Exception:
            self.vault.write_status(identity,'probe_failed',message_code='probe_unavailable')


class PanelHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def reply(self,code,payload,*,html=False,nonce=None):
        body=payload.encode('utf-8') if html else json.dumps(payload,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(code)
        self.send_header('Content-Type','text/html; charset=utf-8' if html else 'application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Cross-Origin-Opener-Policy','same-origin')
        self.send_header('Cross-Origin-Resource-Policy','same-origin')
        self.send_header('Content-Security-Policy',"default-src 'none'; connect-src 'self'; script-src 'nonce-"+(nonce or 'none')+"'; style-src 'nonce-"+(nonce or 'none')+"'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers();self.wfile.write(body)

    def guarded(self,*,post=False):
        if (len(self.headers.get_all('Host',[]))!=1 or self.headers.get('Host')!=urlsplit(self.server.origin).netloc
                or self.headers.get('Sec-Fetch-Site')=='cross-site'):
            self.reply(403,dict(error='local_origin_required'));return False
        if post and (self.headers.get('Origin')!=self.server.origin or not hmac.compare_digest(
                self.headers.get('X-Panel-Token',''),self.server.token)):
            self.reply(403,dict(error='csrf_check_failed'));return False
        return True

    def do_GET(self):
        if not self.guarded():return
        if self.path=='/':
            nonce=secrets.token_urlsafe(20)
            page=PAGE.replace('<main>','<main><p><a href="/activity">查看观看与评论记录 →</a></p>',1)
            self.reply(200,page.replace('NONCE',nonce).replace('TOKEN',self.server.token),html=True,nonce=nonce)
        elif self.path=='/activity':
            nonce=secrets.token_urlsafe(20)
            self.reply(200,ACTIVITY_PAGE.replace('NONCE',nonce),html=True,nonce=nonce)
        elif self.path=='/activity-data':
            try:self.reply(200,read_activity(self.server.activity_file))
            except Exception:self.reply(503,dict(error='activity_unavailable'))
        elif self.path=='/status':
            try:self.reply(200,self.server.vault.public_status())
            except Exception:self.reply(500,dict(error='status_unavailable'))
        elif self.path=='/session-token':
            self.reply(200,dict(token=self.server.token))
        else:self.reply(404,dict(error='not_found'))

    def do_POST(self):
        if not self.guarded(post=True):return
        if self.path!='/submit':self.reply(404,dict(error='not_found'));return
        if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type')!='application/json':
            self.reply(415,dict(error='json_required'));return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 1<=length<=20000:raise ValueError()
            value=json.loads(self.rfile.read(length))
            if type(value) is not dict or set(value)-{'base_url','api_key','protocol','insecure_http_confirmed'}:raise ValueError()
            url=validate_url(value['base_url'].strip())
            api_key=value['api_key'].strip()
            confirmed=value.get('insecure_http_confirmed') is True
            if urlsplit(url).scheme=='http' and not loopback_host(urlsplit(url).hostname) and not confirmed:
                self.reply(400,dict(error='insecure_http_confirmation_required'));return
            identity=self.server.vault.save_submission(url,api_key,value.get('protocol','auto'),
                insecure_http_confirmed=confirmed)
            submission=dict(submission_id=identity,base_url=url,api_key=api_key,
                protocol=value.get('protocol','auto'),insecure_http_confirmed=confirmed)
        except Exception:
            self.reply(400,dict(error='invalid_input'));return
        threading.Thread(target=self.server.probe,args=(submission,),daemon=True).start()
        self.reply(202,dict(submission_id=identity,phase='pending',live_config_changed=False,budget_usd=10))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--private-dir',default=str(Path(__file__).resolve().parents[3]/'state/private/api_panel'))
    parser.add_argument('--memory-only',action='store_true')
    parser.add_argument('--activity-file',default=None)
    args=parser.parse_args()
    server=PanelServer(args.port,LocalVault(args.private_dir,memory_only=args.memory_only),activity_file=args.activity_file)
    print('Local setup panel: '+server.origin,flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':main()
