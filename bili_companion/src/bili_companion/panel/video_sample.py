"""Generate private, randomly chosen colour and speech challenges for a route change."""
import base64
import re
import secrets
import subprocess
import sys
import wave
from pathlib import Path

from bili_companion.cognition.frame_speech import VOICE_PYTHON


def make_clip(root,half_seconds=3):
    first, last = secrets.SystemRandom().sample(('red','blue','yellow','green','magenta'),2)
    fruit, other = secrets.SystemRandom().sample(('苹果','西瓜','香蕉','草莓'),2)
    line=f'我把{fruit}放在桌子上，把{other}放在篮子里。'
    side=secrets.choice(('left','right'))
    x1,x2=(20,140) if side=='left' else (140,20)
    wav, mp4 = root/'word.wav', root/'challenge.mp4'
    subprocess.run([VOICE_PYTHON, '-m', 'bili_companion.panel.video_sample', str(wav), line],
                   capture_output=True, check=True, timeout=45)
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',f'color={first}:s=192x128:r=8:d={half_seconds}',
        '-f','lavfi','-i',f'color={last}:s=192x128:r=8:d={half_seconds}','-i',str(wav),
        '-filter_complex',f'[0:v]drawbox=x={x1}:y=48:w=32:h=32:c=white:t=fill[a0];'
        f'[1:v]drawbox=x={x2}:y=48:w=32:h=32:c=white:t=fill[a1];[a0][a1]concat=n=2:v=1:a=0[v];[2:a]apad[a]',
        '-map','[v]','-map','[a]',
        '-t',str(half_seconds*2),'-c:v','libx264','-threads','1','-c:a','aac',str(mp4)],capture_output=True,check=True,timeout=20)
    payload={'contents':[{'parts':[{'inlineData':{'mimeType':'video/mp4','data':base64.b64encode(mp4.read_bytes()).decode()}},
        {'text':'按顺序说明画面的两种背景色、白色方块起初与最后的位置，并逐句用中文写出完整台词。只返回 JSON，first、last 用英文基本颜色（紫红填 magenta），start_side、end_side 用 left 或 right，speech 写台词。'}]}],
        'generationConfig':{'maxOutputTokens':1536,'responseMimeType':'application/json'}}
    return payload,{'first':first,'last':last,'start_side':side,'end_side':'right' if side=='left' else 'left',
                    'fruit':fruit,'other':other,'speech':line}


def check_answer(answer, expected):
    if not isinstance(answer,dict) or any(str(answer.get(k,'')).strip().lower()!=expected[k]
            for k in ('first','last','start_side','end_side')):
        return False
    speech=answer.get('speech')
    if not isinstance(speech,str) or len(speech)>150:return False
    words=re.sub(r'[^\u4e00-\u9fff]','',speech)
    # Accept ordinary ASR wording differences, while requiring both placements in the right order.
    return bool(re.search(expected['fruit']+r'.{0,7}桌(?:子|面)?上.{0,9}'+expected['other']+r'.{0,7}[篮蓝](?:子|筐)(?:里|内|中)',words))


if __name__=='__main__':
    import numpy as np
    import sherpa_onnx
    model=Path('/opt/hakimi/models/voice/melo')
    vits=sherpa_onnx.OfflineTtsVitsModelConfig(model=str(model/'model.onnx'),tokens=str(model/'tokens.txt'),lexicon=str(model/'lexicon.txt'))
    engine=sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(vits=vits,num_threads=2)))
    audio=engine.generate(sys.argv[2],sid=0,speed=1.05)
    with wave.open(sys.argv[1],'wb') as out:
        out.setparams((1,2,audio.sample_rate,0,'NONE',''));out.writeframes((np.clip(audio.samples,-1,1)*32767).astype('<i2').tobytes())
