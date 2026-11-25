"""One-shot, zero-tool DSH fallback. Never consumes an event or sends a reply.

The trusted broker supplies already-filtered, current-session context. Only this
module prepares the isolated DSH runtime. The model receives no filesystem, shell,
web, MCP, skills or subagent tools. This is a capability boundary, NOT an OS
sandbox for the Node runtime or its installed dependencies.
"""
from hakimi import persona
from hakimi import paths

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from hakimi.bridge.reply_format import MAX_REPLY_TEXT_CHARS

BASE_DIR = paths.base()
RUNTIME_NAME = "dsh_fallback_runtime"
SUPPORTED_VERSION = "0.1.5-rc.2"
MAX_PROMPT_BYTES = 48 * 1024
MAX_PROMPT_CHARS = 24000
MAX_OUTPUT_BYTES = 64 * 1024
TIMEOUT_SECONDS = 90
RUN_LOCK = threading.Lock()
BASE_ENVIRONMENT_NAMES = frozenset({"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
                                   "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
                                   "HOME", "LANG", "LC_ALL", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"})

# Installed bundle composition is pinned: upgrades require offline review.
BUNDLE_HASHES = {
    "dsh-base": "1d34f61f498c5779ecaf2b13c4cbeea1254818450a1021dca594a30c03c230db",
    "dsh-headless": "f833fcb5d0ade52f9bf02756eff17348b518f56e39b052c6109dd4c21475f16d",
}
ALL_ROWS = """timer hmr llm deepseek-llm-api-extensions session session-log-deepseek
typert typert-loader typert-gateway session-title session-title-llm user-questions
agent plugin-package-inventory-deepseek agent-default-model jobs llm-retry settings
credentials llm-pi-ai session-persistence-jsonl attachment-local session-query-sqlite
session-projection storage storage-json storage-domain session-projection-cache
session-telemetry-otel subprocess sandbox sandbox-policy bash-sandbox pwsh-sandbox
approval permission shell-env tool-bash tool-pwsh tool-jobs fs-observation-policy
tool-fs tool-fs-search agent-instructions skill skill-filesystem skill-badge tool-skill
commands command-feedback goal goal-round-driver command-goal plan-mode token-meter
compaction-basic command-compact subagent subagent-spawn-in-process
subagent-fork-in-process tool-subagent-control tool-subagent-list-agents tool-subagent
tool-subagent-fork workflow-worker-thread tool-workflow timeout-policy spill-local
spill-policy session-checkpoint-policy tool-result-pruner tool-todo tool-goal
tool-ralph repeat-tool-reminder web web-search-deepseek web-fetch-http tool-web tools
system-prompt agent-loop fs-sandbox llm-deepseek code-runtime headless-startup
headless-runner""".split()
ENABLED_ROWS = frozenset("""timer llm deepseek-llm-api-extensions session session-title
agent agent-default-model session-projection sandbox-policy tools system-prompt
agent-loop llm-deepseek headless-startup headless-runner""".split())

# DSH itself may create only operational artifacts below this isolated root.
# The Node process is not OS-confined: these are reviewed runtime behaviors.
RUNTIME_ARTIFACTS = (
    "cordis.patch.yml", ".env", "guard.mjs", ".anonymous-user-id", "vision_input.json",
    "profiles/headless/package.json", "profiles/headless/cordis.patch.yml",
    "profiles/headless/cordis.yml", "profiles/headless/pnpm-workspace.yaml",
    "profiles/node_modules/**", "profiles/node_modules.lock/**",
    "profiles/headless/node_modules/**", "profiles/headless/.dsh-module-fallback/**",
    "attachments/v1/**",
)


class FallbackError(RuntimeError):
    """A fixed code safe to log; no prompts, provider errors, or credentials."""

    def __init__(self, code, hint=None):
        super().__init__(code)
        self.protocol_hint = hint or {
            'FALLBACK_REPLY_TARGET_INVALID': 'reply_to 必须是可见消息的十进制编号。',
            'FALLBACK_CONSULT_INVALID': 'consult 只含 action、text、question；question 为1–1000字，text 为1–2000字。',
            'FALLBACK_INTERACTION_INVALID': 'interaction 仅用于 reply，取 normal、conflict 或 ai。',
            'FALLBACK_DIARY_INVALID': '检查 diary_export 的范围；它仅用于 reply，不能与其他附件一起提交。',
            'FALLBACK_MATH_INVALID': 'math 仅用于 reply，按 math.send 的参数提供有效公式对象。',
            'FALLBACK_FILES_INVALID': 'files 仅用于 reply，提供有效文件名或带 name、content 的文件项。',
            'FALLBACK_STICKER_INVALID': 'sticker 仅用于 reply，请填写图库中的图片文件名。',
            'FALLBACK_VIDEO_FRAMES_INVALID': 'video_frames 仅用于 reply，提供1至2个 F1–F6 帧编号。',
            'FALLBACK_JSON_DECODE_FAILED': '根节点必须是一个完整的 JSON 对象。检查引号、逗号和结尾。',
            'FALLBACK_RESPONSE_FORBIDDEN_CONTENT': 'text 含受限内容；按普通群聊正文格式重写。',
        }.get(code)


def _safe_local(base, relative):
    base = Path(base).resolve()
    path = base / relative
    if path.resolve() != path or not path.resolve().is_relative_to(base):
        raise FallbackError("FALLBACK_REDIRECTED_PATH")
    return path


def _write_fixed(runtime, relative, value):
    target = _safe_local(runtime, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value, encoding="utf-8")


def discover_installation():
    """Resolve the installed PowerShell shim without invoking DSH or reading keys."""
    roaming = os.environ.get("APPDATA", "")
    shim = Path(roaming) / "npm" / "dsh.ps1"
    package = shim.parent / "node_modules" / "@deepseek-ai" / "dsh"
    if os.name!='nt':
        shim=Path(os.environ.get('HAKIMI_DSH_COMMAND') or shutil.which('dsh') or '/nonexistent/dsh')
        package=shim.resolve().parent.parent
    node = shutil.which("node")
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not shim.is_file() or not node or not powershell:
        raise FallbackError("FALLBACK_DSH_NOT_INSTALLED")
    try:
        version = json.loads((package / "package.json").read_text(encoding="utf-8"))["version"]
        if version != SUPPORTED_VERSION:
            raise FallbackError("FALLBACK_UNREVIEWED_DSH_VERSION")
        libraries = package / "node_modules" / "@deepseek-ai"
        if not (libraries/'dsh-base/cordis.patch.yml').is_file():libraries=package.parent
        for bundle, expected in BUNDLE_HASHES.items():
            actual = hashlib.sha256((libraries / bundle / "cordis.patch.yml").read_bytes()).hexdigest()
            if actual != expected:
                raise FallbackError("FALLBACK_UNREVIEWED_BUNDLE")
    except (OSError, ValueError, KeyError) as exc:
        raise FallbackError("FALLBACK_INSTALLATION_INVALID") from exc
    return {"shim": shim.resolve(), "package": package.resolve(),
            "node": node, "powershell": powershell}


PROVIDER_HELPER = r"""
const fs = require('node:fs');
const path = require('node:path');
const yaml = require(require.resolve('js-yaml', {paths:[process.argv[1]]}));
const home = process.argv[2];
function load(name) {
  const file=path.join(home,name);
  if(!fs.existsSync(file)) return {};
  if(fs.statSync(file).size > 65536) throw Error('config too large');
  return yaml.load(fs.readFileSync(file,'utf8')) ?? {};
}
const settings=load('settings.yaml');
const selected=settings['agent-default-model'] ?? {};
const route=settings['llm-deepseek'] ?? {};
const ref=route.apiKeyEnv ?? 'DEEPSEEK_API_KEY';
if(!/^[A-Z_][A-Z0-9_]*$/.test(ref)) throw Error('invalid credential reference');
const credentials=load('.credentials.yaml');
const secret=process.env[ref] || (credentials.version===1 ? credentials.refs?.[ref] : undefined);
const result={provider:selected.provider??'deepseek-official',model:selected.model??'deepseek-flash',
 base_url:route.baseURL??process.env.DEEPSEEK_BASE_URL??'https://api.deepseek.com',api_key:secret};
process.stdout.write(JSON.stringify(result));
"""


def _provider(installation):
    home = Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))
    try:
        result = subprocess.run(
            [installation["node"], "-e", PROVIDER_HELPER, str(installation["package"]), str(home)],
            capture_output=True, timeout=10, check=False,
            env={key: value for key, value in os.environ.items()
                 if key.upper() in BASE_ENVIRONMENT_NAMES | {"DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"}},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0 or len(result.stdout) > 16384:
            raise FallbackError("FALLBACK_PROVIDER_CONFIG_UNAVAILABLE")
        provider = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise FallbackError("FALLBACK_PROVIDER_CONFIG_UNAVAILABLE") from exc
    if provider.get("provider") != "deepseek-official":
        raise FallbackError("FALLBACK_PROVIDER_NOT_SUPPORTED")
    if not isinstance(provider.get("model"), str) or not re.fullmatch(r"deepseek-[A-Za-z0-9._-]{1,80}", provider["model"]):
        raise FallbackError("FALLBACK_MODEL_INVALID")
    key = provider.get("api_key")
    if not key and os.name!='nt':
        # QQ CLI video analysis is a separate process from the credential-loaded worker.
        vault=Path.home()/'.config/hakimi/credentials.fernet'
        if vault.is_file():
            from hakimi.core.host_secrets import crypt
            saved=json.loads(crypt(vault.read_bytes(),False))['provider']
            provider.update(api_key=saved['api_key'],base_url=saved['base_url'])
            key=provider['api_key']
    if not isinstance(key, str) or not 8 <= len(key) <= 4096 or "\n" in key:
        raise FallbackError("FALLBACK_CREDENTIAL_UNAVAILABLE")
    url = urlsplit(provider.get("base_url", ""))
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise FallbackError("FALLBACK_ENDPOINT_INVALID")
    return provider


def build_patch(runtime, provider, *, images=False):
    rows = [{"id": name, "disabled": name not in ENABLED_ROWS} for name in ALL_ROWS]
    if images:
        rows.append({"id": "attachment-local", "disabled": False, "config": {
            "maxImagesPerMessage": 2, "maxImageBytes": 524288, "maxMessageImageBytes": 1048576,
            "normalizedImageMaxPixels": 1000000, "imageCompressionConcurrency": 1}})
    rows.extend([
        {"id": "tools", "config": {"mode": "native", "maxParallelSubCalls": 1}},
        {"id": "system-prompt", "config": {
            "includeHarnessIdentity": False, "includeRuntimeContext": False,
            "personaSuffix": "",
            "personaPrefix": persona.text("dsh_runtime_prefix.txt")
        }},
        {"id": "sandbox-policy", "config": {"mode": "read-only", "workspaceRoot": str(runtime)}},
        {"id": "agent-default-model", "config": {"provider": provider["provider"], "model": provider["model"]}},
        {"id": "agent-loop", "config": {"agents": [], "maxParallelToolCalls": 1}},
        {"id": "llm-deepseek", "config": {"apiKeyEnv": "DEEPSEEK_API_KEY", "baseURL": provider["base_url"],
          "thinking": "disabled", "reasoningEffort": "off", "maxTokens": 2400,
          "streamIdleTimeoutMs": 60000, "filesApiTimeoutMs": 3000,
          "retryPolicy": {"mode": "normal", "maxRetries": 0}}},
        {"insert": [{"id": "qq-fallback-no-tools", "name": (runtime / "guard.mjs").as_uri(),
                     "inject": ["tools", "systemPrompt", "deepseekLlmApiExtensions"] + (["attachments"] if images else []),
                     "config": {"vision": bool(images)}}]},
        {"id": "headless-runner", "inject": ["headlessStartup", "qqFallbackNoTools"]},
    ])
    return rows


def prepare_runtime(base, installation, provider, *, images=None):
    """Prepare fixed runtime configuration; never touches business state files."""
    base = Path(base).resolve()
    runtime = _safe_local(base, RUNTIME_NAME)
    runtime.mkdir(exist_ok=True)
    # Empty trust layers prevent inherited home customizations and .env loading.
    _write_fixed(runtime, "cordis.patch.yml", "[]\n")
    _write_fixed(runtime, ".env", "")
    # No settings/credentials are persisted; reject unapproved leftover discovery.
    for name in ("settings.yaml", ".credentials.yaml"):
        if (runtime / name).exists():
            raise FallbackError("FALLBACK_UNEXPECTED_RUNTIME_CONFIG")
    _write_fixed(runtime, "guard.mjs", (Path(__file__).resolve().parent / "dsh_fallback_guard.mjs").read_text(encoding="utf-8"))
    media_path = _safe_local(runtime, "vision_input.json")
    media_path.unlink(missing_ok=True)
    if images:
        try:
            if not isinstance(images, list) or len(images) > 2:
                raise ValueError()
            for image in images:
                if (not isinstance(image, dict) or image.get("media_type") != "image/jpeg"
                        or not re.fullmatch(r"I[1-8]|R1", image.get("id", ""))
                        or not isinstance(image.get("data"), str) or len(image["data"]) > 700000
                        or len(base64.b64decode(image["data"], validate=True)) > 524288):
                    raise ValueError()
        except (ValueError, TypeError):
            raise FallbackError("FALLBACK_IMAGES_INVALID") from None
        _write_fixed(runtime, "vision_input.json", json.dumps(images, ensure_ascii=False, separators=(",", ":")))
    write_headless_profile(runtime, build_patch(runtime, provider, images=bool(images)))
    return runtime


def write_headless_profile(runtime, patch):
    """Write the reviewed bundle selection and a caller-specific headless patch."""
    manifest = {"name": "qq-dsh-headless-isolated", "private": True, "dependencies": {},
                "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless"], "patchReload": "startup"}}}
    _write_fixed(runtime, "profiles/headless/package.json", json.dumps(manifest))
    _write_fixed(runtime, "profiles/headless/cordis.patch.yml", json.dumps(patch, ensure_ascii=False, indent=2))
    _write_fixed(runtime, "profiles/headless/cordis.yml", "[]\n")
    _write_fixed(runtime, "profiles/headless/pnpm-workspace.yaml", "packages: []\n")


