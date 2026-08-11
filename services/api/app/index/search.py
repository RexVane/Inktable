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

import jieba

jieba.setLogLevel(60)  # 关掉 jieba 的构建日志，避免污染 sidecar stdout（端口协议走 stdout）


def segment_for_index(text: str) -> str:
    """建索引用：全切分，长词同时产出其子词。"""
    return " ".join(jieba.cut_for_search(text))


def segment_for_query(text: str) -> str:
    """查询用：同样全切分，保证与索引侧词元对齐。"""
    return " ".join(jieba.cut_for_search(text))


def quote_fts_query(text: str) -> str:
    """把任意用户输入包成 FTS5 短语查询。

    所有进入 MATCH 的字符串都必须经过这里 —— 不能靠调用方自觉。
    内部双引号按 FTS5 规则转义为两个双引号。
    """
    return '"' + text.replace('"', '""') + '"'


SCHEMA = """
-- 主索引：jieba 全切分后的文本
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='',
    tokenize='unicode61'
);

-- 副索引：原文，trigram 分词，兜底子串与编号
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts_tri USING fts5(
    text,
    content='',
    tokenize='trigram'
);
"""


def index_chunk(conn, chunk_id: int, text: str) -> None:
    """把一个 chunk 同时写入两个索引。rowid 对齐 chunks.id。"""
    conn.execute(
        "INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)",
        (chunk_id, segment_for_index(text)),
    )
    conn.execute(
        "INSERT INTO chunks_fts_tri(rowid, text) VALUES (?, ?)",
        (chunk_id, text),
    )


def search(conn, query: str, limit: int = 100) -> dict[str, list[tuple[int, float]]]:
    """双路检索，返回各路的 (chunk_id, bm25) 排名列表。

    不在这里融合 —— 融合是 §12.3b ④ 两级 RRF 的职责，
    它还要合并向量路的结果。
    """
    jieba_q = quote_fts_query(segment_for_query(query))
    raw_q = quote_fts_query(query)

    def run(table: str, q: str, k: int) -> list[tuple[int, float]]:
        try:
            return list(
                conn.execute(
                    f"SELECT rowid, bm25({table}) FROM {table} "
                    f"WHERE {table} MATCH ? ORDER BY bm25({table}) LIMIT ?",
                    (q, k),
                )
            )
        except Exception:
            return []  # 语法异常不该让整次查询失败，其余路照常

    return {
        "jieba": run("chunks_fts", jieba_q, limit),
        # trigram 是兜底路，深度大反而引入噪声（§12.3b ③）
        "trigram": run("chunks_fts_tri", raw_q, min(limit, 30)),
    }
