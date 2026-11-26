"""Bounded media decoding helper. Runs outside the QQ event loop."""
import array,io,json,math,os,subprocess,sys,wave
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request,build_opener,ProxyHandler
from hakimi.chat.official_base import NoRedirect

def approved(url):
    try:
        p=urlsplit(str(url));host=(p.hostname or '').lower();port=p.port
    except (ValueError,TypeError):return False
    return p.scheme=='https' and not p.username and not p.password and port in (None,443) and any(host==suffix or host.endswith('.'+suffix) for suffix in ('qq.com','qq.com.cn','qpic.cn','gtimg.cn'))

def decode(source,target,max_seconds=180):
    source=Path(source);target=Path(target);head=source.read_bytes()[:16]
    if head.startswith((b'#!SILK_V3',b'\x02#!SILK_V3')):
        import pysilk
        class PrefixComplete(Exception):pass
        class Bounded(io.BytesIO):
            def write(self,data):
                limit=16000*2*(max_seconds+1)
                if self.tell()+len(data)>limit:
                    super().write(data[:max(0,limit-self.tell())]);raise PrefixComplete()
                return super().write(data)
        output=Bounded()
        try:pysilk.decode(io.BytesIO(source.read_bytes()),output,16000)
        except PrefixComplete:pass
        with wave.open(str(target),'wb') as f:f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(output.getvalue())
    else:
        subprocess.run(['ffmpeg','-nostdin','-v','error','-protocol_whitelist','file,pipe','-i',str(source),'-t',str(max_seconds+1),'-ac','1','-ar','16000','-c:a','pcm_s16le','-y',str(target)],check=True,timeout=15,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    with wave.open(str(target),'rb') as f:
        if (f.getnchannels(),f.getsampwidth(),f.getframerate())!=(1,2,16000):raise ValueError('invalid_audio_format')
        pcm=f.readframes(16000*(max_seconds+1))
    samples=array.array('h',pcm)
    if sys.byteorder!='little':samples.byteswap()
    duration=len(samples)/16000
    voiced=sum(math.sqrt(sum(x*x for x in samples[i:i+320])/max(1,len(samples[i:i+320])))>100 for i in range(0,len(samples),320))
    if duration<.25 or voiced<8:raise ValueError('no_clear_speech')
    return {'duration':min(duration,max_seconds),'truncated':duration>max_seconds,'wav':str(target)}

def main():
    task=json.loads(sys.stdin.read(100000));target=Path(task['target']);target.parent.mkdir(parents=True,exist_ok=True)
    if task.get('url'):
        if not approved(task['url']):raise ValueError('unapproved_audio_host')
        maximum=min(12_000_000,int(task.get('max_bytes',12_000_000)))
        request=Request(task['url'],headers={'User-Agent':'Hakimi-Voice/1'})
        with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=8) as response:
            data=response.read(maximum+1)
        if not data or len(data)>maximum:raise ValueError('audio_size_limit')
        source=target.with_suffix('.download');source.write_bytes(data)
    else:source=Path(task['source'])
    try:result=decode(source,target,min(180,int(task.get('max_seconds',180))))
    finally:
        if task.get('url'):source.unlink(missing_ok=True)
    print(json.dumps(result))

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'error':str(exc) if type(exc) is ValueError else type(exc).__name__}));raise SystemExit(1)
