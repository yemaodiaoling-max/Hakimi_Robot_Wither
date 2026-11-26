"""One bounded Codex CLI consultation, never a QQ sender or autonomous repairer."""
from hakimi import persona
from hakimi import paths
import json
import base64
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib

from hakimi.features.codex_help import project_root, scrub
from hakimi.models.fallback_runner import _WindowsJob, _PosixJob, _stop_unassigned

SCHEMA = {"type": "object", "additionalProperties": False,
          "properties": {"answer": {"type": "string", "maxLength": 2600},
                         "uncertainty": {"type": "string", "maxLength": 600}},
          "required": ["answer", "uncertainty"]}
DISABLED = ("shell_tool", "unified_exec", "apps", "plugins", "computer_use", "browser_use",
            "browser_use_external", "in_app_browser", "view_image", "image_generation", "multi_agent",
            "code_mode", "code_mode_host", "hooks", "plugin_hooks", "workspace_dependencies", "skill_search")
MAX_CAPTURE = 256 * 1024
DRIVER = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$taskText = [Console]::In.ReadToEnd()
$commandParts = ConvertFrom-Json -InputObject $env:QQ_CODEX_ARGV
$program = $commandParts[0]
$programArgs = @($commandParts | Select-Object -Skip 1)
$taskText | & $program @programArgs
exit $LASTEXITCODE
"""


class CodexHelpError(RuntimeError):
    pass


def installation():
    executable = shutil.which("codex")
    if os.name=='nt' and (not executable or Path(executable).suffix.lower() != ".exe"):
        options = list((Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/bin").glob("*/codex.exe"))
        executable = str(max(options, key=lambda p: p.stat().st_mtime)) if options else None
    if not executable:
        raise CodexHelpError("codex_not_installed")
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    model = None
    try:
        model = tomllib.loads((home / "config.toml").read_text(encoding="utf-8")).get("model")
    except (OSError, ValueError):
        pass
    if model is not None and (not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,80}", model)):
        raise CodexHelpError("invalid_local_model")
    return executable, home, model


def command(executable, runtime, model=None):
    args = [executable, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
            "--sandbox", "read-only", "--color", "never", "--json", "--cd", str(runtime),
            "--output-schema", str(runtime / "schema.json")]
    for name in DISABLED:
        args += ["--disable", name]
    for config in ('approval_policy="never"', 'web_search="disabled"', 'project_doc_max_bytes=0',
                   'model_reasoning_effort="medium"', 'model_verbosity="low"',
                   'features.skip_host_skill_discovery=true', 'model_provider="openai"'):
        args += ["-c", config]
    if model:
        args += ["--model", model]
    return args + ["-"]


def bounded_process(args, prompt, runtime, env, timeout):
    from hakimi.persona.prompt_language import prompt as positive_prompt
    prompt=positive_prompt(prompt)
    # The trusted wrapper blocks on stdin until its process tree belongs to our job.
    # Some native CLIs spawn children or exit before direct Job assignment completes.
    env = dict(env, QQ_CODEX_ARGV=json.dumps(args))
    shell = shutil.which("pwsh") or shutil.which("powershell")
    encoded = base64.b64encode(DRIVER.encode("utf-16-le")).decode("ascii")
    args = [shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    process = subprocess.Popen(args, cwd=runtime, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        start_new_session=os.name!='nt')
    try:
        job = _WindowsJob(process) if os.name=='nt' else _PosixJob(process)
    except Exception:
        _stop_unassigned(process)
        raise CodexHelpError("process_job_failed") from None
    output = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()

    def drain(name):
        pipe = getattr(process, name)
        try:
            while chunk := pipe.read(4096):
                remaining = MAX_CAPTURE-len(output[name])
                if len(chunk) > remaining:
                    overflow.set()
                output[name].extend(chunk[:max(0, remaining)])
        finally:
            pipe.close()

    def feed():
        try:
            process.stdin.write(prompt.encode("utf-8"))
            process.stdin.close()
        except (OSError, ValueError):
            pass

    workers = [threading.Thread(target=drain, args=(name,), daemon=True) for name in output]
    workers.append(threading.Thread(target=feed, daemon=True))
    deadline = time.monotonic()+timeout
    try:
        for worker in workers:
            worker.start()
        process.wait(timeout=max(.1, deadline-time.monotonic()))
    except subprocess.TimeoutExpired:
        raise CodexHelpError("codex_timeout") from None
    finally:
        job.close()
        for worker in workers:
            worker.join(timeout=max(0, min(1, deadline-time.monotonic())))
    if any(worker.is_alive() for worker in workers) or overflow.is_set():
        raise CodexHelpError("codex_output_limit")
    if process.returncode:
        error = CodexHelpError("codex_process_failed")
        error.local_diagnostic = scrub(output["stderr"].decode("utf-8", errors="replace")[-4000:], 2500)
        raise error
    return output["stdout"].decode("utf-8", errors="strict")


def parse_events(raw):
    result, usage = None, {}
    completed = False
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item", {})
        if item.get("type") in {"command_execution", "mcp_tool_call", "web_search", "file_change"}:
            raise CodexHelpError("unexpected_tool_use")
        if event.get("type") in {"turn.failed", "error"}:
            raise CodexHelpError("codex_turn_failed")
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            try:
                result = json.loads(item.get("text", ""))
            except ValueError:
                result = None
        if event.get("type") == "turn.completed":
            completed, usage = True, event.get("usage", {})
    if (not completed or not isinstance(result, dict) or set(result) != {"answer", "uncertainty"}
            or not isinstance(result["answer"], str) or not 1 <= len(result["answer"].strip()) <= 2600
            or not isinstance(result["uncertainty"], str) or len(result["uncertainty"]) > 600):
        raise CodexHelpError("codex_result_invalid")
    return {key: scrub(value, 2600 if key == "answer" else 600) for key, value in result.items()}, usage


def _schema_error(value, schema, path="$"):
    """Small, strict JSON-schema validator for host-controlled output shapes."""
    if not isinstance(schema, dict):
        return None
    expected = schema.get("type")
    if expected is not None:
        options = expected if isinstance(expected, list) else [expected]
        valid = False
        for option in options:
            if option == "object":
                valid = isinstance(value, dict)
            elif option == "array":
                valid = isinstance(value, list)
            elif option == "string":
                valid = isinstance(value, str)
            elif option == "integer":
                valid = type(value) is int
            elif option == "number":
                valid = type(value) in (int, float) and not isinstance(value, bool)
            elif option == "boolean":
                valid = type(value) is bool
            elif option == "null":
                valid = value is None
            if valid:
                break
        if not valid:
            return path + ":type"
    if "const" in schema and value != schema["const"]:
        return path + ":const"
    if "enum" in schema and value not in schema["enum"]:
        return path + ":enum"
    if isinstance(value, str):
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            return path + ":maxLength"
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            return path + ":minLength"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return path + ":minimum"
        if "maximum" in schema and value > schema["maximum"]:
            return path + ":maximum"
    if isinstance(value, list):
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            return path + ":maxItems"
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            return path + ":minItems"
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                error = _schema_error(item, schema["items"], path + "[" + str(index) + "]")
                if error:
                    return error
    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        for key in required:
            if key not in value:
                return path + ":missing:" + str(key)
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                return path + ":additional:" + sorted(extra)[0]
        for key, child in properties.items():
            if key in value:
                error = _schema_error(value[key], child, path + "." + key)
                if error:
                    return error
    return None


def parse_structured_events(raw, schema):
    result, usage = None, {}
    completed = False
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item", {})
        if item.get("type") in {"command_execution", "mcp_tool_call", "web_search", "file_change"}:
            raise CodexHelpError("unexpected_tool_use")
        if event.get("type") in {"turn.failed", "error"}:
            raise CodexHelpError("codex_turn_failed")
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            try:
                result = json.loads(item.get("text", ""))
            except ValueError:
                result = None
        if event.get("type") == "turn.completed":
            completed, usage = True, event.get("usage", {})
    if not completed or not isinstance(result, dict):
        raise CodexHelpError("codex_result_invalid")
    error = _schema_error(result, schema)
    if error:
        raise CodexHelpError("codex_result_invalid:" + error)
    return result, usage


def run_codex_structured(prompt, base, schema, *, model="gpt-6.1-terra", timeout=90,
                         max_prompt_chars=20000):
    """Run the same read-only CLI with a host-supplied JSON schema.

    Existing ``SCHEMA`` consumers keep using ``run_codex``/``parse_events``;
    this entry point does not change their contract.
    """
    if not isinstance(prompt, str) or not 1 <= len(prompt) <= max_prompt_chars:
        raise CodexHelpError("codex_prompt_limit")
    if not isinstance(schema, dict):
        raise CodexHelpError("codex_schema_invalid")
    try:
        executable, home, _configured = installation()
    except CodexHelpError as exc:
        native = paths.codex_executable()
        if os.name == 'nt' or str(exc) != 'codex_not_installed' or not native.is_file():
            raise
        executable = str(native)
        home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    parent = project_root(base) / "state/codex_help_runtime"
    parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="terra_", dir=parent) as directory:
        runtime = Path(directory)
        (runtime / "schema.json").write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
        names = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "APPDATA",
                 "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "LANG", "LC_ALL",
                 "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
        env = {key: value for key, value in os.environ.items() if key.upper() in names}
        env.update(CODEX_HOME=str(home), NO_COLOR="1", PYTHONUTF8="1")
        raw = bounded_process(command(executable, runtime, model), prompt, runtime, env, timeout)
        return parse_structured_events(raw, schema)


def fault_sources(brief, base):
    """Only explicit source files, never logs, business state, settings or paths from chat."""
    allowed = {"fallback_runner.py", "fallback_supervisor.py", "bridge_daemon.py", "official_lane.py",
               "official_transport.py", "official_routes.py", "official_api.py", "official_test.py", "fallback_commit.py", "fallback_prompt.py", "turn_coordinator.py", "delivery_guard.py"}
    snippets = []
    for frame in brief.get("frames", [])[-4:]:
        name, line = frame.get("file"), frame.get("line")
        if name not in allowed or type(line) is not int:
            continue
        path = paths.source_file(name)
        if path is None:
            continue
        if path.is_symlink():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            snippets.append({"file": name, "line": line, "source": "\n".join(lines[max(0,line-5):line+4])[:1600]})
        except OSError:
            pass
    return snippets


def run_codex(brief, base, timeout=70):
    executable, home, model = installation()
    parent = project_root(base) / "state/codex_help_runtime"
    parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="request_", dir=parent) as directory:
        runtime = Path(directory)
        (runtime / "schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
        data = dict(brief)
        if data["kind"] == "fault":
            data["trusted_source_excerpts"] = fault_sources(brief, base)
        prompt = (persona.text("ops/codex_analyst.txt")
                  + json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        if len(prompt.encode()) > 28000:
            raise CodexHelpError("codex_prompt_limit")
        names = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "APPDATA",
                 "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "LANG", "LC_ALL",
                 "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
        env = {key: value for key, value in os.environ.items() if key.upper() in names}
        env.update(CODEX_HOME=str(home), NO_COLOR="1", PYTHONUTF8="1")
        return parse_events(bounded_process(command(executable, runtime, model), prompt, runtime, env, timeout))
