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

    **每个词各自成短语，词之间用 AND**。

    两轮实测教训：

    ① 曾把整串包成一个短语 → 搜「庞贝蠕虫 共生菌」返回 0 条：
       短语要求所有词元在原文里连续出现，而这两个词中间隔着别的字。
       单独搜任一个都能命中 —— 典型的"分开能搜、合起来搜不到"。

    ② 改成按空格切词后仍有同样问题，只是场景更隐蔽：
       **中文句子没有空格**。「银行家算法用到哪些数据结构」被当成一个词，
       要求这 12 个字连续出现 → 0 条。而用户输入的恰恰就是整句自然语言。
       所以对超过 CJK_SPLIT_LEN 的中文串必须先分词再 AND。

    分词后的词元用 OR 而非 AND：自然语言问句里含大量非检索词
    （「哪些」「怎么」「为什么」），全部 AND 会把结果清零。
    OR + BM25 排序天然让命中词多的片排前面。
    """
    terms = [t for t in query.split() if t.strip()]
    if not terms:
        return ""

    parts = []
    for term in terms:
        # 长中文串先切成词，否则整串会被当作一个必须连续出现的短语
        if _needs_split(term):
            words = [w for w in segment_for_query(term).split() if _is_meaningful(w)]
            if words:
                # 同一个原始 term 内部用 OR：问句里的虚词不该成为必要条件
                inner = " OR ".join(quote_fts_query(w) for w in words)
                parts.append(f"({inner})")
                continue

        piece = segment_for_query(term) if segment else term
        piece = piece.strip()
        if piece:
            parts.append(quote_fts_query(piece))

    return " AND ".join(parts)


# 超过这个长度的连续中文串视为"句子"，需要先分词
CJK_SPLIT_LEN = 6

# 中文停用词：出现在几乎所有文档里，作为检索条件毫无区分度，
# 还会把 OR 的结果集撑到全库。
_STOPWORDS = {
    "的", "了", "和", "是", "在", "有", "为", "与", "及", "或", "也", "都",
    "这", "那", "哪", "些", "哪些", "什么", "怎么", "怎样", "如何", "为什么",
    "可以", "能否", "是否", "多少", "几个", "一个", "我们", "你们", "他们",
    "对于", "关于", "根据", "通过", "由于", "因为", "所以", "但是", "而且",
    "需要", "应该", "可能", "已经", "还是", "以及", "其中", "同时",
}


def _needs_split(term: str) -> bool:
    """判断是否是需要分词的中文长串。"""
    if len(term) <= CJK_SPLIT_LEN:
        return False
    cjk = sum(1 for ch in term if "一" <= ch <= "鿿")
    return cjk >= CJK_SPLIT_LEN


def _is_meaningful(word: str) -> bool:
    """过滤停用词与单字虚词。

    单个汉字通常区分度太低（且 trigram 路已覆盖子串匹配），
    但保留单个字母数字（如型号里的 "Ni"、"8M"）。
    """
    if word in _STOPWORDS:
        return False
    if len(word) == 1 and "一" <= word <= "鿿":
        return False
    return bool(word.strip())


def extract_query_terms(query: str) -> list[str]:
    """Return stable, de-duplicated terms for pairwise ranking features."""
    terms: list[str] = []
    for raw in query.split():
        words = segment_for_query(raw).split() if _needs_split(raw) else [raw]
        for word in words:
            normalized = word.strip().lower()
            if normalized and _is_meaningful(normalized) and normalized not in terms:
                terms.append(normalized)
    return terms[:16]


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


def search(conn, query: str, limit: int = 100, *,
           include_hierarchy: bool = True) -> dict[str, list[tuple[int, float]]]:
    """多路检索，返回各路的 (chunk_id, 分数) 排名列表。

    不在这里融合 —— 融合是 §12.3b ④ 两级 RRF 的职责。

    四路各有不可替代的职责：

      jieba    中文成词查询，主力
      trigram  编号、错别字、子串（「HT-2024-0023」靠它）
      substr   前两路都失效时兜底（分词切错词边界，见下）
      vector   语义改写（查询词与原文完全不重合时唯一可行的路）

    **为什么词法三路仍不够**：
    统计分词器必然在未登录词上切错边界。实测 jieba 把「汝窑天青釉」
    切成 `汝窑天 / 青釉`，于是 jieba 路无「汝窑」词元、trigram 路因
    2 字短于 3 字符组也不命中。substr 能兜住这个，但它只做字面匹配 ——
    用户问「怎么避免进程互相等待资源卡死」而原文写「死锁」时，
    四路里只有 vector 能命中。

    向量路不可用（模型未装、sqlite-vec 加载失败）时自动省略，
    其余三路照常工作 —— 语义检索是增强而非依赖。
    """
    jieba_q = build_fts_query(query, segment=True)
    raw_q = build_fts_query(query, segment=False)

    out: dict[str, list[tuple[int, float]]] = {
        "jieba": [], "trigram": [], "substr": [], "vector": [],
    }
    if not jieba_q and not raw_q:
        return out

    def run(table: str, q: str, k: int) -> list[tuple[int, float]]:
        if not q:
            return []
        try:
            return [
                (r[0], -r[1])  # bm25 越小越相关，取负统一为"越大越好"
                for r in conn.execute(
                    f"SELECT {table}.rowid, bm25({table}) FROM {table} "
                    f"JOIN chunks ch ON ch.id = {table}.rowid "
                    f"JOIN contents c ON c.id = ch.content_id "
                    f"WHERE {table} MATCH ? "
                    f"AND ch.index_version = c.active_index_version "
                    f"ORDER BY bm25({table}) LIMIT ?",
                    (q, k),
                )
            ]
        except sqlite3.Error:
            return []

    out["jieba"] = run("chunks_fts", jieba_q, limit)
    out["trigram"] = run("chunks_fts_tri", raw_q, limit)

    # 前两路结果太少时补子串兜底。
    #
    # 不是"全空才补"：分词切错时前两路可能命中少量**其他**分片
    # （比如查「汝窑」时 trigram 恰好在别处匹配到），此时正确结果
    # 仍然出不来。只要召回明显不足就补，代价是一次全表扫描。
    found = len({cid for r in (out["jieba"], out["trigram"]) for cid, _ in r})
    if found < min(3, limit):
        out["substr"] = _substring_search(conn, query, limit)

    out["vector"] = _vector_search(conn, query, limit)

    if include_hierarchy:
        try:
            from app.index.hierarchy import hierarchy_routes
            out.update(hierarchy_routes(conn, query, limit))
        except Exception:
            out.update({"document": [], "section": []})
    return out


def _vector_search(conn, query: str, limit: int) -> list[tuple[int, float]]:
    """语义检索路。模型或扩展不可用时静默返回空 —— 不能让整次检索失败。"""
    try:
        from app.index import embedding as emb
        from app.index import vector as vec

        if not emb.is_available():
            return []
        if vec.count(conn) == 0:
            return []
        qv = emb.get_embedder().encode_one(query)
        active_ids = [row["id"] for row in conn.execute(
            """SELECT ch.id FROM chunks ch JOIN contents c ON c.id = ch.content_id
               WHERE ch.index_version = c.active_index_version"""
        )]
        if not active_ids:
            return []
        return vec.search(conn, qv, limit=limit, candidate_ids=active_ids)
    except Exception as e:  # noqa: BLE001 - 任何异常都只降级，不冒泡
        import logging
        logging.getLogger("inktable.search").debug("向量路跳过：%s", e)
        return []


def _substring_search(conn, query: str, limit: int) -> list[tuple[int, float]]:
    """LIKE 子串兜底。

    长中文句子同样要先分词 —— `LIKE '%银行家算法用到哪些数据结构%'`
    永远不会命中。分词后按"命中词数最多"排序，近似相关性。
    """
    raw_terms = [t for t in query.split() if t.strip()]
    if not raw_terms:
        return []

    terms: list[str] = []
    for t in raw_terms:
        if _needs_split(t):
            terms += [w for w in segment_for_query(t).split() if _is_meaningful(w)]
        else:
            terms.append(t)
    terms = list(dict.fromkeys(terms))[:8]   # 去重并限长，避免 SQL 过长
    if not terms:
        return []

    # 按命中词数排序：全 AND 会让长问句清零，纯 OR 又没有相关性区分
    score_expr = " + ".join(
        ["(CASE WHEN text LIKE ? OR section_path LIKE ? THEN 1 ELSE 0 END)"] * len(terms)
    )
    where = " OR ".join(["(text LIKE ? OR section_path LIKE ?)"] * len(terms))
    like = []
    for t in terms:
        like += [f"%{t}%", f"%{t}%"]

    try:
        rows = conn.execute(
            f"SELECT ch.id, ({score_expr}) AS hits FROM chunks ch "
            f"JOIN contents c ON c.id = ch.content_id "
            f"WHERE ch.index_version = c.active_index_version AND ({where}) "
            f"ORDER BY hits DESC LIMIT ?",
            [*like, *like, limit],
        ).fetchall()
    except sqlite3.Error:
        return []
    return [(r["id"], float(r["hits"])) for r in rows if r["hits"] > 0]
