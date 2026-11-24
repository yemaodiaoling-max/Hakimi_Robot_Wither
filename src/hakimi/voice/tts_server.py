"""Resident ZipVoice synthesiser for Hakimi's QQ voice.

The production VPS has four shared vCPUs and no GPU. A cold ZipVoice process
costs roughly five seconds of model loading on top of synthesis, which is too
much for a chat reply, so this service keeps the model hot and answers one
request at a time over a private Unix socket. ``hakimi.voice.tts_worker`` still
owns loudness conditioning and the QQ Silk encode; this process only turns text
into 24 kHz PCM.
"""
import base64
import json
import os
import re
import socket
import sys
import threading
import time
import wave
from pathlib import Path


MAX_REQUEST = 16_384
MAX_SECONDS = 26.0


def _read_wav(path):
    import numpy as np
    with wave.open(str(path), 'rb') as stream:
        rate, channels, frames = stream.getframerate(), stream.getnchannels(), stream.getnframes()
        raw = stream.readframes(frames)
    audio = np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(audio, dtype=np.float32), rate


def mood_for(text):
    """Cheap, deterministic delivery selection; the prompt carries the emotion."""
    if re.search(r'晚安|谢谢|辛苦|抱抱|喜欢|温柔|休息|睡|别怕|没关系|对不起', text):
        return 'sweet'
    if re.search(r'[!！]|呀|啦|耶|哇|嘿嘿|冲|快|好耶|真的吗', text):
        return 'lively'
    if re.search(r'[?？]|吗|呢|吧|要不要|好不好', text):
        return 'bright'
    return 'calm'


class VoicePack:
    def __init__(self, root):
        self.root = Path(root)
        self.manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        self.voices = self.manifest['voices']
        self.loaded = {}

    def prompt(self, mood):
        name = mood if mood in self.voices else 'calm'
        if name not in self.loaded:
            entry = self.voices[name]
            path = self.root / entry['prompt']
            audio, rate = _read_wav(path)
            self.loaded[name] = (audio, rate, entry['text'])
        return name, self.loaded[name]


class Synthesizer:
    def __init__(self, model, pack, threads=4, steps=4):
        import sherpa_onnx
        self.sherpa = sherpa_onnx
        self.model = str(model)
        self.pack = VoicePack(pack)
        self.threads = int(threads)
        self.steps = int(steps)
        self.engine = None
        self.load_seconds = None
        self.lock = threading.Lock()

    def _engine(self):
        if self.engine is None:
            root = Path(self.model)
            config = self.sherpa.OfflineTtsConfig(model=self.sherpa.OfflineTtsModelConfig(
                zipvoice=self.sherpa.OfflineTtsZipvoiceModelConfig(
                    tokens=str(root/'tokens.txt'),
                    encoder=str(root/'encoder.int8.onnx'),
                    decoder=str(root/'decoder.int8.onnx'),
                    lexicon=str(root/'lexicon.txt') + ',' + str(root/'lexicon-en-us.txt'),
                    vocoder=str(root/'vocos_24khz.onnx')),
                debug=False, num_threads=self.threads, provider='cpu'))
            if not config.validate():
                raise RuntimeError('zipvoice_config_invalid')
            started = time.monotonic()
            self.engine = self.sherpa.OfflineTts(config)
            self.load_seconds = round(time.monotonic() - started, 3)
        return self.engine

    def synthesize(self, text, voice='auto', steps=None):
        import numpy as np
        with self.lock:
            engine = self._engine()
            mood = mood_for(text) if voice in (None, '', 'auto') else voice
            mood, (audio, rate, prompt_text) = self.pack.prompt(mood)
            config = self.sherpa.GenerationConfig()
            config.reference_audio = audio
            config.reference_sample_rate = rate
            config.reference_text = prompt_text
            config.num_steps = int(steps or self.steps)
            config.extra['min_char_in_sentence'] = '30'
            started = time.monotonic()
            output = engine.generate(text, config)
            elapsed = time.monotonic() - started
            samples = np.asarray(output.samples, dtype=np.float32)
            if not len(samples) or samples.size > output.sample_rate * MAX_SECONDS:
                raise RuntimeError('zipvoice_duration_limit')
            pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype('<i2').tobytes()
            return dict(pcm=base64.b64encode(pcm).decode('ascii'), sample_rate=output.sample_rate,
                        voice=mood, steps=config.num_steps, seconds=round(len(samples)/output.sample_rate, 2),
                        synthesis_seconds=round(elapsed, 3), load_seconds=self.load_seconds)


def serve(socket_path, synthesizer):
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(8)
    print(json.dumps({'ready': True, 'socket': str(path), 'threads': synthesizer.threads,
                      'steps': synthesizer.steps, 'voices': sorted(synthesizer.pack.voices)}), flush=True)
    while True:
        connection, _ = server.accept()
        with connection:
            try:
                connection.settimeout(120)
                data = b''
                while b'\n' not in data and len(data) < MAX_REQUEST:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                if not data.strip():
                    continue
                request = json.loads(data.split(b'\n', 1)[0])
                if request.get('op') == 'health':
                    answer = {'ok': True, 'load_seconds': synthesizer.load_seconds,
                              'voices': sorted(synthesizer.pack.voices)}
                else:
                    text = str(request.get('text') or '').strip()
                    if not 1 <= len(text) <= 80:
                        raise ValueError('text_length')
                    answer = synthesizer.synthesize(text, request.get('voice', 'auto'), request.get('steps'))
                    answer['ok'] = True
                connection.sendall((json.dumps(answer) + '\n').encode())
            except Exception as error:
                try:
                    connection.sendall((json.dumps({'ok': False, 'error': type(error).__name__,
                                                     'detail': str(error)[:200]}) + '\n').encode())
                except OSError:
                    pass


def main():
    base = Path(os.environ.get('HAKIMI_BASE', '/opt/hakimi/data'))
    config = json.loads((base / 'voice_config.json').read_text(encoding='utf-8'))
    socket_path = config.get('tts_socket') or str(base / 'state/tts_server.sock')
    model = config.get('tts_zipvoice_model') or '/opt/hakimi/models/voice/zipvoice'
    pack = config.get('tts_voice_pack') or '/opt/hakimi/models/voice/default'
    synthesizer = Synthesizer(model, pack, config.get('tts_threads', 4), config.get('tts_steps', 4))
    synthesizer._engine()
    print(json.dumps({'warm': True, 'load_seconds': synthesizer.load_seconds}), flush=True)
    serve(socket_path, synthesizer)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'fatal': type(exc).__name__, 'detail': str(exc)[:300]}), flush=True)
        raise
