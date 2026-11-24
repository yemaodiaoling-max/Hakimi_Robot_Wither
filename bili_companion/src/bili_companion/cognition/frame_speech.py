"""Turn a short MP4 into ordered, timestamped pictures and local Chinese ASR."""
import base64
import json
import re
import subprocess
import sys
import wave
from array import array
from pathlib import Path

from .gateway import CognitionUnavailable

VOICE_PYTHON = '/opt/hakimi/voice-runtime/bin/python'
WENET = Path('/opt/hakimi/models/voice/wenet')


def transcribe(wav):
    # Run in the existing speech environment. The video service itself has no ML dependencies.
    result = subprocess.run([VOICE_PYTHON, '-m', 'bili_companion.cognition.frame_speech', str(wav)],
                            capture_output=True, check=True, timeout=40)
    return json.loads(result.stdout)


def media_parts(source, root, duration, has_audio, *, retain_frames_without_speech=False):
    spacing = max(.25, min(5., duration / 5))
    filters = (f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{spacing:.4f})',"
               "scale=512:512:force_original_aspect_ratio=decrease:force_divisible_by=2,showinfo")
    capture = subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'info', '-i', str(source),
                              '-map', '0:v:0', '-vf', filters, '-fps_mode', 'vfr', '-frames:v', '8',
                              '-threads', '1', str(root/'frame-%02d.jpg')], capture_output=True, check=True, timeout=30)
    frames = sorted(root.glob('frame-*.jpg'))
    times = re.findall(rb'\bpts_time:([0-9.eE+-]+)', capture.stderr)
    if not frames or len(times) < len(frames):
        raise CognitionUnavailable('video_frames_unavailable')
    parts = []
    for frame, when in zip(frames, times):
        parts.append({'type':'text', 'text':f'画面 · 片段内 {float(when):.2f} 秒'})
        data = base64.b64encode(frame.read_bytes()).decode('ascii')
        parts.append({'type':'image_url', 'image_url':{'url':'data:image/jpeg;base64,'+data, 'detail':'low'}})

    speech = ''
    audible = False
    if has_audio:
        wav = root/'speech.wav'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(source), '-map', '0:a:0',
                        '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(wav)],
                       capture_output=True, check=True, timeout=30)
        with wave.open(str(wav), 'rb') as audio:
            samples = array('h', audio.readframes(audio.getnframes()))
        audible = bool(samples and max(max(samples), -min(samples)) > 100)
        if audible:
            try:
                value = transcribe(wav)
                speech = value.strip() if isinstance(value, str) else ''
            except (OSError, ValueError, subprocess.SubprocessError):
                if not retain_frames_without_speech:
                    raise
            if not speech and not retain_frames_without_speech:
                raise CognitionUnavailable('original_sound_required')
    if speech:
        audio_note = ('本机 WeNet 从整段音轨识别的中文台词（可能有错字，无逐句时间；用段落范围）：' + speech[:1600])
    elif audible:
        audio_note = '本机未从实际音轨识别出可用台词。标记 audio 缺失，只写画面证据。'
    else:
        audio_note = '此片段没有音轨或音轨静音。标记 audio 缺失，只写画面证据。'
    parts.append({'type':'text', 'text':audio_note})
    text_bytes = sum(len(p['text'].encode()) for p in parts if p['type']=='text')
    return parts, len(frames)*1024 + text_bytes, bool(speech)


if __name__ == '__main__':
    import numpy as np
    import sherpa_onnx
    engine = sherpa_onnx.OfflineRecognizer.from_wenet_ctc(model=str(WENET/'model.int8.onnx'),
        tokens=str(WENET/'tokens.txt'), num_threads=2)
    with wave.open(sys.argv[1], 'rb') as source:
        pcm = np.frombuffer(source.readframes(source.getnframes()), dtype='<i2').astype(np.float32)/32768
    stream = engine.create_stream()
    stream.accept_waveform(16000, pcm)
    engine.decode_stream(stream)
    print(json.dumps(stream.result.text.strip()))
