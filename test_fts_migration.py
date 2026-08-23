"""FTS 迁移测试：在真实库副本上验证 trigram 重构（schema 迁移 + 中文召回 + 写入同步）。"""

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from yinor.models import Episode
from yinor.storage import Storage, fts_text

src = Path("data/yinor.db")
dst = Path(tempfile.gettempdir()) / "yinor_mig_test.db"
shutil.copy(src, dst)

# ── 迁移前状态 ──
raw = sqlite3.connect(dst)
trigs = raw.execute(
    "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'"
).fetchone()[0]
old_sql = raw.execute(
    "SELECT sql FROM sqlite_master WHERE name='facts_fts'"
).fetchone()[0]
print(f"[迁移前] 触发器={trigs}, facts_fts 含 content=: {'content=' in old_sql}")
raw.close()

# ── 触发迁移（Storage 初始化即迁移）──
st = Storage(dst)
raw = sqlite3.connect(dst)
trigs_after = raw.execute(
    "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'"
).fetchone()[0]
new_sql = raw.execute(
    "SELECT sql FROM sqlite_master WHERE name='facts_fts'"
).fetchone()[0]
fts_counts = {
    t: raw.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    for t in ("facts_fts", "entities_fts", "episodes_fts")
}
main_counts = {
    t: raw.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    for t in ("facts", "entities", "episodes")
}
print(f"[迁移后] 触发器={trigs_after}, facts_fts 含 content=: {'content=' in new_sql}")
print(f"[行数] fts={fts_counts}")
print(f"[行数] 主表={main_counts}")
assert trigs_after == 0, "触发器未清干净"
assert "content=" not in new_sql, "FTS 表未转 standalone"
assert list(fts_counts.values()) == list(main_counts.values()), "FTS 行数与主表不一致"
raw.close()

# ── 中文召回验证 ──
hits = st.fts_search_facts("端口", "default", limit=5)
print(f"[中文召回] facts MATCH '端口' → {len(hits)} 条")
hits2 = st.fts_search_episodes("图谱", "default", limit=5)
print(f"[中文召回] episodes MATCH '图谱' → {len(hits2)} 条")
hits3 = st.fts_search_entities("记忆", "default", limit=5)
print(f"[中文召回] entities MATCH '记忆' → {len(hits3)} 条")

# ── 预切分单测 ──
assert fts_text("知识图谱") == "知 识 图 谱"
assert fts_text("端口20102") == "端 口 20102"
assert fts_text("cyRouter重启") == "cyRouter 重 启"
assert fts_text("") == ""
print("[fts_text] 单测全过")

# ── 写入同步验证：新 episode 入库后 FTS 即时可查 ──
import uuid
from datetime import datetime, timezone

ep = Episode(
    uuid=str(uuid.uuid4()),
    name="fts-sync-test",
    group_id="default",
    source="text",
    source_description="t",
    content="折叠按钮的配色方案是青绿色",
    created_at=datetime.now(timezone.utc).isoformat(),
    valid_at=None,
)
st.upsert_episode(ep)
sync_hits = st.fts_search_episodes("配色方案", "default", limit=5)
assert any(u == ep.uuid for u, _ in sync_hits), "新写入 episode 未同步进 FTS"
print("[写入同步] 新 episode 中文短语即时召回 ✓")

# 删除同步
st.delete_episode(ep.uuid)
sync_hits2 = st.fts_search_episodes("配色方案", "default", limit=5)
assert not any(u == ep.uuid for u, _ in sync_hits2), "删除后 FTS 仍残留"
print("[删除同步] 删除后 FTS 无残留 ✓")

# 幂等：二次初始化不再触发迁移
st2 = Storage(dst)
print("[幂等] 二次初始化无异常 ✓")

st.close()
st2.close()
print("\n全部通过")
