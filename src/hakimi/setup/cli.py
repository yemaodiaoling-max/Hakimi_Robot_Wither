"""``hakimi setup`` command line."""
from __future__ import annotations

import argparse
import json
import sys

from hakimi.setup import settings as st

DEFAULT_PORT = 18768


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hakimi setup", description="首次安装与统一设置")
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init", help="创建实例目录、本地网关证书和面板令牌（不会覆盖已有文件）")
    init.add_argument("--modules", default="", help="逗号分隔：qq,video,pet")
    url = sub.add_parser("url", help="打印设置面板地址（含本地令牌，请勿外传）")
    url.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("status", help="显示已配置项（不显示任何密钥）")
    apply = sub.add_parser("apply", help="把已保存的设置重新写入各模块配置")
    apply.add_argument("--no-prepare", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "init":
        result = st.init_instance()
        if args.modules:
            from hakimi import modules
            result["modules"] = modules.save([m.strip() for m in args.modules.split(",") if m.strip()])
            s = st.load()
            s["modules"] = result["modules"]
            st.save(s)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("\n下一步：启动面板 `hakimi console --base <实例>/official_test_runtime`，然后运行 `hakimi setup url`。")
        return 0
    if args.action == "url":
        from hakimi.console.console_auth import load_or_create_token
        token = load_or_create_token(st.CONSOLE_TOKEN)
        print(f"http://127.0.0.1:{args.port}/setup.html#token={token}")
        print("（远程服务器请先建立 SSH 隧道：ssh -L {0}:127.0.0.1:{0} 用户@服务器）".format(args.port), file=sys.stderr)
        return 0
    if args.action == "status":
        print(json.dumps(st.summary(), ensure_ascii=False, indent=2))
        return 0
    s = st.load()
    report = st.apply(s, {}, run_prepare=not args.no_prepare)
    for row in report:
        print(("✓ " if row["ok"] else "✗ ") + row["step"] + "：" + str(row["detail"]))
    return 0 if all(r["ok"] for r in report) else 1


if __name__ == "__main__":
    sys.exit(main())
