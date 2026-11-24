"""Offline original-voice synthesis, executed with a fixed CPU/clock budget.

The configured backends share one interface:

* ``vits``    -- the original Melo VITS voice, synthesised in-process.
* ``gpt-sovits`` -- an operator-configured local role model returning PCM16 WAV.
* ``zipvoice``-- zero-shot voice from your own licensed reference clips. When the resident ``hakimi-tts``
  service is reachable the 180 MB model is already hot; otherwise this process
  falls back to loading it locally, and finally to the VITS voice, so a voice
  request never ends in silence.
"""
import base64
import io
import json
import os
import math
import re
import signal
import socket
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

MAX_TEXT = 80
_DIGITS = '零一二三四五六七八九'
_FULLWIDTH = str.maketrans('０１２３４５６７８９', '0123456789')


def _number(value):
    number = int(value)
    if number < 10:
        return _DIGITS[number]
    if number < 20:
        return '十' + (_DIGITS[number % 10] if number % 10 else '')
    if number < 100:
        return _DIGITS[number // 10] + '十' + (_DIGITS[number % 10] if number % 10 else '')
    return ''.join(_DIGITS[int(c)] for c in str(number))


def spoken(text):
    """Digits and percent signs are spoken; the ZipVoice lexicon has no digits."""
    text = text.translate(_FULLWIDTH)
    text = re.sub(r'(\d+(?:\.\d+)?)%', lambda m: '百分之' + _number(m.group(1).replace('.', '')), text)
    text = re.sub(r'\d+', lambda m: _number(m.group(0)), text)
    text = re.sub(r'[\u200b-\u200f\ufe0e\ufe0f]', '', text)
    return text.strip()


def _server_task(task):
    settings = task['zipvoice']
    request = {'text': spoken(str(task['text'])), 'voice': settings.get('voice', 'auto'),
               'steps': settings.get('steps')}
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(float(settings.get('connect_timeout', 1.5)))
    try:
        connection.connect(settings['socket'])
        connection.settimeout(float(settings.get('read_timeout', 30)))
        connection.sendall((json.dumps(request, ensure_ascii=False) + '\n').encode())
        data = b''
        while b'\n' not in data:
            chunk = connection.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > 12_000_000:
                raise ValueError('tts_server_response_limit')
    finally:
        connection.close()
    answer = json.loads(data.split(b'\n', 1)[0])
    if not answer.get('ok'):
        raise RuntimeError(answer.get('detail') or answer.get('error') or 'tts_server_failed')
    pcm = base64.b64decode(answer['pcm'])
    import numpy as np
    return np.frombuffer(pcm, dtype='<i2').astype(np.float32)/32768.0, answer['sample_rate'], answer


def _local_task(task):
    import numpy as np
    import sherpa_onnx
    settings = task['zipvoice']
    root = Path(settings['model'])
    pack = Path(settings['pack'])
    manifest = json.loads((pack/'manifest.json').read_text(encoding='utf-8'))
    voice = settings.get('voice') or 'auto'
    if voice == 'auto':
        try:
            from hakimi.voice.tts_server import mood_for
            voice = mood_for(task['text'])
        except ImportError:
            voice = 'calm'
    entry = manifest['voices'].get(voice) or manifest['voices']['calm']
    audio, rate = _read_wav(pack/entry['prompt'])
    config = sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(
        zipvoice=sherpa_onnx.OfflineTtsZipvoiceModelConfig(
            tokens=str(root/'tokens.txt'), encoder=str(root/'encoder.int8.onnx'),
            decoder=str(root/'decoder.int8.onnx'),
            lexicon=str(root/'lexicon.txt') + ',' + str(root/'lexicon-en-us.txt'),
            vocoder=str(root/'vocos_24khz.onnx')),
        debug=False, num_threads=int(settings.get('threads', 4)), provider='cpu'))
    engine = sherpa_onnx.OfflineTts(config)
    generation = sherpa_onnx.GenerationConfig()
    generation.reference_audio = audio
    generation.reference_sample_rate = rate
    generation.reference_text = entry['text']
    generation.num_steps = int(settings.get('steps', 4))
    generation.extra['min_char_in_sentence'] = '30'
    output = engine.generate(spoken(str(task['text'])), generation)
    if not len(output.samples):
        raise RuntimeError('zipvoice_empty')
    return np.asarray(output.samples, dtype=np.float32), output.sample_rate, dict(voice=voice, local=True)


def _read_wav(source, *, max_seconds=None):
    import numpy as np
    with wave.open(str(source) if isinstance(source, Path) else source, 'rb') as stream:
        rate, channels, frames = stream.getframerate(), stream.getnchannels(), stream.getnframes()
        if stream.getsampwidth() != 2 or stream.getcomptype() != 'NONE':
            raise ValueError('invalid_tts_wav_format')
        if max_seconds is not None:
            if not (1 <= channels <= 2 and 8000 <= rate <= 48000 and 0 < frames <= rate*max_seconds):
                raise ValueError('invalid_tts_wav_dimensions')
        raw = stream.readframes(frames)
        if len(raw) != frames*channels*2:
            raise ValueError('incomplete_tts_wav')
    audio = np.frombuffer(raw, dtype='<i2').astype(np.float32)/32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, rate