POWERSHELL_DRIVER = r"""
$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:DSH_PERMISSION_MODE = 'read-only'
$env:DSH_TELEMETRY_DISABLED = '1'
$env:DSH_TOOLS_MODE = 'native'
Set-Location -LiteralPath $env:QQ_FALLBACK_RUNTIME
$taskText = [Console]::In.ReadToEnd()
& $env:QQ_FALLBACK_DSH --profile headless $taskText
exit $LASTEXITCODE
"""


def _child_environment(runtime, installation, provider):
    env = {key: value for key, value in os.environ.items() if key.upper() in BASE_ENVIRONMENT_NAMES}
    env.update({"DSH_HOME": str(runtime), "DSH_PERMISSION_MODE": "read-only",
                "DSH_TELEMETRY_DISABLED": "1", "DSH_TOOLS_MODE": "native",
                "DEEPSEEK_API_KEY": provider["api_key"], "DEEPSEEK_BASE_URL": provider["base_url"],
                "QQ_FALLBACK_RUNTIME": str(runtime), "QQ_FALLBACK_DSH": str(installation["shim"]),
                "NO_COLOR": "1", "TERM": "dumb"})
    return env


class _WindowsJob:
    """Own the complete child tree, even after its initial PowerShell exits."""
    def __init__(self, process):
        if os.name != "nt":
            raise FallbackError("FALLBACK_WINDOWS_JOB_REQUIRED")
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount",
                        "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise FallbackError("FALLBACK_JOB_CREATE_FAILED")
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle)))):
            self.close()
            raise FallbackError("FALLBACK_JOB_ASSIGN_FAILED")

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.kernel.CloseHandle(handle):
                raise FallbackError("FALLBACK_JOB_CLOSE_FAILED")


