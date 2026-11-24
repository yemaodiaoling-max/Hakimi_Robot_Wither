"""Command line entry: ``hakimi <command> [args...]`` (or ``python -m hakimi``).

Each command runs one module as ``__main__``, exactly as the historical
``python <module>.py`` invocation did.
"""
import runpy
import sys

COMMANDS = {
    "service": ("hakimi.ops.cloud_service", "bot services: agent | worker | help | diary"),
    "supervisor": ("hakimi.ops.cloud_supervisor", "health checks, Terra review, Sol repair"),
    "activity": ("hakimi.ops.cloud_activity", "operator activity views (status, recent, turns ...)"),
    "console": ("hakimi.console.memory_console", "memory and relationship console"),
    "embedding": ("hakimi.memory.memory_embedding_service", "local semantic embedding service"),
    "cli": ("hakimi.tools.qq_cli", "model-facing QQ tool entry (qq_cli)"),
    "lane": ("hakimi.transport.official_lane", "official QQ lane worker"),
    "help-service": ("hakimi.features.codex_help_service", "Codex expert help queue"),
    "diary": ("hakimi.features.diary_daily", "daily diary pass"),
    "release": ("hakimi.ops.release", "test, wait for an idle lane, restart services"),
    "shims": ("hakimi.ops.shims", "rewrite entry shims at historical script paths"),
    "config": ("hakimi.config", "instance configuration files: list | check | show"),
    "features": ("hakimi.features.registry", "feature registry and on/off state"),
    "smoke": ("hakimi.ops.cloud_smoke", "offline model/renderer checks"),
    "setup": ("hakimi.setup", "first-run setup: init | url | status | apply"),
    "gateway": ("hakimi.gateway", "local model gateway: init | serve | status"),
    "modules": ("hakimi.modules", "installed/enabled modules: qq, video, pet"),
}


def usage():
    width = max(map(len, COMMANDS))
    lines = ["usage: hakimi <command> [args...]", ""]
    lines += [f"  {name.ljust(width)}  {text}" for name, (_, text) in COMMANDS.items()]
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"} or argv[0] not in COMMANDS:
        print(usage(), file=sys.stderr if argv and argv[0] not in {"-h", "--help"} else sys.stdout)
        return 0 if not argv or argv[0] in {"-h", "--help"} else 2
    module = COMMANDS[argv[0]][0]
    sys.argv = [module] + argv[1:]
    runpy.run_module(module, run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
