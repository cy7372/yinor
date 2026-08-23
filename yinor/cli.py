"""yinor CLI：add / search / history / stats / serve。

用法:
  python -m yinor.cli add "对话内容" [--group xxx] [--no-extract]
  python -m yinor.cli search "查询"
  python -m yinor.cli history "实体名"
  python -m yinor.cli stats [--group xxx]
  python -m yinor.cli serve [--port 8600]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging


def _get_memory(args: argparse.Namespace):
    from .llm import LLMClient
    from .memory import Memory

    llm = LLMClient()
    return Memory(db_path=args.db, llm=llm, default_group=args.group)


async def cmd_add(args: argparse.Namespace) -> None:
    mem = _get_memory(args)
    ep = await mem.add_episode(
        content=args.content,
        name=args.name,
        source=args.source,
        source_description=args.source_desc or "",
        extract=not args.no_extract,
        update_summary=not args.no_summary,
    )
    print(f"[OK] episode {ep.uuid}")
    print(f"  name: {ep.name}")
    print(f"  stats: {mem.stats(args.group)}")
    mem.close()


async def cmd_search(args: argparse.Namespace) -> None:
    from .memory import fmt_search

    mem = _get_memory(args)
    resp = await mem.search(args.query, limit=args.limit)
    print(fmt_search(resp))
    mem.close()


async def cmd_history(args: argparse.Namespace) -> None:
    mem = _get_memory(args)
    results = await mem.history(args.entity, limit=args.limit)
    for r in results:
        window = ""
        if r.valid_at:
            window = " [" + r.valid_at[:10]
            window += " → " + (r.invalid_at[:10] if r.invalid_at else "现在") + "]"
        marker = "已失效" if r.invalid_at else "有效  "
        print(f"  {marker} {r.fact}{window}")
    mem.close()


def cmd_stats(args: argparse.Namespace) -> None:
    mem = _get_memory(args)
    print(json.dumps(mem.stats(args.group), ensure_ascii=False, indent=2))
    mem.close()


def cmd_serve(args: argparse.Namespace) -> None:
    """启动 FastAPI 本地服务（供 pi 扩展调用）。"""
    import uvicorn

    uvicorn.run("yinor.server:app", host=args.host, port=args.port, log_level="info")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="yinor 记忆系统")
    parser.add_argument("--db", default=None, help="数据库路径（默认 data/yinor.db）")
    parser.add_argument("--group", default="default", help="group_id 分区")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="摄入一条信息")
    p_add.add_argument("content", help="内容文本")
    p_add.add_argument("--name", default=None, help="episode 名称")
    p_add.add_argument("--source", default="text", help="来源类型 text/json/message")
    p_add.add_argument("--source-desc", default="", help="来源描述")
    p_add.add_argument("--no-extract", action="store_true", help="只存 episode 不提取")
    p_add.add_argument("--no-summary", action="store_true", help="跳过 summary 更新")
    p_add.set_defaults(func=cmd_add)

    p_search = sub.add_parser("search", help="混合检索")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.set_defaults(func=cmd_search)

    p_hist = sub.add_parser("history", help="实体演变史")
    p_hist.add_argument("entity")
    p_hist.add_argument("--limit", type=int, default=20)
    p_hist.set_defaults(func=cmd_history)

    p_stats = sub.add_parser("stats", help="统计")
    p_stats.set_defaults(func=cmd_stats)

    p_serve = sub.add_parser("serve", help="启动本地服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=20102)
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    if args.db is None:
        from .memory import DEFAULT_DB_PATH

        args.db = DEFAULT_DB_PATH

    cmd = args.func
    if cmd in (cmd_add, cmd_search, cmd_history):
        asyncio.run(cmd(args))
    else:
        cmd(args)


if __name__ == "__main__":
    main()