class _PosixJob:
    def __init__(self,process):self.pid=process.pid

    def close(self):
        import signal
        try:os.killpg(self.pid,signal.SIGKILL)
        except ProcessLookupError:pass


def _stop_unassigned(process):
    # No stdin has been written yet, so the fixed PowerShell script cannot have
    # passed ReadToEnd or launched DSH. Terminate this one owned process only.
    try:
        process.kill()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass
    for pipe in (process.stdin, process.stdout, process.stderr):
        pipe.close()


def _invoke(prompt, runtime, installation, provider, timeout_seconds=None):
    """One fixed PowerShell command. Prompt goes through stdin, never shell code."""
    import base64
    deadline = time.monotonic() + (TIMEOUT_SECONDS if timeout_seconds is None else min(TIMEOUT_SECONDS, max(1, timeout_seconds)))
    command = base64.b64encode(POWERSHELL_DRIVER.encode("utf-16-le")).decode("ascii")
    process = subprocess.Popen(
        [installation["powershell"], "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=runtime, env=_child_environment(runtime, installation, provider),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        start_new_session=os.name!='nt',
    )
    # Assignment precedes the first stdin byte. PowerShell is blocked on
    # ReadToEnd and cannot launch the Node child before it belongs to the job.
    try:
        job = _WindowsJob(process) if os.name=='nt' else _PosixJob(process)
    except Exception as exc:
        _stop_unassigned(process)
        if isinstance(exc, FallbackError):
            raise
        raise FallbackError("FALLBACK_JOB_ASSIGN_FAILED") from exc
    output = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()
    io_failed = threading.Event()

    def drain(name, pipe):
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                remaining = MAX_OUTPUT_BYTES - len(output[name])
                if len(chunk) > remaining:
                    overflow.set()
                if remaining > 0:
                    output[name].extend(chunk[:remaining])
        except (OSError, ValueError):
            io_failed.set()
        finally:
            pipe.close()  # The owning I/O thread alone closes its buffered pipe.

    def write_prompt():
        try:
            process.stdin.write(prompt.encode("utf-8"))
            process.stdin.close()
        except (OSError, ValueError):
            io_failed.set()
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    workers = [threading.Thread(target=drain, args=(name, getattr(process, name)), daemon=True)
               for name in ("stdout", "stderr")]
    workers.append(threading.Thread(target=write_prompt, daemon=True))
    started = []
    failure = None
    try:
        for thread in workers:
            thread.start()
            started.append(thread)
        process.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        failure = "FALLBACK_TIMEOUT"
    except (OSError, RuntimeError):
        failure = "FALLBACK_PROCESS_FAILED"
    finally:
        # Always close the job, even if the parent exited successfully: orphaned
        # grandchildren may still hold stdin/stdout and must not survive.
        try:
            job.close()
        except Exception:
            failure = "FALLBACK_JOB_CLOSE_FAILED"
        cleanup_deadline = min(deadline, time.monotonic() + 2)
        for thread in started:
            thread.join(timeout=max(0, cleanup_deadline - time.monotonic()))
        # Never close a live worker's buffered pipe from this thread: close can
        # wait on its read/write lock indefinitely. Daemon workers own cleanup.
        if any(thread.is_alive() for thread in started):
            failure = failure or "FALLBACK_PIPE_CLEANUP_TIMEOUT"
    if failure:
        raise FallbackError(failure)
    if io_failed.is_set():
        raise FallbackError("FALLBACK_PROCESS_FAILED")
    if overflow.is_set():
        raise FallbackError("FALLBACK_OUTPUT_TOO_LARGE")
    if process.returncode != 0:
        raise FallbackError("FALLBACK_PROCESS_FAILED")
    try:
        return bytes(output["stdout"]).decode("utf-8-sig")
    except UnicodeError as exc:
        raise FallbackError("FALLBACK_OUTPUT_ENCODING") from exc


def normalize_reply_to(value):
    """Normalize a OneBot numeric message ID, including signed IDs, never CQ."""
    if type(value) not in (str, int):
        raise FallbackError("FALLBACK_REPLY_TARGET_INVALID")
    try:
        text = str(value)
        if not re.fullmatch(r"-?[0-9]{1,20}", text):
            raise ValueError("invalid message ID")
        return str(int(text))
    except (ValueError, OverflowError) as exc:
        raise FallbackError("FALLBACK_REPLY_TARGET_INVALID") from exc


def parse_response(raw):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise FallbackError("FALLBACK_RESPONSE_INVALID", "根节点必须是长度受限的 JSON 文本。")
    raw=raw.strip().lstrip("\ufeff")
    # A whole-response JSON fence is presentation only, never executable content.
    wrapped=re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```",raw,re.IGNORECASE)
    if wrapped:raw=wrapped.group(1).strip()

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                field = key if key in ('action','text','input','tool','question','reply_to') else 'JSON 对象'
                raise FallbackError("FALLBACK_RESPONSE_INVALID", f"{field} 出现重复字段，请各保留一次。")
            result[key] = value
        return result

    try:
        decoder=json.JSONDecoder(object_pairs_hook=pairs)
        result,end=decoder.raw_decode(raw)
        trailer=raw[end:].strip()
        # DSH/provider sometimes echoes its response-format descriptor after the answer.
        # Accept only that exact descriptor; never accept a second reply or tool request.
        if trailer and decoder.decode(trailer)!={'type':'json_object'}:
            raise ValueError('unexpected trailing output')
    except (ValueError, RecursionError) as exc:
        raise FallbackError("FALLBACK_JSON_DECODE_FAILED") from exc
    if isinstance(result, dict) and result.get('action') == 'tool':
        # Empty reply-only placeholders grant no capabilities. Keep all nonempty
        # extras invalid; never silently discard a second action or attachment.
        optional = {'reply_to','sticker','question','video_frames','memory','files','math','interaction','diary_export'}
        result = {k:v for k,v in result.items()
                  if k not in optional or v not in (None, [], '', {})}
        result.setdefault('text', '')
        issues = []
        if set(result) != {'action','text','tool','input'}:
            issues.append('Use exactly action,text,tool,input; omit reply-only fields.')
        if not isinstance(result.get('text'), str):
            issues.append('text must be a string.')
        elif len(result['text']) > 2000:
            issues.append('text fallback draft must be at most 2000 characters; put math in input.markdown for math.send.')
        if not isinstance(result.get('tool'), str):
            issues.append('tool must be one offered function name string.')
        if not isinstance(result.get('input'), dict):
            issues.append('input must be a JSON object, not a string, array or null.')
        if issues:
            error = FallbackError('FALLBACK_TOOL_INVALID')
            error.protocol_hint = ' '.join(issues)
            raise error
        return result
    if not isinstance(result, dict):
        raise FallbackError("FALLBACK_RESPONSE_INVALID", "根节点必须是对象，包含 action 和 text。")
    missing = {'action','text'} - result.keys()
    if missing:
        raise FallbackError("FALLBACK_RESPONSE_INVALID", "缺少必填字段：" + ', '.join(sorted(missing)))
    allowed = {"action","text","reply_to","sticker","question","video_frames","memory","files","math","interaction","diary_export","tool","input"}
    extra = result.keys() - allowed
    if extra:
        fields = ', '.join(k if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,31}',k) else '未识别字段' for k in sorted(extra)[:4])
        raise FallbackError("FALLBACK_RESPONSE_INVALID", "请移除不支持的顶层字段：" + fields)
    if result['action'] not in ('reply','silent','consult'):
        raise FallbackError("FALLBACK_RESPONSE_INVALID", "action 应为 reply、silent、consult 或 tool。")
    if not isinstance(result['text'], str):
        raise FallbackError("FALLBACK_RESPONSE_INVALID", "text 必须为字符串。")
    if any(key in result for key in ('tool','input')):
        raise FallbackError('FALLBACK_RESPONSE_INVALID', 'tool 和 input 只适用于 action=tool。')
    if result["action"] == "consult":
        result.pop("memory", None)
    if result["action"] == "reply":
        # Some providers emit JSON null for unused optional fields. Omission
        # grants no quote/media capability and should not discard a valid answer.
        result = {key: value for key, value in result.items()
                  if not (key in {"reply_to", "sticker", "video_frames", "files", "math"} and value is None)
                  and not (key in {"video_frames", "files"} and value == [])}
    text = result["text"]
    if result["action"] == "consult":
        if (set(result) != {"action", "text", "question"} or not isinstance(result["question"], str)
                or not 1 <= len(result["question"].strip()) <= 1000 or not 1 <= len(text.strip()) <= 2000):
            raise FallbackError("FALLBACK_CONSULT_INVALID")
    elif "question" in result:
        raise FallbackError("FALLBACK_RESPONSE_INVALID", "question 只适用于 action=consult。")
    if ((result["action"] == "silent" and text != "")
            or (result["action"] == "reply" and (len(text.strip()) > MAX_REPLY_TEXT_CHARS
                                                or (not text.strip() and "sticker" not in result and "files" not in result and "math" not in result and "diary_export" not in result)))):
        hint = 'silent 的 text 必须为空字符串。' if result['action']=='silent' else f'reply 的 text 最长 {MAX_REPLY_TEXT_CHARS} 字，无附件时必须有正文。'
        raise FallbackError("FALLBACK_RESPONSE_INVALID", hint)
    forbidden = r"(?:[A-Za-z]:[\\/]|\\\\|\[CQ:|```|DSH_PERMISSION_MODE|danger-full-access|Invoke-Expression|Start-Process|(?:^|\s)(?:powershell|pwsh|cmd\s*/c|python\s+-|dsh\s+--))"
    if re.search(forbidden, text, re.IGNORECASE) or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
        raise FallbackError("FALLBACK_RESPONSE_FORBIDDEN_CONTENT")
    response = {"action": result["action"], "text": text.strip()}
    if 'interaction' in result:
        if result['action']!='reply' or result['interaction'] not in ('normal','conflict','ai'):
            raise FallbackError('FALLBACK_INTERACTION_INVALID')
        response['interaction']=result['interaction']
    if 'diary_export' in result:
        if result['action']!='reply' or any(k in result for k in ('math','files','sticker','video_frames')):
            raise FallbackError('FALLBACK_DIARY_INVALID')
        from hakimi.features.diary_store import export_spec
        try:response['diary_export']=export_spec(result['diary_export'])
        except (ValueError,TypeError):raise FallbackError('FALLBACK_DIARY_INVALID') from None
    if 'math' in result:
        if result['action']!='reply':raise FallbackError('FALLBACK_MATH_INVALID')
        from hakimi.tools.math_render import validate
        try:response['math']=validate(result['math'])
        except ValueError as exc:raise FallbackError('FALLBACK_MATH_INVALID') from exc
    if "files" in result:
        if result["action"] != "reply":raise FallbackError("FALLBACK_FILES_INVALID")
        from hakimi.transport.file_delivery import validate_specs
        try:response["files"] = validate_specs(result["files"])
        except ValueError as exc:raise FallbackError("FALLBACK_FILES_INVALID") from exc
    if result["action"] in {"reply", "silent"} and "memory" in result:
        from hakimi.memory.smart_memory import valid_memory
        memory = valid_memory(result["memory"])
        if memory:
            response["memory"] = memory
    if result["action"] == "consult":
        response["question"] = result["question"].strip()
    if "reply_to" in result:
        if result["action"] != "reply":
            raise FallbackError("FALLBACK_RESPONSE_INVALID", "reply_to 只适用于 action=reply。")
        response["reply_to"] = normalize_reply_to(result["reply_to"])
    if "sticker" in result:
        name = result["sticker"]
        if (result["action"] != "reply" or not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.(?:jpg|jpeg|png|gif|webp)", name, re.IGNORECASE)):
            raise FallbackError("FALLBACK_STICKER_INVALID")
        response["sticker"] = name
    if "video_frames" in result:
        frames = result["video_frames"]
        if result["action"] != "reply" or not isinstance(frames,list) or not 1 <= len(frames) <= 2 or any(
                not isinstance(frame,str) or not re.fullmatch(r"F[1-6]",frame) for frame in frames):
            raise FallbackError("FALLBACK_VIDEO_FRAMES_INVALID")
        response["video_frames"] = list(dict.fromkeys(frames))
    return response


def run_fallback(prompt, base=BASE_DIR, *, timeout_seconds=None, images=None):
    """Return action/text and optional numeric reply_to; fixed error codes only."""
    if (not isinstance(prompt, str) or not 1 <= len(prompt.encode("utf-8")) <= MAX_PROMPT_BYTES
            or len(prompt) > MAX_PROMPT_CHARS):
        raise FallbackError("FALLBACK_PROMPT_INVALID")
    if not RUN_LOCK.acquire(blocking=False):
        raise FallbackError("FALLBACK_ALREADY_RUNNING")
    try:
        installation = discover_installation()
        provider = _provider(installation)
        runtime = prepare_runtime(base, installation, provider, images=images)
        from hakimi.prompt.prompt_language import prompt as positive_prompt
        task = 'Return one JSON object with action and text, using the tool protocol in the current task.\n'+positive_prompt(prompt)
        raw = (_invoke(task, runtime, installation, provider) if timeout_seconds is None else
               _invoke(task, runtime, installation, provider, timeout_seconds=timeout_seconds))
        return parse_response(raw)
    finally:
        if "runtime" in locals():
            (runtime / "vision_input.json").unlink(missing_ok=True)
        RUN_LOCK.release()
