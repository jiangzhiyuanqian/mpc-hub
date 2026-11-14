"""命令行入口。

    mpc gui          打开桌面设置窗口（填 Key、选平台、测对话）
    mpc chat         在终端里直接跟客服 agent 对话
    mpc serve        启动客服后台（默认只监听本机）
    mpc selftest     离线跑通全链路自检（不连任何真实平台）
    mpc capabilities 打印当前能力矩阵
    mpc audit        查看最近审计流水
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .connectors import LoopbackConnector, Registry
from .contracts import Envelope, Platform
from .hub import Hub
from .store import Store

DEFAULT_DB = os.getenv("MPC_DB", "data/mpc.db")


def _hub(db_path: str = DEFAULT_DB) -> Hub:
    registry = Registry()
    registry.register(LoopbackConnector(), "loopback")
    return Hub(store=Store(db_path), registry=registry)


def cmd_gui(args: argparse.Namespace) -> int:
    from .desktop import launch

    return launch()


def cmd_chat(args: argparse.Namespace) -> int:
    """终端对话：用配置里的 Key 走真实模型；没填 Key 时走离线 Mock。"""
    from .agent.chat import ConsoleChat
    from .runtime import build_runtime

    runtime = build_runtime(db_path=args.db)
    llm = runtime.llm
    print("[mpc] 终端对话模式。输入内容回车发送；/history 看历史，/clear 清空，/exit 退出。")
    if not runtime.settings.llm.api_key and runtime.settings.llm.provider != "ollama":
        print("[mpc] 提示：还没填 API Key，现在用的是离线 Mock 回复（不会联网）。")
        print("      填 Key：mpc gui  →  「模型接口」页 → 保存配置。")
    print(f"[mpc] 配置：{runtime.settings.llm.provider} / {runtime.settings.llm.model}\n")

    chat = ConsoleChat(runtime.store, runtime.governance, llm)
    while True:
        try:
            line = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in {"/exit", "/quit"}:
            break
        if line == "/history":
            for row in chat.history(limit=20):
                print(f"  [{row.get('direction')}] {row.get('text', '')[:120]}")
            print()
            continue
        if line == "/clear":
            chat.clear()
            print("已清空。\n")
            continue
        result = chat.reply(line)
        reply = result.get("reply") or ""
        if reply:
            print(f"客服 > {reply}")
        else:
            print(f"（未回复：{result.get('reason', result.get('status'))}）")
        print()
    runtime.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .server.app import create_app

    os.environ.setdefault("MPC_DB", args.db)
    app = create_app()
    print(f"[mpc] 客服后台启动：http://{args.host}:{args.port}  (db={args.db})")
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """离线自检：走完 入站→快判→生成→闸门→发送 全链路。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "selftest.db")
        store = Store(db)
        registry = Registry()
        loopback = LoopbackConnector()
        registry.register(loopback, "loopback")
        hub = Hub(store=store, registry=registry)

        checks: list[tuple[str, bool, str]] = []

        def check(name: str, ok: bool, detail: str = "") -> None:
            checks.append((name, ok, detail))
            print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  → ' + detail) if detail else ''}")

        def env(text: str, mid: str) -> Envelope:
            return Envelope(
                message_id=mid,
                platform=Platform.LOOPBACK,
                conversation_id="selftest",
                sender_id="user-1",
                sender_name="测试用户",
                text=text,
            )

        # 1) 普通提问 → 应生成并发出
        r1 = hub.ingest(env("这个产品怎么用？", "m1"))
        check("普通提问：生成回复并发送", r1["status"] == "sent" and len(loopback.outbox) == 1, r1["status"])

        # 2) 纯问候 → 不应回
        r2 = hub.ingest(env("你好", "m2"))
        check("纯问候：不回复", r2["status"] in {"dropped", "no_reply"}, r2["status"])

        # 3) 承诺性话术 → 挂起审批
        r3 = hub.ingest(env("你直接转账给我，我先给你下单", "m3"))
        pending = hub.pending_approvals()
        check("承诺性话术：挂起等人工确认", r3["status"] == "held" and len(pending) == 1, r3["status"])

        # 4) 人工放行 → 真的发出去
        appr = hub.approve(pending[0]["approval_id"], approved=True, edited_text="已人工核实，走线下流程。") if pending else {}
        check("人工放行：消息发出", appr.get("status") == "sent", str(appr))

        # 5) 重复消息 → 幂等丢弃
        hub.ingest(env("重复消息测试", "m4"))
        r5 = hub.ingest(env("重复消息测试", "m4"))
        check("重复消息：幂等忽略", r5["status"] == "duplicate", r5["status"])

        # 6) 出站硬规则：超长 / 凭据泄漏
        base = env("规则校验", "m5")
        v_len = hub.gov.check_outbound(base, "啊" * 1200)
        check("出站闸：超长回复被拦", v_len.decision.value == "drop", v_len.reason)
        v_sec = hub.gov.check_outbound(base, "你的 key 是 sk-abcdefghijklmnopqrstuvwx")
        check("出站闸：凭据泄漏被拦", v_sec.decision.value == "drop", v_sec.reason)

        # 7) 审计与指标
        audit_n = len(store.audit_recent(limit=200))
        check("审计留痕", audit_n > 0, f"{audit_n} 条")
        print(f"  计数: {hub.counters}")
        store.close()

        failed = [c for c in checks if not c[1]]
        print(f"[mpc] 自检结果：{len(checks) - len(failed)}/{len(checks)} 通过")
        if failed:
            return 1
    print("[mpc] 自检通过：全链路可用")
    return 0


def cmd_capabilities(args: argparse.Namespace) -> int:
    hub = _hub(args.db)
    print(json.dumps(hub.capabilities(), ensure_ascii=False, indent=2))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    store = Store(args.db)
    for row in reversed(store.audit_recent(limit=args.limit)):
        print(
            f"{row['created_at']:.0f} [{row['platform']}/{row['conversation_id']}] "
            f"{row['stage']} -> {row['decision']}  {row['reason']}"
        )
    store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mpc", description="多平台连接中心 · 独立客服后台")
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite 数据库路径")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("gui", help="打开桌面设置窗口（填 Key / 选平台 / 测对话）")
    s.set_defaults(func=cmd_gui)

    s = sub.add_parser("chat", help="在终端里跟客服 agent 对话")
    s.set_defaults(func=cmd_chat)

    s = sub.add_parser("serve", help="启动 HTTP 后台")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8760)
    s.add_argument("--log-level", default="info")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("selftest", help="离线全链路自检")
    s.set_defaults(func=cmd_selftest)

    s = sub.add_parser("capabilities", help="打印能力矩阵")
    s.set_defaults(func=cmd_capabilities)

    s = sub.add_parser("audit", help="查看审计流水")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(func=cmd_audit)
    return p


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 ANSI 码页，日志重定向到文件时会写成 GBK；
    # 这里统一按 UTF-8 输出，日志文件才能被正常读取与检索。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
