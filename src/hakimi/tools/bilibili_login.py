"""User-authorized Bilibili-only browser login, encrypted for this Windows user."""
from hakimi import paths
from contextlib import contextmanager
import http.cookiejar
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.request
import urllib.parse

ROOT = paths.base()
if ROOT.name == "official_test_runtime":ROOT = ROOT.parent
VAULT = ROOT/"state/bilibili_cookies.dpapi"


def _crypt(data, protect):
    if os.name!='nt':
        from hakimi.core.host_secrets import crypt
        return crypt(data,protect)
    script = "$ErrorActionPreference='Stop';$v=[Console]::In.ReadToEnd();"
    if protect:
        script += "$s=ConvertTo-SecureString $v -AsPlainText -Force;[Console]::Write((ConvertFrom-SecureString $s))"
    else:
        script += "$s=ConvertTo-SecureString $v;$p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s);try{[Console]::Write([Runtime.InteropServices.Marshal]::PtrToStringBSTR($p))}finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p)}"
    shell = shutil.which("powershell.exe") or shutil.which("pwsh")
    result = subprocess.run([shell,"-NoProfile","-NonInteractive","-Command",script],input=data,
                            capture_output=True,timeout=12,
                            env={k:v for k,v in os.environ.items() if k.upper() != "PSMODULEPATH"},
                            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    if result.returncode:raise RuntimeError("BILIBILI_LOGIN_ENCRYPTION_FAILED")
    return result.stdout


def export_edge():
    from yt_dlp.cookies import extract_cookies_from_browser
    jar = extract_cookies_from_browser("edge")
    return save_jar(jar)


def save_jar(jar):
    rows = [{key:getattr(c,key) for key in ("name","value","domain","path","secure","expires")}
            for c in jar if c.domain.lstrip(".") in {"bilibili.com","b23.tv"} or c.domain.endswith(".bilibili.com")]
    if not any(row["name"] == "SESSDATA" for row in rows):raise RuntimeError("NO_BILIBILI_LOGIN_FOUND")
    VAULT.write_bytes(_crypt(json.dumps(rows).encode(), True))
    return {"saved":True,"site_cookie_count":len(rows),"has_login":any(c["name"]=="SESSDATA" for c in rows)}


def qr_login(poll=False):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    if poll:
        key = json.loads((ROOT/"state/bili_qr_pending.json").read_text(encoding="utf-8"))["key"]
        url = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?"+urllib.parse.urlencode({"qrcode_key":key})
    else:url = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    with opener.open(urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"}),timeout=10) as response:
        data = json.load(response).get("data",{})
    if poll:
        if data.get("code") == 0:
            if not any(c.name == "SESSDATA" for c in jar) and data.get("url"):
                parsed = urllib.parse.urlsplit(data["url"])
                if parsed.scheme == "https" and (parsed.hostname or "").endswith(".bilibili.com"):
                    try:
                        with opener.open(data["url"],timeout=10) as response:response.read(1024)
                    except urllib.error.HTTPError:pass
                if not any(c.name == "SESSDATA" for c in jar) and (parsed.hostname or "").endswith(".bilibili.com"):
                    params = dict(urllib.parse.parse_qsl(parsed.query))
                    for name in ("SESSDATA","bili_jct","DedeUserID","DedeUserID__ckMd5"):
                        if params.get(name):jar.set_cookie(http.cookiejar.Cookie(0,name,params[name],None,False,
                            ".bilibili.com",True,True,"/",True,True,int(time.time())+30*86400,False,None,None,{}))
            result = save_jar(jar)
            (ROOT/"state/bili_qr_pending.json").unlink(missing_ok=True)
            return result
        return {"saved":False,"qr_code":data.get("code")}
    import qrcode
    path = ROOT/f"state/bilibili_login_qr_{int(time.time())}.png"
    qrcode.make(data["url"]).save(path)
    (ROOT/"state/bili_qr_pending.json").write_text(json.dumps({"key":data["qrcode_key"]}),encoding="utf-8")
    return {"qr_image":str(path)}


@contextmanager
def cookie_file():
    if not VAULT.exists():
        yield None
        return
    rows = json.loads(_crypt(VAULT.read_bytes(), False))
    with tempfile.TemporaryDirectory(prefix="bili_auth_",dir=ROOT/"state") as folder:
        path = Path(folder)/"cookies.txt"
        jar = http.cookiejar.MozillaCookieJar(str(path))
        for row in rows:
            jar.set_cookie(http.cookiejar.Cookie(0,row["name"],row["value"],None,False,row["domain"],True,
                row["domain"].startswith("."),row["path"],True,row["secure"],row["expires"],False,None,None,{}))
        jar.save(ignore_discard=True,ignore_expires=True)
        try:yield path
        finally:path.unlink(missing_ok=True)


if __name__ == "__main__":
    import sys
    try:print(json.dumps(qr_login(poll="--poll" in sys.argv) if any(a in sys.argv for a in ("--qr","--poll")) else export_edge()))
    except Exception as exc:print(json.dumps({"saved":False,"error_type":type(exc).__name__,
        "code":str(exc) if str(exc).startswith(("BILIBILI_","NO_BILIBILI_")) else "LOGIN_NOT_COMPLETED"}))
