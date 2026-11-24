"""Persistent local recognizer, no keys/network; JSON line requests and results."""
import json,sys,wave
from pathlib import Path

def model(settings):
    if settings['asr_backend']=='vosk':
        import vosk
        vosk.SetLogLevel(-1)
        return vosk.Model(settings['asr_model'])
    import sherpa_onnx
    if settings['asr_backend']=='wenet':
        root=Path(settings['asr_model'])
        return sherpa_onnx.OfflineRecognizer.from_wenet_ctc(model=str(root/'model.int8.onnx'),tokens=str(root/'tokens.txt'),num_threads=2)
    root=Path(settings['asr_model']);size=settings.get('asr_size','small')
    return sherpa_onnx.OfflineRecognizer.from_whisper(encoder=str(root/(size+'-encoder.int8.onnx')),decoder=str(root/(size+'-decoder.int8.onnx')),tokens=str(root/(size+'-tokens.txt')),language='zh',task='transcribe',num_threads=2)

def recognize(engine,path,settings):
    import numpy as np
    with wave.open(path,'rb') as f:data=f.readframes(f.getnframes())
    samples=np.frombuffer(data,dtype=np.int16).astype(np.float32)/32768
    segments=[];parts=[]
    for start in range(0,min(len(samples),16000*180),16000*30):
        chunk=samples[start:start+16000*30]
        if settings['asr_backend']=='vosk':
            import vosk
            recognizer=vosk.KaldiRecognizer(engine,16000);recognizer.SetWords(True)
            recognizer.AcceptWaveform((chunk*32768).astype(np.int16).tobytes());value=json.loads(recognizer.FinalResult());text=value.get('text','').replace(' ','')
            confidence=sum(word['conf'] for word in value.get('result',[]))/max(1,len(value.get('result',[])))
            if confidence<.5:text=''
        else:
            stream=engine.create_stream();stream.accept_waveform(16000,chunk);engine.decode_stream(stream);text=stream.result.text.strip();confidence=None
        if text:
            parts.append(text);segments.append({'start':start/16000,'end':(start+len(chunk))/16000,'text':text,'confidence':confidence})
        else:parts.append('[此段未能可靠识别]')
    return {'text':'\n'.join(parts) if segments else '', 'segments':segments,'model':settings['asr_backend']}

def main():
    settings=json.loads(Path(sys.argv[1]).read_text());engine=model(settings)
    for line in sys.stdin:
        try:
            request=json.loads(line);result=recognize(engine,request['wav'],settings)
            print(json.dumps(result,ensure_ascii=False),flush=True)
        except Exception as exc:print(json.dumps({'error':type(exc).__name__}),flush=True)
if __name__=='__main__':main()
