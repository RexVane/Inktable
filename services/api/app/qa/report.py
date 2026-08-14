"""每周知识库摘要报告。

汇总最近 7 天的收录情况（新文件、类型分布、来源分布、代表性文档），
配置了模型时再让模型写一段导读。生成结果落盘到数据目录
`reports/<ISO周>.md`，同一周内幂等复用，不重复生成、不重复调模型。
"""

from __future__ import annotations

import logging
import sqlite3
import time

from app.db.database import APP_DIR

log = logging.getLogger("inktable.report")

REPORTS_DIR = APP_DIR / "reports"
VISIBLE = "(f.source_id IS NULL OR s.enabled = 1)"


def _week_key(now: float | None = None) -> str:
    return time.strftime("%G-W%V", time.localtime(now or time.time()))


def _gather(conn: sqlite3.Connection, since: float) -> dict:
    base = (
        "FROM files f LEFT JOIN sources s ON s.id = f.source_id "
        f"WHERE {VISIBLE} AND f.mtime >= ?"
    )
    total = conn.execute(f"SELECT count(*) c {base}", (since,)).fetchone()["c"]
    by_ext = conn.execute(
        f"SELECT lower(COALESCE(f.ext,'')) ext, count(*) c {base} "
        "GROUP BY lower(COALESCE(f.ext,'')) ORDER BY c DESC LIMIT 12",
        (since,),
    ).fetchall()
    by_source = conn.execute(
        f"SELECT COALESCE(s.name,'（已移除来源）') name, count(*) c {base} "
        "GROUP BY s.id ORDER BY c DESC LIMIT 12",
        (since,),
    ).fetchall()
    # 代表文档：优先有正文摘要的，带来源/日期/首个章节，供导读与展示
    docs = conn.execute(
        f"""SELECT f.name, f.mtime, f.size, COALESCE(s.name,'') src,
                   (SELECT dr.summary_text FROM document_representations dr
                     JOIN contents c ON c.id = f.content_id
                    WHERE dr.content_id = c.id
                      AND dr.index_version = c.active_index_version LIMIT 1) AS summary
            {base} AND f.content_id IS NOT NULL
            ORDER BY (CASE WHEN (SELECT 1 FROM document_representations dr
                                  JOIN contents c ON c.id = f.content_id
                                 WHERE dr.content_id = c.id) IS NULL THEN 1 ELSE 0 END),
                     f.size DESC LIMIT 24""",
        (since,),
    ).fetchall()
    lib_total = conn.execute(
        "SELECT count(*) c FROM files f LEFT JOIN sources s ON s.id = f.source_id "
        f"WHERE {VISIBLE}"
    ).fetchone()["c"]
    return {
        "total": total,
        "lib_total": lib_total,
        "by_ext": [dict(r) for r in by_ext],
        "by_source": [dict(r) for r in by_source],
        "docs": [dict(r) for r in docs],
    }


def _fmt_size(n: int | None) -> str:
    n = n or 0
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.0f} KB"
    return f"{n} B"


def _llm_digest(data: dict) -> str:
    """模型导读：读本周文档摘要，写一段真正有信息量的综述。

    未配置模型或失败时静默跳过，报告主体不依赖它。放开 token 与素材量 ——
    之前 80-150 字的限制正是"没啥有用东西"的主因。
    """
    from app.qa import llm

    if not llm.is_configured() or not data["docs"]:
        return ""
    items = "\n".join(
        f"- 《{d['name']}》（{d.get('src') or '未知来源'}）："
        f"{(d.get('summary') or '（无正文摘要）')[:400]}"
        for d in data["docs"][:16]
    )
    try:
        return llm.chat(
            [{"role": "system", "content":
              "你是个人知识库助手，为用户写本周新增资料的综述。要求：\n"
              "1) 按主题把相关文档归拢成 2-4 个方面，分点说明每个方面本周都进来了"
              "什么、有什么值得注意的内容（具体到文档里的事实，不要泛泛而谈）；\n"
              "2) 如果有合同、票据、报告等重要文件，单独点出并提示可能需要跟进的事项；\n"
              "3) 用 Markdown 小标题和分点组织，中文，充分但不啰嗦。\n"
              "只输出综述正文。"},
             {"role": "user", "content":
              f"本周共新增 {data['total']} 个文件。以下是其中有正文的代表文档及摘要：\n\n"
              + items}],
            temperature=0.4, max_tokens=None, timeout=60,
        ).strip()
    except llm.LLMError as exc:
        log.info("周报导读生成失败（跳过）：%s", exc)
        return ""


def _markdown(week: str, data: dict, digest: str) -> str:
    lines = [f"# 知识库周报 · {week}", ""]
    lines.append(f"生成于 {time.strftime('%Y-%m-%d %H:%M')} · "
                 f"本周新收录 **{data['total']}** 个文件 · 库内共 {data['lib_total']} 个")
    if not data["total"]:
        lines += ["", "本周没有新收录的文件。", ""]
        return "\n".join(lines)

    if digest:
        lines += ["", "## 本周综述", "", digest]

    if data["by_source"] or data["by_ext"]:
        lines += ["", "## 本周概览", ""]
        if data["by_source"]:
            parts = "，".join(f"{r['name']} {r['c']}" for r in data["by_source"][:6])
            lines.append(f"- **来源**：{parts}")
        if data["by_ext"]:
            parts = "，".join(f"{(r['ext'] or '无扩展名')} {r['c']}" for r in data["by_ext"][:8])
            lines.append(f"- **类型**：{parts}")

    if data["docs"]:
        lines += ["", "## 本周重点文档", ""]
        for d in data["docs"][:16]:
            summary = (d.get("summary") or "").strip().replace("\n", " ")
            meta = " · ".join(filter(None, [d.get("src") or "", _fmt_size(d.get("size"))]))
            lines.append(f"### {d['name']}")
            lines.append(f"<small>{meta}</small>" if meta else "")
            lines.append(summary[:260] if summary else "（该文件暂无正文摘要）")
            lines.append("")
    lines.append("")
    return "\n".join(lines)


def weekly_report(conn: sqlite3.Connection, *, force: bool = False) -> dict:
    """取本周报告；不存在（或 force）时生成并落盘。幂等。"""
    week = _week_key()
    path = REPORTS_DIR / f"{week}.md"
    if path.is_file() and not force:
        return {"week": week, "markdown": path.read_text(encoding="utf-8"),
                "path": str(path), "generated": False}

    data = _gather(conn, time.time() - 7 * 86400)
    digest = _llm_digest(data)
    markdown = _markdown(week, data, digest)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return {"week": week, "markdown": markdown, "path": str(path), "generated": True}
