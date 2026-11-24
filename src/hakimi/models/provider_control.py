"""Local operator controls for the shared Gemini/dsh owner policy; no QQ messages."""
from hakimi import paths
import argparse
import json
from pathlib import Path

from hakimi.models.provider_health import ProviderHealth


def main():
    parser = argparse.ArgumentParser(description="本机切换哈基米生成服务；不改变正在处理的 dsh 任务")
    parser.add_argument("command",choices=("status","auto","dsh","gemini","pause-primary","resume-primary"))
    args = parser.parse_args()
    health = ProviderHealth(paths.root())
    if args.command in {"auto","dsh","gemini"}:
        health.set_mode(args.command)
    elif args.command=="pause-primary":
        health.pause_primary()
    elif args.command=="resume-primary":
        health.resume_primary()
    print(json.dumps(health.snapshot(),ensure_ascii=False))


if __name__=="__main__":
    main()