def role_samples(text, command, timeout):
    """Read a bounded PCM16 WAV from the trusted operator-configured role CLI."""
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_TEXT or any(ord(c) < 32 for c in text):
        raise ValueError('invalid_role_text')
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise TimeoutError('role_time_budget_exhausted')
    if not isinstance(command, (str, Path)) or not Path(command).is_absolute():
        raise ValueError('invalid_role_command')
    args = [str(command), '--stdout']
    with tempfile.TemporaryFile() as audio:
        # A role launcher can start a second interpreter. Timeouts must stop both.
        with subprocess.Popen(args, stdin=subprocess.PIPE, stdout=audio, stderr=subprocess.DEVNULL,
                              start_new_session=True) as proc:
            try:
                proc.communicate(json.dumps({'text':text}, ensure_ascii=False).encode('utf-8'), timeout=timeout)
            except BaseException:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                raise
            if proc.returncode:
                raise subprocess.CalledProcessError(proc.returncode, args)
        if audio.tell() > 26*48000*2*2+65536:
            raise ValueError('role_wav_too_large')
        audio.seek(0)
        return _read_wav(audio, max_seconds=26)


def _role(task, output, bitrate):
    role = task['role']
    samples, rate = role_samples(task['text'],role['command'],role['timeout'])
    size = _encode(samples,rate,output,bitrate,True)
    return {'duration':round(len(samples)/rate,2),'bytes':size,'format':'tencent_silk_24k',
            'backend':'gpt-sovits'}


def _encode(samples, rate, output, bitrate, loudness):
    import numpy as np
    import pysilk
    if not len(samples) or not np.all(np.isfinite(samples)):
        raise ValueError('invalid_tts_samples')
    wav = output.with_suffix('.wav')
    pcm = output.with_suffix('.pcm')
    try:
        with wave.open(str(wav), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(rate)
            stream.writeframes((np.clip(samples, -1, 1)*32767).astype('<i2').tobytes())
        filters = []
        if loudness:
            filters.append('loudnorm=I=-21:TP=-3:LRA=10:linear=true')
        filters.append('aresample=24000')
        command = ['ffmpeg', '-nostdin', '-v', 'error', '-i', str(wav), '-ac', '1', '-af', ','.join(filters),
                   '-f', 's16le', '-y', str(pcm)]
        subprocess.run(command, check=True, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        encoded = io.BytesIO()
        pysilk.encode(io.BytesIO(pcm.read_bytes()), encoded, 24000, bitrate, tencent=True)
        data = encoded.getvalue()
        if not data.startswith(b'\x02#!SILK_V3') or len(data) > 2_000_000:
            raise ValueError('invalid_tencent_silk')
        output.write_bytes(data)
        return len(data)
    finally:
        wav.unlink(missing_ok=True)
        pcm.unlink(missing_ok=True)


def _vits(task, output, bitrate):
    import numpy as np
    import sherpa_onnx
    root = Path(task['model'])
    engine = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(
        vits=sherpa_onnx.OfflineTtsVitsModelConfig(model=str(root/'model.onnx'), tokens=str(root/'tokens.txt'),
                                                   lexicon=str(root/'lexicon.txt')), num_threads=2)))
    audio = engine.generate(task['text'], sid=0, speed=1.05)
    if not 0 < len(audio.samples) <= audio.sample_rate*25:
        raise ValueError('tts_duration_limit')
    size = _encode(np.asarray(audio.samples, dtype=np.float32), audio.sample_rate, output, bitrate, False)
    return {'duration': round(len(audio.samples)/audio.sample_rate, 2), 'bytes': size,
            'format': 'tencent_silk_24k', 'original_stock_voice': True, 'backend': 'vits'}


def _zipvoice(task, output, bitrate):
    import time
    started = time.monotonic()
    settings = task['zipvoice']
    details = {}
    try:
        samples, rate, details = _server_task(task)
        path = 'server'
    except Exception as error:
        details = {'server_error': type(error).__name__}
        samples, rate, details = _local_task(task)
        path = 'local'
    synthesised = time.monotonic() - started
    if not 0 < len(samples) <= rate*26:
        raise ValueError('tts_duration_limit')
    size = _encode(samples, rate, output, bitrate, True)
    return {'duration': round(len(samples)/rate, 2), 'bytes': size, 'format': 'tencent_silk_24k',
            'original_stock_voice': True, 'backend': 'zipvoice', 'path': path,
            'voice': details.get('voice'), 'steps': settings.get('steps'),
            'synthesis_seconds': round(synthesised, 2)}


def main():
    task = json.loads(sys.stdin.read(20000))
    output = Path(task['output'])
    bitrate = int(task.get('bitrate', 32000))
    engine = (task.get('engine') or 'vits').lower()
    if engine == 'gpt-sovits' or (engine == 'zipvoice' and task.get('zipvoice')):
        try:
            metadata = _role(task,output,bitrate) if engine == 'gpt-sovits' else _zipvoice(task,output,bitrate)
        except Exception as error:
            if not task.get('fallback_model'):
                raise
            output.unlink(missing_ok=True)
            metadata = _vits(dict(task, model=task['fallback_model']), output, bitrate)
            metadata['backend'] = 'vits-fallback'
            metadata['role_error' if engine == 'gpt-sovits' else 'zipvoice_error'] = type(error).__name__
    else:
        metadata = _vits(task, output, bitrate)
    metadata['voice_revision'] = task.get('revision') or engine
    print(json.dumps(metadata, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
