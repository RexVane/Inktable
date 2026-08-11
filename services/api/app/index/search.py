"""中文全文检索 —— FTS5 双索引。

FTS5 默认 unicode61 分词器对中文**零命中**：整段汉字被当作单个 token，
查「保修」「验收」一律返回 0 条。方案 §9.1 定的解法是双索引：

    chunks_fts      jieba 全切分后写入 + unicode61   → 主索引，成词查询
    chunks_fts_tri  原文写入 + trigram                → 副索引，子串/编号/错别字

两路都查，结果并入 RRF 融合（§12.3b ④）。

M0 实测补充了两条修正，缺任何一条都会静默漏结果，详见 docs/M0-RESULTS.md：

1. **建索引必须用 cut_for_search 而非 cut**
   精确模式把「保修期」切成单个词元，查「保修」时 jieba 侧无此词元、
   trigram 侧因 2 字短于 3 字符组也不命中 —— 两路同时落空。
   全切分模式对「保修期」同时产出「保修」和「保修期」。

2. **查询串必须包裹成短语查询**
   FTS5 把 `-` 当 NOT 运算符，`HT-2024-0023` 这类编号会被解析成语法。
   而编号检索恰恰是引入 trigram 的主要理由。
"""

from __future__ import annotations

import sqlite3

import jieba

jieba.setLogLevel(60)  # 关掉 jieba 的构建日志，避免污染 sidecar stdout（端口协议走 stdout）


def segment_for_index(text: str) -> str:
    """建索引用：全切分，长词同时产出其子词。"""
    return " ".join(jieba.cut_for_search(text))


def segment_for_query(text: str) -> str:
    """查询用：同样全切分，保证与索引侧词元对齐。"""
    return " ".join(jieba.cut_for_search(text))


def quote_fts_query(text: str) -> str:
    """把一个词包成 FTS5 短语。

    短语引号有两个作用：
      1. 屏蔽 FTS5 语法字符（`-` 会被当成 NOT，`HT-2024-0023` 直接语法错）
      2. 要求内部词元**相邻**，保住多字词的完整性

    内部双引号按 FTS5 规则转义为两个双引号。
    """
    return '"' + text.replace('"', '""') + '"'


def build_fts_query(query: str, *, segment: bool) -> str:
    """把用户输入编译成 FTS5 表达式。

    **每个词各自成短语，词之间用 AND** —— 这是关键。

    曾经的写法是把整串包成一个短语，结果搜「庞贝蠕虫 共生菌」返回 0 条：
    短语要求所有词元在原文里连续出现，而这两个词中间隔着别的字。
    单独搜「庞贝」「蠕虫」却都能命中 —— 典型的"分开能搜、合起来搜不到"。

    现在：
        庞贝蠕虫 共生菌
      → "庞贝 蠕虫" AND "共生 共生菌"
        └ 词内相邻（保完整性）  └ 词间不限位置（AND）

    segment=True 走 jieba 主索引（词元需与索引侧对齐），
    False 走 trigram 副索引（直接用原文，兜底编号与未登录词）。
    """
    terms = [t for t in query.split() if t.strip()]
    if not terms:
        return ""

    parts = []
    for term in terms:
        piece = segment_for_query(term) if segment else term
        piece = piece.strip()
        if piece:
            parts.append(quote_fts_query(piece))

    return " AND ".join(parts)


# FTS5 建表语句的唯一来源是 app/db/schema.py（init_db 执行它）。
#
# 这里曾经也放了一份 —— 两份定义分叉过一次：给这份加了
# contentless_delete=1，而实际生效的是 schema.py 那份，改了等于没改。
# 单一来源，不再重复。



