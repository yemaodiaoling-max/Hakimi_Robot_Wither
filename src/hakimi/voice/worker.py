"""One broker-side ASR worker; QQ only polls completed work."""
import fcntl,json,os,select,subprocess,sys,time
from pathlib import Path
from hakimi import paths
from hakimi.memory import voice_receipts as store

class Recognizer:
    def __init__(self,base,cfg):self.base=Path(base);self.cfg=cfg;self.process=None
    def close(self):
        if self.process:
            self.process.kill();self.process.wait();self.process=None
    def run(self,wav,timeout=90):
        if self.process is None or self.process.poll() is not None:
            config=self.base/'state/voice_recognizer.json';config.write_text(json.dumps(self.cfg))
            self.process=subprocess.Popen([self.cfg['python'],'-m','hakimi.voice.recognizer',str(config)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=helper_env())
        self.process.stdin.write(json.dumps({'wav':str(wav)}).encode()+b'\n');self.process.stdin.flush();data=b'';deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if select.select([self.process.stdout],[],[],max(0,deadline-time.monotonic()))[0]:
                piece=os.read(self.process.stdout.fileno(),65536)
                if not piece:self.close();raise RuntimeError('recognizer_stopped')
                data+=piece
                if len(data)>200000:self.close();raise RuntimeError('recognizer_output_limit')
                if b'\n' in data:return json.loads(data.split(b'\n',1)[0])
        self.close();raise TimeoutError('recognizer_timeout')

def helper_env():
    env={key:value for key,value in os.environ.items() if not any(secret in key.upper() for secret in ('SECRET','TOKEN','API_KEY','AGY_LLM_GATEWAY'))}
    env.update(PYTHONPATH=str(Path(__file__).resolve().parents[2]),OMP_NUM_THREADS='2',OPENBLAS_NUM_THREADS='2')
    return env

def process(base,job,cfg,recognizer):
    work=Path(base)/'state/voice_audio'/store.key(job['session'],job['mid']);work.mkdir(parents=True,exist_ok=True)
    text=[];duration=0;truncated=False
    try:
        if time.time()-job['at']>cfg.get('max_queue_age_seconds',1800):
            store.finish(base,job,error='SOURCE_EXPIRED');return
        for i,attachment in enumerate(job['event'].get('_voice_attachments',[])[:3]):
            wav=work/(str(i)+'.wav');task={'url':attachment['url'],'target':str(wav),'max_bytes':cfg['max_bytes'],'max_seconds':max(1,cfg['max_seconds']-int(duration))}
            result=subprocess.run([cfg['python'],'-m','hakimi.voice.audio'],input=json.dumps(task).encode(),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=helper_env(),timeout=25)
            decoded=json.loads(result.stdout)
            if decoded.get('error'):raise RuntimeError(decoded['error'])
            recognized=recognizer.run(wav);duration+=decoded['duration'];truncated|=decoded['truncated']
            if recognized.get('error'):raise RuntimeError(recognized['error'])
            if recognized.get('text'):text.append(recognized['text'])
            if duration>=cfg['max_seconds']:truncated=True;break
        content='\n'.join(text).strip()
        if not content:store.finish(base,job,error='NO_CLEAR_SPEECH',duration=duration)
        else:store.finish(base,job,text=content+('\n[语音超过处理上限，后续未识别]' if truncated else ''),duration=duration)
    except Exception as exc:
        reason=str(exc) if type(exc) is RuntimeError else type(exc).__name__
        if reason in {'HTTPError','URLError','TimeoutExpired','TimeoutError','recognizer_timeout','recognizer_stopped'} and job['attempts']<2:
            with store.database(base) as db:db.execute("UPDATE jobs SET status='queued',next=?,error='TRANSIENT_RETRY' WHERE session=? AND mid=? AND status='processing' AND attempts=?",(time.time()+min(30,2**(job['attempts']+1)),job['session'],job['mid'],job['attempts']+1))
        else:store.finish(base,job,error='NO_CLEAR_SPEECH' if reason=='no_clear_speech' else 'AUDIO_PROCESSING_FAILED',duration=duration)
    finally:
        for p in work.glob('*'):p.unlink(missing_ok=True)
        work.rmdir()

def main():
    base=paths.base();lock=Path(base)/'state/voice_worker.lock';lock.parent.mkdir(exist_ok=True,parents=True)
    with lock.open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with store.database(base) as db:db.execute("UPDATE jobs SET status='queued',next=? WHERE status='processing'",(time.time()+2,))
        cfg=store.settings(base);recognizer=Recognizer(base,cfg);last_cleanup=0
        try:
            while True:
                updated=store.settings(base)
                if updated!=cfg:
                    recognizer.close();cfg=updated;recognizer=Recognizer(base,cfg)
                if time.time()-last_cleanup>60:
                    store.prune(base)
                    from hakimi.voice.tts import cleanup
                    cleanup(base);last_cleanup=time.time()
                    for p in (Path(base)/'state/voice_audio').glob('*/*'):
                        if time.time()-p.stat().st_mtime>900:p.unlink(missing_ok=True)
                job=store.claim(base) if cfg.get('enabled') else None
                if job:process(base,job,cfg,recognizer)
                else:time.sleep(.4)
        finally:recognizer.close()
if __name__=='__main__':main()
