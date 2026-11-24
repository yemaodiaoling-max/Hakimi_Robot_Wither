"""One reusable DSH process for text selection, owned by the worker service."""
import atexit
from collections import deque
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import uuid


class TextWorker:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = self.job = self.connection = self.reader = None
        self.trace = deque(maxlen=128)
        self.trace_lock = threading.Lock()
        self.base = None
        atexit.register(self.close)

    def close(self):
        if self.connection:
            self.connection.close()
        if self.reader:
            self.reader.close()
        if self.job:
            self.job.close()
        if self.process:
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.process = self.job = self.connection = self.reader = None

    def _stderr(self, process):
        for line in iter(process.stderr.readline, b''):
            if line.startswith(b'HAKIMI_TRACE '):
                with self.trace_lock:
                    self.trace.append(line)
        process.stderr.close()

    def _receive(self, timeout):
        self.connection.settimeout(max(.1, timeout))
        line = self.reader.readline(1048576)
        if not line or not line.endswith(b'\n'):
            raise RuntimeError('TEXT_JUDGE_WORKER_DISCONNECTED')
        return json.loads(line)

    def _start(self, base, deadline):
        from hakimi.models.dsh_text_judge import _installation, _provider, model_patch, TRANSPORT_TRACE
        from hakimi.models.fallback_runner import _WindowsJob, _PosixJob
        import base64
        installation = _installation()
        provider = _provider(installation)
        state = base / 'state'; state.mkdir(parents=True, exist_ok=True)
        module = state / 'dsh_text_worker.mjs'
        module.write_text(Path(__file__).with_suffix('.mjs').read_text(encoding='utf-8'), encoding='utf-8')
        patch = model_patch(provider) + [
            {'id': 'headless-runner', 'disabled': True},
            {'insert': [{'id': 'hakimi-text-worker', 'name': module.as_uri()}]}]
        patch_path = state / 'dsh_text_worker.patch.json'
        patch_path.write_text(json.dumps(patch), encoding='utf-8')
        trace_path = state / 'dsh_text_judge_trace.cjs'
        trace_path.write_text(TRANSPORT_TRACE, encoding='utf-8')
        libraries = installation['package'] / 'node_modules' / '@deepseek-ai'
        if not (libraries / 'dsh-agent/lib/index.js').is_file():
            libraries = installation['package'].parent
        env = dict(os.environ, DEEPSEEK_API_KEY=provider['api_key'],
                   DSH_PERMISSION_MODE='danger-full-access', QQ_TEXT_JUDGE_BASE=str(base),
                   QQ_TEXT_JUDGE_PATCH=str(patch_path), QQ_TEXT_JUDGE_DSH=str(installation['shim']),
                   QQ_TEXT_JUDGE_LIBRARIES=str(libraries))
        env['NODE_OPTIONS'] = (env.get('NODE_OPTIONS', '') + ' --require=' + json.dumps(str(trace_path))).strip()
        driver = """$ErrorActionPreference = 'Stop'
$env:DSH_PERMISSION_MODE = 'danger-full-access'
Set-Location -LiteralPath $env:QQ_TEXT_JUDGE_BASE
& $env:QQ_TEXT_JUDGE_DSH --profile headless --patch $env:QQ_TEXT_JUDGE_PATCH 'persistent text selection'
exit $LASTEXITCODE
"""
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); listener.listen(1)
            listener.settimeout(max(.1, deadline-time.monotonic()))
            env['QQ_TEXT_JUDGE_PORT'] = str(listener.getsockname()[1])
            command = base64.b64encode(driver.encode('utf-16-le')).decode('ascii')
            self.process = subprocess.Popen([installation['powershell'], '-NoLogo', '-NoProfile',
                '-NonInteractive', '-EncodedCommand', command], cwd=base, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), start_new_session=os.name != 'nt')
            self.job = _WindowsJob(self.process) if os.name == 'nt' else _PosixJob(self.process)
            threading.Thread(target=self._stderr, args=(self.process,), daemon=True).start()
            self.connection, _ = listener.accept()
        self.reader = self.connection.makefile('rb')
        if self._receive(deadline-time.monotonic()).get('ready') is not True:
            raise RuntimeError('TEXT_JUDGE_WORKER_NOT_READY')
        self.base = base

    def usage(self):
        result={}
        with self.trace_lock:
            for line in self.trace:
                try:event=json.loads(line.decode().split('HAKIMI_TRACE ',1)[1])
                except (ValueError,IndexError):continue
                if event.get('phase')=='api_usage' and isinstance(event.get('usage'),dict):
                    result=event['usage']
        return result

    def run(self, task, base, timeout_seconds, thinking, *, parser=None, include_usage=False):
        from hakimi.models.dsh_text_judge import parse_selection, _phase_times, _timing, JudgeError
        base = Path(base).resolve()
        with self.lock:
            started = time.monotonic(); started_at = time.time()*1000
            deadline = started + max(1, float(timeout_seconds))
            warm = bool(self.process and self.process.poll() is None and self.base == base)
            job_usage={}
            with self.trace_lock:
                self.trace.clear()
            try:
                if not warm:
                    self.close(); self._start(base, deadline)
                launched = time.monotonic()
                request_id = uuid.uuid4().hex
                self.connection.settimeout(max(.1, deadline-time.monotonic()))
                self.connection.sendall((json.dumps(dict(id=request_id, task=str(task), thinking=thinking), ensure_ascii=False)+'\n').encode())
                result = self._receive(deadline-time.monotonic())
                job_usage=result.get('usage') if isinstance(result.get('usage'),dict) else {}
                if result.get('id') != request_id or result.get('error'):
                    raise JudgeError(result.get('error', 'TEXT_JUDGE_AGENT_FAILED') + ':' + str(result.get('detail', '')))
                parsing = time.monotonic()
                selection = (parser or parse_selection)(result.get('text', ''))
                parse_ms = round((time.monotonic()-parsing)*1000, 3)
                with self.trace_lock:
                    phases = _phase_times(b''.join(self.trace), started_at)
                phases.update(resident=True, warm=warm, thinking=thinking)
                _timing(base, started, launched, task, 'selected', phases=phases, parse_ms=parse_ms)
                return {'result':selection,'usage':job_usage or self.usage()} if include_usage else selection
            except Exception as exc:
                with self.trace_lock:
                    phases = _phase_times(b''.join(self.trace), started_at)
                phases.update(resident=True, warm=warm, thinking=thinking)
                usage=job_usage or self.usage()
                self.close()
                _timing(base, started, time.monotonic(), task, 'worker_error', phases=phases)
                if isinstance(exc, JudgeError):
                    exc.usage=usage
                    raise
                error=JudgeError('TEXT_JUDGE_WORKER_FAILED');error.usage=usage
                raise error from exc


worker = TextWorker()
