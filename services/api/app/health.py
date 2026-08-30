"""真实健康检查 —— M0 冒烟的核心（PLAN §15 A0）。

每一项都实际执行而非声明：
  · enable_load_extension  → CPython 构建是否启用扩展加载
  · sqlite-vec             → dlopen 动态库 + 建表 + 插入 + KNN 查询
  · FTS5                   → 建虚拟表 + MATCH 查询
  · 中文分词               → jieba 全切分 + trigram 双索引实测命中

冻结环境（PyInstaller）下最易翻车的正是原生扩展加载，
只返回 200 的冒烟测试暴露不出来 —— 所以这里跑真实操作。
"""

from __future__ import annotations

import os
import sqlite3
import sys


def _check_sqlite_vec() -> dict:
    try:
        import sqlite_vec
    except ImportError as e:
        return {"ok": False, "error": f"import failed: {e}"}

    db = sqlite3.connect(":memory:")
    try:
        db.enable_load_extension(True)
    except AttributeError:
        return {"ok": False, "error": "CPython built without enable_load_extension"}

    try:
        sqlite_vec.load(db)
        version = db.execute("select vec_version()").fetchone()[0]

        # 真实建表 + 插入 + KNN，不只是加载
        db.execute("create virtual table t using vec0(embedding float[4])")
        for rowid, vec in [(1, [1, 0, 0, 0]), (2, [0, 1, 0, 0])]:
            db.execute(
                "insert into t(rowid, embedding) values (?, ?)",
                [rowid, sqlite_vec.serialize_float32(vec)],
            )
        hit = db.execute(
            "select rowid from t where embedding match ? order by distance limit 1",
            [sqlite_vec.serialize_float32([0.9, 0.1, 0, 0])],
        ).fetchone()

        if hit[0] != 1:
            return {"ok": False, "error": f"KNN returned wrong rowid: {hit[0]}"}
        return {"ok": True, "version": version}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


def _check_fts5() -> dict:
    db = sqlite3.connect(":memory:")
    try:
        db.execute("create virtual table t using fts5(c)")
        db.execute("insert into t values ('hello world')")
        n = db.execute("select count(*) from t where t match 'hello'").fetchone()[0]
        return {"ok": n == 1}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


