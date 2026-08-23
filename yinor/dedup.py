"""实体自动去重：判定哪些重复实体对可安全自动合并。

策略——保守、可解释、低误报。把候选分两档：
  • auto_merge=True：归一化同名 / 分隔符差异 / 路径归一 / 域名 www 前缀 /
    中英对照（高相似）→ 可由后台或一键按钮自动合并
  • auto_merge=False：npm scope 包名不同（@suey/web≠@suey/gateway）、
    路径↔非路径（~/.pi≠pi）、事件↔对象（图谱建成≠图谱）等 → 仅生成候选，留人工确认

扫描逻辑（find_candidates）从 server 的 quality 端点统一收口于此，
quality 与 /api/dedup 共用同一份候选发现，避免分叉。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from .storage import Storage

# 与 server._NAME_NORM_RE 一致：非「单词字符/空白/CJK」及下划线 → 空格
NAME_NORM = re.compile(r"[^\w\s\u4e00-\u9fff]|_")
# 路径特征：含 \ 或 /、盘符开头（x:）、~ 开头
_PATHISH = re.compile(r"[\\/]|[a-z]:|^[~.].*[/\\]")
# npm scope 包名：@scope/name
_SCOPED_PKG = re.compile(r"^@[^/]+/([^/]+)$")
# 形似域名：xxx.yy（2+ 位 TLD），无空格无路径符
_DOMAINISH = re.compile(r"^[a-z0-9.-]{1,63}\.[a-z]{2,}$", re.IGNORECASE)
# 全分隔符（用于 alnum 比较：cy-router ≡ cyrouter）
_SEP = re.compile(r"[-_.\\/\s]")


# ---------- 基元判定 ----------


def norm_name(s: str) -> str:
    """归一化名称：lower + 标点/下划线转空格 + strip（与 quality 一致）。"""
    return NAME_NORM.sub(" ", (s or "").lower()).strip()


def alnum_only(s: str) -> str:
    """去掉所有分隔符（-_.\\/ 空白），仅留字母数字 CJK。"""
    return _SEP.sub("", (s or "").lower())


def is_path(s: str) -> bool:
    """是否像路径（含分隔符、盘符、~/. 开头）。"""
    return bool(_PATHISH.search((s or "").strip()))


def scoped_name(s: str) -> str | None:
    """@scope/name → name；非 scope 包返回 None。"""
    m = _SCOPED_PKG.match((s or "").strip())
    return m.group(1) if m else None


def is_ascii(s: str) -> bool:
    try:
        (s or "").encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def is_domain(s: str) -> bool:
    return bool(_DOMAINISH.match((s or "").strip()))


def _normpath(s: str) -> str:
    return (s or "").lower().replace("\\", "/").rstrip("/")


def _basename(s: str) -> str:
    return (s or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _strip_www(s: str) -> str:
    s = (s or "").lower()
    return s[4:] if s.startswith("www.") else s


# ---------- 核心判定 ----------


def should_auto_merge(a: str, b: str, sim: float | None) -> tuple[bool, str]:
    """判定实体 a、b 是否可安全自动合并。返回 (是否, 原因)。

    sim 为两者名称向量相似度证据；**None = 无向量证据**（如写入时层0 全量预匹配），
    此时依赖相似度的规则（同名路径项 / 中英对照）一律禁用——
    否则全量配对下"纯ASCII名 × 含CJK名"会被误并（实测 NGINX→GLM套餐 事故）。
    """
    a, b = a or "", b or ""

    # 排除1：npm scope 包——只要任一是 scope 包，必须包名完全相同才放行
    # （@suey/web 与 @suey/gateway 是不同实体；@suey/web 与 web 也保守跳过）
    sa, sb = scoped_name(a), scoped_name(b)
    if sa is not None or sb is not None:
        if sa != sb:
            return False, "scope包名不同"
        # 包名相同（@suey/web vs @SUEY/Web）→ 落到下面规则A判定

    # 排除2：路径 ↔ 非路径（~/.pi vs pi；E:\\dir vs dir）
    if is_path(a) != is_path(b):
        return False, "路径↔非路径"

    na, nb = norm_name(a), norm_name(b)
    # 规则A：归一化等价（大小写 / 标点 / 空格差异，如 cyRouter≡cyrouter、Nginx≡nginx）
    if na and na == nb:
        return True, "归一化同名"

    # 规则E：去全部分隔符后等价（cy-router ≡ cyrouter；mindmemos CLI 重名）
    aa, ab = alnum_only(a), alnum_only(b)
    if aa and aa == ab:
        return True, "分隔符差异"

    # 规则B：双方都是路径
    if is_path(a) and is_path(b):
        if _normpath(a) == _normpath(b):
            return True, "路径分隔符"
        # basename 相同 + 高相似（同项目不同根，如 junction/迁移后旧路径）
        if sim is not None and _basename(a) == _basename(b) and sim >= 0.93:
            return True, "同名路径项"

    # 规则C：双方都形似域名，去 www 前缀等价（www.dancher.net ≡ dancher.net）
    if is_domain(a) and is_domain(b) and _strip_www(a) == _strip_www(b):
        return True, "域名www前缀"

    # 规则D：中英对照（一方纯 ASCII，一方含 CJK）+ 高相似（需向量证据）
    if sim is not None and (is_ascii(a) ^ is_ascii(b)) and sim >= 0.95:
        return True, "中英对照"

    return False, ""


# ---------- 候选发现（统一收口，quality 与 dedup 共用） ----------


def find_candidates(
    storage: Storage,
    group_id: str | None = None,
    sim_threshold: float = 0.88,
) -> list[dict[str, Any]]:
    """发现同组实体重复候选对，每条带 auto_merge / auto_reason 标签。

    group_id=None 或 'all' 扫描全部分区；否则只扫该组。
    候选来源：①同组归一化同名；②同组名称向量相似度≥sim_threshold。
    keep = 引用（facts+mentions）多的实体。
    """
    st = storage
    if group_id and group_id != "all":
        groups = [group_id]
    else:
        groups = [
            r["group_id"]
            for r in st.conn.execute(
                "SELECT DISTINCT group_id FROM entities"
            ).fetchall()
        ]

    # 引用计数：facts（source/target）+ mentions，用于决定 keep/remove 方向
    refs = dict(
        st.conn.execute(
            """SELECT e.uuid,
                      (SELECT COUNT(*) FROM facts f
                       WHERE f.source_uuid = e.uuid OR f.target_uuid = e.uuid)
                    + (SELECT COUNT(*) FROM mentions m
                       WHERE m.entity_uuid = e.uuid) AS c
               FROM entities e""",
            (),
        ).fetchall()
    )

    def keep_first(a: str, b: str) -> tuple[str, str]:
        """引用多的保留为 keep。"""
        return (a, b) if refs.get(a, 0) >= refs.get(b, 0) else (b, a)

    out: list[dict[str, Any]] = []
    seen: set[frozenset[str]] = set()

    for g in groups:
        ents = st.get_all_entities(g)
        # 向量矩阵预载：供 alnum 桶显示真实相似度 + ③向量通道复用
        uuids_emb, mat = st.load_embeddings("entity", g)
        emb_idx = {u: i for i, u in enumerate(uuids_emb)}
        norms = np.linalg.norm(mat, axis=1) if len(uuids_emb) else np.array([])

        def pair_sim(
            a: str, b: str, emb_idx=emb_idx, mat=mat, norms=norms
        ) -> float | None:
            # 默认参数绑定当前轮的循环变量（B023）：闭包仅当轮使用，禁后期绑定歧义
            ia, ib = emb_idx.get(a), emb_idx.get(b)
            if ia is None or ib is None:
                return None
            try:
                s = float(mat[ia] @ mat[ib] / (norms[ia] * norms[ib] + 1e-9))
            except (TypeError, ValueError, IndexError):
                return None
            return s if np.isfinite(s) else None

        # ① 归一化同名
        by_norm: dict[str, list[str]] = {}
        for e in ents:
            by_norm.setdefault(norm_name(e.name), []).append(e.uuid)
        for _name, uuids in by_norm.items():
            if len(uuids) < 2:
                continue
            keep, remove = keep_first(uuids[0], uuids[1])
            pair = frozenset((keep, remove))
            if pair in seen:
                continue
            seen.add(pair)
            ke = st.get_entity(keep)
            re_ = st.get_entity(remove)
            kn = ke.name if ke else keep
            rn = re_.name if re_ else remove
            am, ar = should_auto_merge(kn, rn, 1.0)
            out.append(
                {
                    "keep_uuid": keep,
                    "remove_uuid": remove,
                    "keep_name": kn,
                    "remove_name": rn,
                    "group_id": g,
                    "similarity": 1.0,
                    "reason": "同名不同实体",
                    "auto_merge": am,
                    "auto_reason": ar,
                }
            )

        # ② alnum 确定性同名（零向量成本：分隔符差异但对子向量分略低于阈值时，
        #    ①③两条通道都会漏——实测 cy-router↔cyRouter sim=0.869 被 0.88 阈值挡在门外）
        by_alnum: dict[str, list[str]] = {}
        for e in ents:
            by_alnum.setdefault(alnum_only(e.name), []).append(e.uuid)
        for _key, uuids in by_alnum.items():
            if len(uuids) < 2:
                continue
            keep, remove = keep_first(uuids[0], uuids[1])
            pair = frozenset((keep, remove))
            if pair in seen:
                continue
            seen.add(pair)
            ke = st.get_entity(keep)
            re_ = st.get_entity(remove)
            kn = ke.name if ke else keep
            rn = re_.name if re_ else remove
            # sim=None：确定性桶不需要向量证据（D/B2 规则自动禁用）
            am, ar = should_auto_merge(kn, rn, None)
            ps = pair_sim(keep, remove)
            out.append(
                {
                    "keep_uuid": keep,
                    "remove_uuid": remove,
                    "keep_name": kn,
                    "remove_name": rn,
                    "group_id": g,
                    "similarity": round(ps, 3) if ps is not None else 1.0,
                    "reason": "分隔符差异",
                    "auto_merge": am,
                    "auto_reason": ar,
                }
            )

        # ③ 名称向量相似
        if len(uuids_emb) < 2:
            continue
        sims = (mat @ mat.T) / (norms[:, None] * norms[None, :] + 1e-9)
        n = len(uuids_emb)
        for i in range(n):
            for j in range(i + 1, n):
                try:
                    s = float(sims[i][j])
                except (TypeError, ValueError):
                    continue
                if not np.isfinite(s) or s < sim_threshold:
                    continue
                keep, remove = keep_first(uuids_emb[i], uuids_emb[j])
                pair = frozenset((keep, remove))
                if pair in seen:
                    continue
                seen.add(pair)
                ke = st.get_entity(keep)
                re_ = st.get_entity(remove)
                kn = ke.name if ke else keep
                rn = re_.name if re_ else remove
                am, ar = should_auto_merge(kn, rn, s)
                out.append(
                    {
                        "keep_uuid": keep,
                        "remove_uuid": remove,
                        "keep_name": kn,
                        "remove_name": rn,
                        "group_id": g,
                        "similarity": round(s, 3),
                        "reason": "名称高相似",
                        "auto_merge": am,
                        "auto_reason": ar,
                    }
                )
    return out


def run_auto_merge(
    storage: Storage,
    group_id: str | None = None,
    dry_run: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """发现并执行自动合并（仅 auto_merge=True 的候选）。

    dry_run=True 只返回候选不写库；否则逐对调 storage.merge_entities。
    单次 limit 防长事务；某对 remove 已被前序合并删除时跳过（下次定时清）。
    """
    cands = [c for c in find_candidates(storage, group_id) if c["auto_merge"]]
    preview = cands[:limit]
    if dry_run:
        return {"dry_run": True, "count": len(cands), "candidates": preview}

    merged: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for c in preview:
        try:
            r = storage.merge_entities(c["keep_uuid"], c["remove_uuid"])
            merged.append(
                {
                    **r,
                    "auto_reason": c["auto_reason"],
                    "keep_name": c["keep_name"],
                    "remove_name": c["remove_name"],
                }
            )
        except Exception as e:  # noqa: BLE001 —— 单对失败不中断整体
            errors.append({"remove_uuid": c["remove_uuid"], "error": str(e)})
    return {"dry_run": False, "merged": len(merged), "errors": errors, "detail": merged}


# ---------- 跨分区 same_as 链接（与组内合并互补） ----------


def find_link_candidates(storage: Storage) -> list[dict[str, Any]]:
    """跨分区实体链接候选：归一化 / alnum 桶中的跨组配对。

    与 find_candidates（组内重复→合并）互补：同一现实实体散在多个分区时，
    不合并（保留分区边界），而是建立 same_as 逻辑链接。
    auto_link=True 走 should_auto_merge 同款确定性规则（sim=None 无向量证据）。
    已存在的链接自动排除（幂等）。
    """
    st = storage
    rows = st.conn.execute("SELECT uuid, name, group_id FROM entities").fetchall()
    seen: set[frozenset[str]] = {
        frozenset((link["a_uuid"], link["b_uuid"])) for link in st.get_all_links()
    }
    out: list[dict[str, Any]] = []

    def collect(keyfn: Any, reason: str) -> None:
        buckets: dict[str, list[tuple[str, str, str]]] = {}
        for r in rows:
            buckets.setdefault(keyfn(r["name"]), []).append(
                (r["uuid"], r["name"], r["group_id"])
            )
        for _key, members in buckets.items():
            if len({m[2] for m in members}) < 2:
                continue  # 同组重复由 find_candidates（合并通道）处理
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    ua, na, ga = members[i]
                    ub, nb, gb = members[j]
                    if ga == gb:
                        continue
                    pair = frozenset((ua, ub))
                    if pair in seen:
                        continue
                    seen.add(pair)
                    am, ar = should_auto_merge(na, nb, None)
                    out.append(
                        {
                            "a_uuid": ua,
                            "b_uuid": ub,
                            "a_name": na,
                            "b_name": nb,
                            "a_group": ga,
                            "b_group": gb,
                            "reason": reason,
                            "auto_link": am,
                            "auto_reason": ar,
                        }
                    )

    collect(norm_name, "归一化同名")
    collect(alnum_only, "分隔符差异")
    return out


def run_auto_link(
    storage: Storage, dry_run: bool = False, limit: int = 200
) -> dict[str, Any]:
    """执行自动跨组链接（仅 auto_link=True 的候选）。幂等（add_link 是 OR IGNORE）。"""
    cands = [c for c in find_link_candidates(storage) if c["auto_link"]]
    if dry_run:
        return {"dry_run": True, "count": len(cands), "candidates": cands[:limit]}
    linked: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for c in cands[:limit]:
        try:
            storage.add_link(c["a_uuid"], c["b_uuid"])
            linked.append(c)
        except Exception as e:  # noqa: BLE001 —— 单对失败不中断整体
            errors.append({"a": c["a_uuid"], "b": c["b_uuid"], "error": str(e)})
    return {"dry_run": False, "linked": len(linked), "errors": errors, "detail": linked}
