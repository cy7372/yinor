"""黄金评测集回放：调 /search 算命中率与 MRR，量化检索质量。

用法（项目根目录）：
  python eval_search.py            # 跑全部 query，输出命中率/MRR/明细
  python eval_search.py -v         # 逐条打印命中详情
数据依赖：本地 data/yinor.db（评测集针对本地数据分布构建）。
"""

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

BASE = "http://127.0.0.1:20102"
HERE = Path(__file__).resolve().parent
QUERIES = HERE / "queries.yaml"
RERANK = "--rerank" in sys.argv  # 开 LLM 二段重排（延迟 +5~12s/查询，质量对比用）


def load_queries() -> list[dict]:
    try:
        with open(QUERIES, encoding="utf-8") as f:
            return yaml.safe_load(f)["queries"]
    except (OSError, KeyError, yaml.YAMLError) as e:
        sys.exit(f"评测集加载失败: {e}")


def search(q: str, group: str | None = None, limit: int = 20, rerank: bool = False) -> dict:
    params = {
        "q": q,
        "limit": str(limit),
        "rerank": "true" if rerank else "false",
    }
    if group and group != "all":
        params["group_id"] = group
    url = f"{BASE}/search?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        sys.exit(f"search 请求失败（服务在跑吗？{BASE}）: {e}")


def search_merged(
    q: str, groups: list[str], limit: int = 20, rerank: bool = False
) -> dict:
    """模拟 pi 扩展的双层检索：逐分区查询后按 score 合并去重。"""
    merged_eps: dict[str, dict] = {}
    facts_n = 0
    for g in groups:
        resp = search(q, group=g, limit=limit, rerank=rerank)
        facts_n += len(resp.get("facts", []))
        for h in resp.get("episodes", []):
            merged_eps.setdefault(h["uuid"], h)
    episodes = sorted(merged_eps.values(), key=lambda h: -h.get("score", 0.0))
    return {"episodes": episodes, "facts": [None] * facts_n}


def rank_of(expect: list[str], hits: list[dict]) -> int | None:
    """期望 uuid 在命中列表中的 1-based 排名；未命中 None。"""
    for i, h in enumerate(hits, 1):
        if h.get("uuid") in expect:
            return i
    return None


def evaluate(verbose: bool = False) -> None:
    queries = load_queries()
    hit_n = 0
    mrr_sum = 0.0
    lat_sum = 0.0
    rows = []
    for item in queries:
        q = item["q"]
        expect = item.get("expect_episodes") or []
        groups = item.get("groups") or ["default"]
        t0 = time.perf_counter()
        resp = search_merged(q, groups, rerank=RERANK)
        lat = (time.perf_counter() - t0) * 1000
        lat_sum += lat
        eps = resp.get("episodes", [])
        r = rank_of(expect, eps) if expect else None
        if expect:
            if r is not None:
                hit_n += 1
                mrr_sum += 1.0 / r
        rows.append(
            {
                "q": q,
                "note": item.get("note", ""),
                "rank": r,
                "hits": len(eps),
                "facts": len(resp.get("facts", [])),
                "ms": round(lat),
            }
        )
    total = len([x for x in queries if x.get("expect_episodes")])
    n_all = max(1, len(queries))
    print(f"查询数 {total}（另负例 {len(queries) - total}）")
    print(f"HitRate@20: {hit_n}/{total} = {hit_n / max(1, total):.2%}")
    print(f"MRR:        {mrr_sum / max(1, total):.3f}")
    print(f"平均延迟:   {lat_sum / n_all:.0f}ms")
    if verbose:
        for r in rows:
            mark = f"#{r['rank']}" if r["rank"] else (
                "—" if r["note"].startswith("负例") else "MISS"
            )
            print(
                f"  [{mark:>4}] {r['ms']:>5}ms eps={r['hits']:>2} | {r['q']}  ({r['note']})"
            )


if __name__ == "__main__":
    tag = " [rerank]" if RERANK else ""
    print(f"--- 模式{tag} ---")
    evaluate(verbose="-v" in sys.argv)