def index_chunk(conn, chunk_id: int, text: str, section_path: str = "") -> None:
    """把一个 chunk 同时写入两个索引。rowid 对齐 chunks.id。

    **标题路径必须一起进索引**：标题文字只存在 chunks.section_path 里，
    不在 text 里。实测「宋代五大名窑」作为 h1 标题时完全搜不到 ——
    用户搜自己文档的标题却没有结果，是最刺眼的一类检索盲区。

    前置而非追加：标题是最强的相关性信号，BM25 对靠前的词元没有偏好，
    但前置能让 snippet 生成时优先展示标题上下文。
    """
    indexed = f"{section_path}\n{text}" if section_path else text
    conn.execute(
        "INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)",
        (chunk_id, segment_for_index(indexed)),
    )
    conn.execute(
        "INSERT INTO chunks_fts_tri(rowid, text) VALUES (?, ?)",
        (chunk_id, indexed),
    )


def search(conn, query: str, limit: int = 100) -> dict[str, list[tuple[int, float]]]:
    """三路检索，返回各路的 (chunk_id, 分数) 排名列表。

    不在这里融合 —— 融合是 §12.3b ④ 两级 RRF 的职责，
    它还要合并向量路的结果。

    **为什么需要第三路（LIKE 子串）**：

    前两路都会在同一种情况下失效 —— 统计分词器切错专有名词的边界。
    实测 jieba 把「汝窑天青釉」切成 `汝窑天 / 青釉`：
      · jieba 路：索引里没有「汝窑」这个词元 → 0 命中
      · trigram 路：「汝窑」只有 2 字，短于 3 字符组 → 0 命中
    结果是一个明明在文档里的词完全搜不到。

    这不是换分词器能解决的（任何统计分词都会在未登录词上切错），
    也不是调参数能解决的（trigram 的 3 字符是其定义）。
    只能靠子串匹配兜底：慢，但保证不漏。

    LIKE 没有索引、是全表扫描，所以放在最后且只在前两路结果不足时才跑 ——
    正常查询走不到这里，代价为零。
    """
    jieba_q = build_fts_query(query, segment=True)
    raw_q = build_fts_query(query, segment=False)

    if not jieba_q and not raw_q:
        return {"jieba": [], "trigram": [], "substr": []}

    def run(table: str, q: str, k: int) -> list[tuple[int, float]]:
        if not q:
            return []
        try:
            return [
                (r[0], -r[1])  # bm25 越小越相关，取负统一为"越大越好"
                for r in conn.execute(
                    f"SELECT rowid, bm25({table}) FROM {table} "
                    f"WHERE {table} MATCH ? ORDER BY bm25({table}) LIMIT ?",
                    (q, k),
                )
            ]
        except sqlite3.Error:
            return []

    out = {
        "jieba": run("chunks_fts", jieba_q, limit),
        "trigram": run("chunks_fts_tri", raw_q, limit),
        "substr": [],
    }

    # 前两路结果太少时补子串兜底。
    #
    # 不是"全空才补"：分词切错时前两路可能命中少量**其他**分片
    # （比如查「汝窑」时 trigram 恰好在别处匹配到），此时正确结果
    # 仍然出不来。只要召回明显不足就补，代价是一次全表扫描。
    found = len({cid for r in (out["jieba"], out["trigram"]) for cid, _ in r})
    if found < min(3, limit):
        out["substr"] = _substring_search(conn, query, limit)

    return out


def _substring_search(conn, query: str, limit: int) -> list[tuple[int, float]]:
    """LIKE 子串兜底。所有词都必须出现（AND 语义，与 FTS5 路一致）。"""
    terms = [t for t in query.split() if t.strip()]
    if not terms:
        return []

    # 与 index_chunk 保持一致：标题路径也参与匹配
    where = " AND ".join(["(text LIKE ? OR section_path LIKE ?)"] * len(terms))
    params: list[str] = []
    for t in terms:
        params += [f"%{t}%", f"%{t}%"]
    try:
        rows = conn.execute(
            f"SELECT id FROM chunks WHERE {where} LIMIT ?", [*params, limit]
        ).fetchall()
    except sqlite3.Error:
        return []
    # 无相关性分数可用，按主键顺序给递减分，让 RRF 至少有个稳定排名
    return [(r[0], 1.0 / (i + 1)) for i, r in enumerate(rows)]
