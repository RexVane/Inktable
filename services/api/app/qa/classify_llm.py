"""LLM 分类 —— PLAN B1 / §16.1。

规则引擎（A6）管得住"同来源同类型"这类模式；剩下的散件才值得花钱
问模型。三条纪律：

  · **手动触发，每次点击即一次授权** —— 云端分类会把文件名与正文开头
    发出去（§1 约束 3），不设常开开关，让每次发送都出自用户显式动作
  · **模型只能从既有分类树里选 id**（§16.1）：返回未知 id 直接跳过、
    留给人工，绝不"修复"也绝不据此建分类 —— 这同时消灭了注入类风险
  · 结果按 by='llm' 写入，用户随时改；规则与用户决定永远优先
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3

from app.organize.classify import assign_category, category_tree
from app.qa import llm

log = logging.getLogger("inktable.classify_llm")

BATCH = 20            # 单次调用最多归几个文件
HEAD_CHARS = 300      # 每个文件发送的正文开头长度 —— 只发必要文本（§16.1）

_JSON_BLOCK = re.compile(r"\[[\s\S]*\]")


def llm_classify_unclassified(conn: sqlite3.Connection, limit: int = BATCH) -> dict:
    if not llm.is_configured():
        return {"classified": 0, "skipped": 0, "error": "未配置模型服务"}

    cats = category_tree(conn)
    if not cats:
        return {"classified": 0, "skipped": 0, "error": "还没有分类，先在侧边栏创建"}

    rows = conn.execute(
        """SELECT f.id, f.name, f.ext,
                  (SELECT ch.text FROM chunks ch
                   WHERE ch.content_id = f.content_id
                   ORDER BY ch.ordinal LIMIT 1) AS head
           FROM files f
           WHERE f.category_id IS NULL AND f.confirmed_by_user = 0
             AND f.state NOT IN ('missing', 'cloud_placeholder')
           ORDER BY f.detected_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    if not rows:
        return {"classified": 0, "skipped": 0}

    cat_lines = "\n".join(
        f"  {c['id']}: {'　' * c['depth']}{c['name']}" for c in cats
    )
    valid_ids = {c["id"] for c in cats}
    file_lines = "\n".join(
        f"  {r['id']}: 《{r['name']}》 {(r['head'] or '')[:HEAD_CHARS]}"
        for r in rows
    )

    messages = [
        {"role": "system", "content":
         "你是文件归类助手。只能从给定分类 id 中选择；无法判断的文件返回 null。"
         '只输出 JSON 数组，形如 [{"file_id":1,"category_id":2}]，不要任何其他文字。'},
        {"role": "user", "content":
         f"分类树（id: 名称）：\n{cat_lines}\n\n待归类文件（id: 文件名 正文开头）：\n{file_lines}"},
    ]

    raw = llm.chat(messages, temperature=0.0, max_tokens=1200)
    m = _JSON_BLOCK.search(raw)
    if not m:
        return {"classified": 0, "skipped": len(rows), "error": "模型未返回有效 JSON"}
    try:
        picks = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"classified": 0, "skipped": len(rows), "error": "模型 JSON 解析失败"}

    file_ids = {r["id"] for r in rows}
    done = skipped = 0
    for item in picks if isinstance(picks, list) else []:
        try:
            fid = int(item.get("file_id"))
            cid = item.get("category_id")
        except (TypeError, ValueError, AttributeError):
            skipped += 1
            continue
        # 只认这批文件 + 既有分类 id；越界一律跳过（§16.1）
        if fid not in file_ids or cid is None or int(cid) not in valid_ids:
            skipped += 1
            continue
        assign_category(conn, [fid], int(cid), by="llm")
        done += 1

    log.info("LLM 归类：%d 成，%d 跳过", done, skipped)
    return {"classified": done, "skipped": skipped + (len(rows) - done - skipped)}