def _check_chinese_search() -> dict:
    """中文双索引实测（PLAN §9.1）。

    FTS5 默认 unicode61 分词器对中文**零命中** —— 整段汉字被当作单个 token。
    方案定的解法是 jieba + trigram 双索引，这里验证它真的有效。

    M0 实测补充的两条修正（方案 v6 未覆盖，见 docs/M0-findings.md）：
      1. 必须用 jieba.cut_for_search（全切分），否则「保修期」切成一个词后
         查「保修」落空 —— 而双字词是中文最主流的查询形态
      2. 查询串必须用双引号包裹，否则 `HT-2024-0023` 的连字符被 FTS5 当语法
    """
    try:
        import jieba

        jieba.setLogLevel(60)
    except ImportError as e:
        return {"ok": False, "error": f"jieba import failed: {e}"}

    text = "本采购合同的保修期为二十四个月，甲方应于验收合格之日起计算，发票编号 HT-2024-0023。"
    db = sqlite3.connect(":memory:")
    try:
        db.execute("create virtual table t_tri using fts5(c, tokenize='trigram')")
        db.execute("insert into t_tri values (?)", [text])

        segmented = " ".join(jieba.cut_for_search(text))
        db.execute("create virtual table t_jb using fts5(c)")
        db.execute("insert into t_jb values (?)", [segmented])

        def match(table: str, query: str) -> int:
            quoted = '"' + query.replace('"', '""') + '"'
            return db.execute(
                f"select count(*) from {table} where {table} match ?", [quoted]
            ).fetchone()[0]

        # 覆盖双字词、三字词、编号 —— 每一类都曾在实测中漏过
        probes = ["保修", "保修期", "验收", "甲方", "发票", "二十四", "HT-2024-0023"]
        missed = []
        for q in probes:
            tri = match("t_tri", q)
            jb = match("t_jb", " ".join(jieba.cut_for_search(q)))
            if not (tri or jb):
                missed.append(q)

        return {
            "ok": not missed,
            "probes": len(probes),
            "missed": missed,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


def _check_embedding() -> dict:
    """嵌入服务（Ollama bge-m3）。

    真实编码 + 语义自检，不只探测端口 —— 模型拉了一半、量化损坏等
    情况只有真跑一次编码才能暴露。
    服务不可用不算失败（is_available=False 时纯关键词检索仍可用），
    但**编码失败**（服务在却算不出）必须暴露。
    """
    try:
        from app.index import embedding as emb
    except ImportError as e:
        return {"ok": False, "error": f"import failed: {e}"}

    if not emb.is_available():
        return {"ok": True, "available": False,
                "note": "未检测到 Ollama bge-m3，语义检索关闭"
                        "（安装 Ollama 后执行 ollama pull bge-m3）"}

    try:
        m = emb.get_embedder()
        v = m.encode(["汝窑天青釉", "宿舍电费充值", "汝窑瓷器的釉色"])
        sim_related = float(v[0] @ v[2])
        sim_unrelated = float(v[0] @ v[1])
        if sim_related <= sim_unrelated:
            return {"ok": False, "available": True,
                    "error": f"语义自检失败：相关 {sim_related:.2f} ≤ 无关 {sim_unrelated:.2f}"}
        return {"ok": True, "available": True, "model": m.model_id,
                "dim": m.dim, "loaded": emb.is_loaded()}
    except Exception as e:
        return {"ok": False, "available": True, "error": str(e)}


def _check_reranker() -> dict:
    """Verify dynamically imported Cross-Encoder runtime when assets exist.

    报的是 `active_spec()` 而不是某个常量：`is_available()` 的语义是「注册表里
    任意一个模型装上了就算可用」，所以「可用」并不能推出「跑的是默认那个」。
    额外列出 `installed`，否则用户看到 available=true 却无从知道生效的是哪个。
    """
    try:
        from app.retrieval.cross_encoder import MODELS, active_spec, is_available
    except ImportError as e:
        return {"ok": False, "available": False, "error": f"import failed: {e}"}

    try:
        model_id = active_spec().model_id
        installed = sorted(
            spec.key for spec in MODELS.values() if _reranker_installed(spec)
        )
    except Exception as e:  # noqa: BLE001 - 探测模型目录不该拖垮 /health
        return {"ok": False, "available": False, "error": f"probe failed: {e}"}

    assets_available = is_available()
    if not assets_available:
        return {
            "ok": True,
            "available": False,
            "model": model_id,
            "installed": installed,
            "note": "Cross-Encoder assets are not installed; local-static is the default",
        }
    try:
        import onnxruntime
        import tokenizers
    except ImportError as e:
        return {
            "ok": False,
            "available": False,
            "model": model_id,
            "installed": installed,
            "error": f"runtime import failed: {e}",
        }
    return {
        "ok": True,
        "available": True,
        "model": model_id,
        "installed": installed,
        "onnxruntime": onnxruntime.__version__,
        "tokenizers": tokenizers.__version__,
    }


def _reranker_installed(spec) -> bool:
    from app.retrieval.cross_encoder import _spec_installed

    return _spec_installed(spec)


def _check_ocr() -> dict:
    """Probe the platform OCR runtime; absence remains an optional capability."""
    try:
        from app.parsing import ocr
    except ImportError as e:
        return {"ok": False, "available": False, "error": f"import failed: {e}"}
    try:
        available = ocr.is_available()
        result = {
            "ok": True,
            "available": available,
            "engine": ocr.engine_id(),
            "engine_label": ocr.engine_label(),
        }
        if not available:
            result["note"] = "系统 OCR 或 OCR 语言包不可用"
        return result
    except Exception as e:
        return {
            "ok": False,
            "available": False,
            "engine": ocr.engine_id(),
            "error": str(e),
        }


def _check_retrieval_config() -> dict:
    """把生效的检索配置报出来。

    存在的理由很具体：`npm run dist` 冻结的是**当时**的 services/api 源码，
    而 8/26 那次重出安装包排在门控级联四条改动之前 —— 产物里根本没有门控，
    文档却按门控的实测数字写。当时没有任何办法从装好的应用上看出这一点。
    这一块就是那个办法：`mode` 与四个旋钮凑出「跑的是哪套检索」，
    与 docs/RETRIEVAL-PERF.md §5.9 的发布配置逐个对得上才算产物没跑偏。
    """
    try:
        from app.retrieval import rerank
        from app.retrieval.cross_encoder import is_available, resolved_max_tokens
    except ImportError as e:
        return {"ok": False, "error": f"import failed: {e}"}
    try:
        preference = os.environ.get("ORDO_RERANKER", "").strip().lower() or "auto"
        effective = preference
        if effective == "auto":
            effective = "cascade" if is_available() else "local"
        return {
            "ok": True,
            "mode": preference,
            "effective_mode": effective,
            "lex_gate": round(rerank.CASCADE_LEX_GATE, 3),
            "vec_share": round(rerank.CASCADE_VEC_SHARE, 3),
            "pairs": rerank.CASCADE_PAIRS,
            "max_tokens": resolved_max_tokens(),
        }
    except Exception as e:  # noqa: BLE001 - 报配置不该拖垮 /health
        return {"ok": False, "error": str(e)}


def collect_health() -> dict:
    checks = {
        "sqlite_vec": _check_sqlite_vec(),
        "fts5": _check_fts5(),
        "chinese_search": _check_chinese_search(),
        "embedding": _check_embedding(),
        "reranker": _check_reranker(),
        "retrieval_config": _check_retrieval_config(),
        "ocr": _check_ocr(),
    }
    return {
        "status": "ok" if all(c["ok"] for c in checks.values()) else "degraded",
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "frozen": getattr(sys, "frozen", False),
        "checks": checks,
    }
