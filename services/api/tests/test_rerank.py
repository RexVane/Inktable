"""M3 reranker protocol, soft cap, and explicit degradation contracts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np

from app.db.database import connect, init_db
from app.retrieval import rerank


def _db():
    conn = connect(":memory:")
    init_db(conn)
    conn.execute(
        "INSERT INTO contents(id, sha256, size) VALUES (1, 'a', 1), (2, 'b', 1)"
    )
    for chunk_id in range(1, 16):
        content_id = 1 if chunk_id <= 14 else 2
        conn.execute(
            """INSERT INTO chunks
               (id, content_id, ordinal, text, text_hash, index_version)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (chunk_id, content_id, chunk_id, f"片段 {chunk_id}", f"h{chunk_id}"),
        )
    conn.commit()
    return conn


def _candidates():
    return [
        SimpleNamespace(chunk_id=chunk_id, rrf_score=1.0 / chunk_id)
        for chunk_id in range(1, 16)
    ]


def test_rrf_fallback_preserves_exact_order(monkeypatch):
    conn = _db()
    try:
        monkeypatch.setenv("INKTABLE_RERANKER", "rrf")
        result = rerank.run_rerank(conn, "问题", _candidates())
    finally:
        conn.close()

    assert [item.chunk_id for item in result.ranked] == list(range(1, 16))
    assert result.degraded is True
    assert result.model_id == "rrf-only"
    assert result.reranked_count == 0


def test_local_failure_degrades_without_reordering(monkeypatch):
    conn = _db()
    try:
        monkeypatch.setenv("INKTABLE_RERANKER", "local")
        monkeypatch.setattr(
            rerank.LocalStaticReranker, "rerank",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fail")),
        )
        result = rerank.run_rerank(conn, "问题", _candidates())
    finally:
        conn.close()

    assert [item.chunk_id for item in result.ranked] == list(range(1, 16))
    assert result.degraded is True
    assert result.model_id == "rrf-only"


def test_soft_cap_limits_long_document_before_local_rerank(monkeypatch):
    conn = _db()
    seen = []

    def record(_self, _query, candidates):
        seen.extend(candidates)
        return [
            rerank.RerankOutput(item.chunk_id, item.rrf_score)
            for item in candidates
        ]

    try:
        monkeypatch.setenv("INKTABLE_RERANKER", "local")
        monkeypatch.setattr(rerank.LocalStaticReranker, "rerank", record)
        result = rerank.run_rerank(conn, "问题", _candidates())
    finally:
        conn.close()

    assert sum(item.content_id == 1 for item in seen) == rerank.SOFT_PER_CONTENT
    assert any(item.content_id == 2 for item in seen)
    assert result.reranked_count == len(seen)


def test_redundant_coverage_demoted_within_document():
    """同文档里重复覆盖同一批查询词的分片让位给带新覆盖的分片。"""
    inputs = [
        rerank.RerankInput(1, 1, "如何启动 python 服务器", "", 0.9),
        rerank.RerankInput(2, 1, "再讲一遍 python 启动方式", "", 0.8),
        rerank.RerankInput(3, 1, "使用 python 3.11 版本 开发", "", 0.7),
    ]
    ranked = [
        rerank.RerankOutput(1, 0.90),
        rerank.RerankOutput(2, 0.88),  # 无新增覆盖，应被降权
        rerank.RerankOutput(3, 0.86),  # 新增覆盖"版本/开发"，应升到第 2
    ]
    adjusted = rerank._demote_redundant_coverage(
        "python 启动 版本 开发", inputs, ranked,
    )
    assert [item.chunk_id for item in adjusted] == [1, 3, 2]
    # 惩罚是软的：被降权分片仍在结果里，未被淘汰（K3）
    assert len(adjusted) == 3


def test_cross_content_near_duplicates_softly_demoted():
    """不同 content 的同文分片：只有最高分那份保持原分，副本降权。"""
    import pytest

    from app.index import embedding as emb
    if not emb.is_available():
        pytest.skip("本地嵌入模型不可用")

    text = "大湾区的AI应用已渗透到交通出行、政务服务、医疗服务等领域"
    inputs = [
        rerank.RerankInput(1, 1, text, "", 0.9),
        rerank.RerankInput(2, 2, text, "", 0.8),
        rerank.RerankInput(3, 3, "Inktable 的 AI 问答通过引用校验保证可信", "", 0.7),
    ]
    ranked = rerank.LocalStaticReranker().rerank("大湾区 AI 应用", inputs)
    scores = {output.chunk_id: output.score for output in ranked}
    assert ranked[0].chunk_id == 1
    # 副本（chunk 2）被明显降权，而不是与原件平分秋色
    assert scores[2] < scores[1] * 0.85
    # 不同内容的 chunk 3 未触发近重复惩罚：与副本文本余弦远低于阈值
    assert scores[3] > 0


def test_redundancy_pass_keeps_degraded_inputs_untouched():
    """降级路径（空文本输入）不受去冗影响，保持 RRF 顺序。"""
    inputs = [
        rerank.RerankInput(1, 0, "", "", 0.9),
        rerank.RerankInput(2, 0, "", "", 0.8),
    ]
    ranked = [rerank.RerankOutput(1, 0.9), rerank.RerankOutput(2, 0.8)]
    adjusted = rerank._demote_redundant_coverage("问题 词", inputs, ranked)
    assert [item.chunk_id for item in adjusted] == [1, 2]
    assert [item.score for item in adjusted] == [0.9, 0.8]


def test_cross_encoder_pair_scores_drive_order(monkeypatch):
    class FakeRuntime:
        model_id = "fake-cross"

        def score(self, _query, documents):
            assert len(documents) == 2
            return np.asarray([-2.0, 2.0], dtype=np.float32)

    monkeypatch.setattr(
        "app.retrieval.cross_encoder.get_runtime", lambda: FakeRuntime(),
    )
    model = rerank.CrossEncoderReranker()
    inputs = [
        rerank.RerankInput(1, 1, "irrelevant", "", 0.9),
        rerank.RerankInput(2, 2, "answer", "", 0.8),
    ]
    ranked = model.rerank("question", inputs)
    assert model.model_id == "fake-cross"
    assert [item.chunk_id for item in ranked] == [2, 1]
    assert ranked[0].score > ranked[1].score


def test_cross_encoder_failure_falls_back_to_local_with_degraded_trace(monkeypatch):
    conn = _db()

    def fail_cross(*_args, **_kwargs):
        raise RuntimeError("missing model")

    def local_order(_self, _query, candidates):
        return [
            rerank.RerankOutput(item.chunk_id, item.rrf_score)
            for item in candidates
        ]

    try:
        monkeypatch.setenv("INKTABLE_RERANKER", "cross")
        monkeypatch.setattr(rerank, "CrossEncoderReranker", fail_cross)
        monkeypatch.setattr(rerank.LocalStaticReranker, "rerank", local_order)
        result = rerank.run_rerank(conn, "问题", _candidates())
    finally:
        conn.close()

    assert result.model_id == "local-static-v3"
    assert result.degraded is True
    assert result.reranked_count > 0


def test_cascade_head_follows_fusion_order_not_local_score(monkeypatch):
    """级联的头部必须按融合顺序截取，不能按本地分。

    本地打分器在改写类问题上会把 gold 压下去（覆盖/邻近特征全为 0），
    用它选头部等于在二级重排之前先把最该救的候选淘汰掉 —— 这正是
    P19/P20/A09 三道题排到第 24-25 位的原因。
    """
    scored: dict[str, list[str]] = {}

    class FakeRuntime:
        model_id = "fake-cross"

        def score(self, _query, documents):
            scored["documents"] = list(documents)
            # 头部里最后一个给最高分，验证 CE 的判断能覆盖融合名次
            logits = [-1.0] * len(documents)
            logits[-1] = 5.0
            return np.asarray(logits, dtype=np.float32)

    monkeypatch.setattr(
        "app.retrieval.cross_encoder.get_runtime", lambda: FakeRuntime(),
    )
    monkeypatch.setattr(rerank, "CASCADE_PAIRS", 3)
    monkeypatch.setattr(rerank, "CASCADE_MIN_HEAD", 3)

    # 本地打分器故意给出与融合顺序完全相反的排序
    def reversed_local(_self, _query, candidates):
        return [
            rerank.RerankOutput(item.chunk_id, float(item.chunk_id))
            for item in candidates
        ]

    monkeypatch.setattr(rerank.LocalStaticReranker, "rerank", reversed_local)

    inputs = [
        rerank.RerankInput(chunk_id, chunk_id, f"正文 {chunk_id}", "", 1.0 / chunk_id)
        for chunk_id in range(1, 6)
    ]
    model = rerank.CascadeReranker(rerank.LocalStaticReranker())
    ranked = model.rerank("问题", inputs)

    # CE 只看到融合顺序的前 3 个，而不是本地分最高的 5/4/3
    assert scored["documents"] == ["正文 1", "正文 2", "正文 3"]
    # 头部第 3 个拿到最高 CE 分，应排到第一
    assert ranked[0].chunk_id == 3
    # 尾部（4、5）必须整体压在 CE 头部之下
    head_scores = [item.score for item in ranked if item.chunk_id in {1, 2, 3}]
    tail_scores = [item.score for item in ranked if item.chunk_id in {4, 5}]
    assert min(head_scores) >= max(tail_scores)
    assert "cascade:" in model.model_id


def test_cascade_head_shrinks_with_variant_count(monkeypatch):
    """变体越多单候选越贵，头部必须收缩，否则比较类问题顶穿 P95。"""
    monkeypatch.setattr(rerank, "CASCADE_PAIRS", 24)
    monkeypatch.setattr(rerank, "CASCADE_MIN_HEAD", 8)

    class FakeRuntime:
        model_id = "fake-cross"

        def score(self, _query, documents):
            return np.zeros(len(documents), dtype=np.float32)

    monkeypatch.setattr(
        "app.retrieval.cross_encoder.get_runtime", lambda: FakeRuntime(),
    )
    model = rerank.CascadeReranker(rerank.LocalStaticReranker())
    assert model._head_size(1, 100) == 24
    assert model._head_size(2, 100) == 12
    # 三变体时 24//3=8 恰好落在下限上
    assert model._head_size(3, 100) == 8
    # 变体多到把预算压穿下限时不再继续收缩，宁可超一点延迟
    assert model._head_size(8, 100) == 8
    # 候选数本身不足时以候选数为准
    assert model._head_size(1, 5) == 5


def test_cascade_falls_back_to_local_when_cross_encoder_missing(monkeypatch):
    """CE 模型未安装时级联必须退回一级本地打分器，而不是退到 RRF。

    级联的一级就是当前生产默认，降级后的质量不该跌破它。
    """
    conn = _db()

    def fail_runtime():
        raise RuntimeError("model not installed")

    def local_order(_self, _query, candidates):
        return [
            rerank.RerankOutput(item.chunk_id, item.rrf_score)
            for item in candidates
        ]

    try:
        monkeypatch.setenv("INKTABLE_RERANKER", "cascade")
        monkeypatch.setattr(
            "app.retrieval.cross_encoder.get_runtime", fail_runtime,
        )
        monkeypatch.setattr(rerank.LocalStaticReranker, "rerank", local_order)
        result = rerank.run_rerank(conn, "问题", _candidates())
    finally:
        conn.close()

    assert result.model_id == "local-static-v3"
    assert result.degraded is True
    assert result.reranked_count > 0


def _cascade_with(monkeypatch, ce_logits, local_scores):
    """构造一个级联重排器：CE 与本地打分器给出相反的偏好。"""

    class FakeRuntime:
        model_id = "fake-cross"

        def score(self, _query, documents):
            return np.asarray(ce_logits[:len(documents)], dtype=np.float32)

    monkeypatch.setattr(
        "app.retrieval.cross_encoder.get_runtime", lambda: FakeRuntime(),
    )

    def fake_local(_self, _query, candidates):
        return [
            rerank.RerankOutput(item.chunk_id, local_scores[item.chunk_id])
            for item in candidates
        ]

    monkeypatch.setattr(rerank.LocalStaticReranker, "rerank", fake_local)
    return rerank.CascadeReranker(rerank.LocalStaticReranker())


def test_cascade_ignores_local_score_when_query_has_no_lexical_evidence(monkeypatch):
    """改写类问题（查询词在正文里根本不出现）必须把权重让给 CE。

    这类问题上本地打分器的 coverage / proximity / exact 特征全为 0，分数
    是噪声。实测固定 37% 本地权重会把 P20（问"相对路径跑出目录"、原文写
    "路径穿越"）从第 8 名压回第 14 名。
    """
    # 查询词一个都不在正文里 —— 词法置信度为 0
    model = _cascade_with(
        monkeypatch,
        ce_logits=[-4.0, 4.0],          # CE 认为第 2 个才是答案
        local_scores={1: 1.0, 2: 0.0},  # 本地分完全相反
    )
    inputs = [
        rerank.RerankInput(1, 1, "完全无关的正文", "", 1.0),
        rerank.RerankInput(2, 2, "路径穿越的防护做法", "", 0.5),
    ]
    ranked = model.rerank("怎样防止用户用相对路径跑出自己的文件目录", inputs)
    assert [item.chunk_id for item in ranked] == [2, 1], "词法无证据时应完全听 CE"


def test_cascade_keeps_local_score_when_lexical_evidence_is_strong(monkeypatch):
    """词法证据充分时本地分必须仍然起作用。

    文件名/元数据类问题（M08「找出计算机网络课程设计成绩单 PDF」）靠本地
    打分器的 filename_cov / type_match 特征答对，纯 CE 会把它压到第 2。
    """
    model = _cascade_with(
        monkeypatch,
        ce_logits=[0.4, 0.6],           # CE 只轻微偏向第 2 个
        local_scores={1: 1.0, 2: 0.0},  # 本地分强烈偏向第 1 个
    )
    # 查询词在第一个候选的正文里密集出现 —— 词法置信度高
    inputs = [
        rerank.RerankInput(
            1, 1, "计算机网络课程设计成绩单的评分与名单", "成绩单", 1.0,
            file_name="计算机网络课程设计成绩单.pdf",
        ),
        rerank.RerankInput(2, 2, "另一份无关文档", "", 0.5),
    ]
    ranked = model.rerank("找出计算机网络课程设计成绩单 PDF", inputs)
    assert [item.chunk_id for item in ranked] == [1, 2], "词法证据强时本地分应能翻转轻微的 CE 偏好"


def test_lexical_gate_is_off_by_default(monkeypatch):
    """默认必须逐字保持原行为：门控关时 CE 照常参与。

    这条守的是「新增旋钮不改默认路径」。少了它，一次默认值手滑就会让所有
    改写类问题静默失去 CE，而指标要跑完整套 65 题才看得出来。
    """
    monkeypatch.setattr(rerank, "CASCADE_LEX_GATE", 0.0)
    model = _cascade_with(
        monkeypatch,
        ce_logits=[0.0, 5.0],
        local_scores={1: 1.0, 2: 0.0},
    )
    inputs = [
        rerank.RerankInput(1, 1, "命中查询词的正文", "", 1.0,
                           file_name="命中查询词.md"),
        rerank.RerankInput(2, 2, "另一份文档", "", 0.5),
    ]
    ranked = model.rerank("命中查询词", inputs)
    assert [item.chunk_id for item in ranked] == [2, 1], "门控关时 CE 应仍生效"
    assert model.model_id.startswith("cascade:")


def test_lexical_gate_skips_cross_encoder_when_evidence_is_strong(monkeypatch):
    """词法证据强时整个跳过 CE —— 并且 trace 必须说实话。

    跳过后实际生效的模型就是一级本地打分器，`model_id` 仍报 `cascade:` 是
    谎报：排查「为什么这题名次和纯本地一样」时会找错方向。
    """
    monkeypatch.setattr(rerank, "CASCADE_LEX_GATE", 0.45)

    called: list[int] = []

    class FakeRuntime:
        model_id = "fake-cross"

        def score(self, _query, documents):
            called.append(len(documents))
            return np.asarray([0.0] * len(documents), dtype=np.float32)

    monkeypatch.setattr(
        "app.retrieval.cross_encoder.get_runtime", lambda: FakeRuntime(),
    )
    monkeypatch.setattr(
        rerank.LocalStaticReranker, "rerank",
        lambda _self, _q, cands: [
            rerank.RerankOutput(item.chunk_id, 1.0 if item.chunk_id == 1 else 0.0)
            for item in cands
        ],
    )
    model = rerank.CascadeReranker(rerank.LocalStaticReranker())
    inputs = [
        rerank.RerankInput(1, 1, "计算机网络课程设计成绩单的评分与名单",
                           "成绩单", 1.0,
                           file_name="计算机网络课程设计成绩单.pdf"),
        rerank.RerankInput(2, 2, "另一份无关文档", "", 0.5),
    ]
    ranked = model.rerank("计算机网络课程设计成绩单", inputs)
    assert called == [], "词法证据强时不该调用 CE"
    assert [item.chunk_id for item in ranked] == [1, 2]
    assert model.model_id == "cascade-lex-skip:local-static-v3"


def test_lexical_gate_still_runs_cross_encoder_without_lexical_evidence(monkeypatch):
    """查询词一个都不出现时必须照常进 CE —— 门控不能把改写类也挡掉。

    真实库实测里 CE 的两个最大收益（P19 第 23→3、A09 第 16→4）都在这个
    区间；挡掉它们等于把 Recall@5 从 96.2% 退回 92.5%。
    """
    monkeypatch.setattr(rerank, "CASCADE_LEX_GATE", 0.45)
    model = _cascade_with(
        monkeypatch,
        ce_logits=[-4.0, 4.0],
        local_scores={1: 1.0, 2: 0.0},
    )
    inputs = [
        rerank.RerankInput(1, 1, "完全无关的正文", "", 1.0),
        rerank.RerankInput(2, 2, "路径穿越的防护做法", "", 0.5),
    ]
    ranked = model.rerank("怎样防止用户用相对路径跑出自己的文件目录", inputs)
    assert [item.chunk_id for item in ranked] == [2, 1]
    assert model.model_id.startswith("cascade:")


def test_vector_share_is_off_by_default_and_head_stays_fusion_order(monkeypatch):
    """默认头部必须仍是纯融合序前 N 个 —— 引入配额不改默认路径。"""
    monkeypatch.setattr(rerank, "CASCADE_VEC_SHARE", 0.0)
    model = _cascade_with(monkeypatch, ce_logits=[0.0] * 8,
                          local_scores={i: 0.0 for i in range(1, 9)})
    items = [rerank.RerankInput(i, i, "t", "", 1.0 / i, vector_rank=9 - i)
             for i in range(1, 9)]
    head, tail = model._split_head(items, 3)
    assert [i.chunk_id for i in head] == [1, 2, 3]
    assert [i.chunk_id for i in tail] == [4, 5, 6, 7, 8]


def test_vector_share_reserves_head_slots_for_vector_route(monkeypatch):
    """配额打开时向量路名次靠前的候选必须挤进头部。

    实测依据：门控挑进 CE 的题里 A09 的 gold 在融合序第 24 位、向量序第 1 位。
    只按融合序截头部 K 就下不来，Rerank P95 也就压不到门槛内。
    """
    monkeypatch.setattr(rerank, "CASCADE_VEC_SHARE", 0.5)
    model = _cascade_with(monkeypatch, ce_logits=[0.0] * 8,
                          local_scores={i: 0.0 for i in range(1, 9)})
    # 融合序 1..8；chunk 8 在融合序最后，但向量路第 1
    items = [rerank.RerankInput(i, i, "t", "", 1.0 / i,
                                vector_rank=1 if i == 8 else (10 - i))
             for i in range(1, 9)]
    head, tail = model._split_head(items, 4)
    ids = [i.chunk_id for i in head]
    assert 8 in ids, "向量路第 1 的候选必须进头部"
    assert 1 in ids, "融合序第 1 的候选不能被挤掉"
    assert len(head) == 4
    assert set(ids) & {i.chunk_id for i in tail} == set()


def test_vector_share_fills_quota_from_fusion_when_vector_route_is_empty(monkeypatch):
    """向量路不可用（模型未装）时配额不能浪费，头部要按融合序补满。"""
    monkeypatch.setattr(rerank, "CASCADE_VEC_SHARE", 0.5)
    model = _cascade_with(monkeypatch, ce_logits=[0.0] * 6,
                          local_scores={i: 0.0 for i in range(1, 7)})
    items = [rerank.RerankInput(i, i, "t", "", 1.0 / i) for i in range(1, 7)]
    head, _ = model._split_head(items, 4)
    assert [i.chunk_id for i in head] == [1, 2, 3, 4]


def test_focus_window_prefers_query_dense_region():
    """长分片必须按查询词密度取窗口，而不是从开头硬切。

    分片正文实测 p90 为 956 字，而 CE 截断在 384 token —— 答案落在后半段
    就完全看不到。这是 CE 在长文档上失效的直接原因。
    """
    needle = "断点续传通过 REST 命令从指定偏移继续传输"
    text = "无关开头。" * 200 + needle + "无关结尾。" * 200
    window = rerank._focus_window(text, ["断点续传", "偏移"], 120)
    assert len(window) <= 120
    assert "断点续传" in window

    # 没有任何查询词命中时退回开头，与原截断行为一致
    fallback = rerank._focus_window(text, ["完全不存在的词"], 60)
    assert fallback == text[:60]
    # 本来就短于预算的文本原样返回
    assert rerank._focus_window("短文本", ["短"], 100) == "短文本"


def test_auto_falls_back_to_local_when_cross_encoder_is_not_installed(monkeypatch):
    """没装 CE 资产时 `auto` 必须是纯本地，且不算降级。

    用户不该因为缺一个 279MB 的模型就拿到打了 `degraded` 标记的检索 ——
    那个标记是给「本该可用却失败了」用的。这里显式钉死 `is_available`，
    否则测试结果会取决于跑测试的机器上恰好有没有那个文件。
    """
    conn = _db()

    def reject_cross(*_args, **_kwargs):
        raise AssertionError("auto must not instantiate a rejected Cross-Encoder candidate")

    try:
        monkeypatch.setenv("INKTABLE_RERANKER", "auto")
        monkeypatch.setattr(rerank, "CrossEncoderReranker", reject_cross)
        monkeypatch.setattr(
            "app.retrieval.cross_encoder.is_available", lambda: False,
        )
        result = rerank.run_rerank(conn, "问题", _candidates())
    finally:
        conn.close()

    assert result.model_id == "local-static-v3"
    assert result.degraded is False


def test_auto_uses_gated_cascade_when_cross_encoder_is_installed(monkeypatch):
    """装了 CE 资产时 `auto` 走门控级联 —— 但仍然不能走被否掉的纯 cross。

    门控级联在真实库 65 题上四条门槛全过（R@5 96.2% / nDCG 90.3% /
    搜索 P95 1.5-1.7s / Rerank P95 0.9s），纯 cross 与裸 cascade 都不过。
    """
    conn = _db()

    def reject_cross(*_args, **_kwargs):
        raise AssertionError("auto must not instantiate a rejected Cross-Encoder candidate")

    class FakeRuntime:
        model_id = "fake-cross"

        def score(self, _query, documents):
            return np.zeros(len(documents), dtype=np.float32)

    try:
        monkeypatch.setenv("INKTABLE_RERANKER", "auto")
        monkeypatch.setattr(rerank, "CrossEncoderReranker", reject_cross)
        monkeypatch.setattr(
            "app.retrieval.cross_encoder.is_available", lambda: True,
        )
        monkeypatch.setattr(
            "app.retrieval.cross_encoder.get_runtime", lambda: FakeRuntime(),
        )
        result = rerank.run_rerank(conn, "问题", _candidates())
    finally:
        conn.close()

    assert result.model_id.startswith(("cascade:", "cascade-lex-skip:"))
    assert result.degraded is False


def test_shipped_cascade_defaults_match_the_measured_configuration():
    """默认值就是过门槛的那套实测配置 —— 改动其一必须重跑 65 题。

    这四个数字是一起测出来的：门控 0.45 决定哪些查询进 CE，向量配额 25% 与
    K=20 决定头部够不够深（U02 融合第 12 位、P19 向量第 3 位），
    max_tokens=192 决定单对成本。任何一个被顺手改掉，门槛结论就不再成立。
    """
    from app.retrieval import cross_encoder as ce

    assert rerank.CASCADE_LEX_GATE == 0.45
    assert rerank.CASCADE_VEC_SHARE == 0.25
    assert rerank.CASCADE_PAIRS == 20
    assert ce.__file__  # 默认 max_tokens 写在运行时构造里，见下
    src = Path(ce.__file__).read_text(encoding="utf-8")
    assert '"INKTABLE_RERANK_MAX_TOKENS", "192"' in src


def test_local_reranker_lexical_only_without_embedding_model(monkeypatch):
    """Ollama 不可用时本地打分器仍确定性重排（纯词法特征），不抛错、不降级 RRF。

    这是 test_auto_uses_selected_local_reranker_without_cross_encoder 的实现侧
    保证：auto 模式选中 local-static-v3 时，精排不应因现场编码不可用而失败。
    """
    from app.index import embedding as emb

    def no_model(*_args, **_kwargs):
        raise emb.EmbeddingUnavailable("本地嵌入模型不可用")

    monkeypatch.setattr(emb, "get_embedder", no_model)
    inputs = [
        rerank.RerankInput(1, 1, "python 服务器如何启动与配置", "", 0.9),
        rerank.RerankInput(2, 1, "完全无关的另一段正文内容", "", 0.5),
    ]
    ranked = rerank.LocalStaticReranker().rerank("如何启动 python 服务器", inputs)
    # 词法覆盖 + RRF 就足以把命中候选排到前面，且不依赖现场编码
    assert [item.chunk_id for item in ranked] == [1, 2]
    assert ranked[0].score > ranked[1].score


def test_cross_encoder_length_bucketing_restores_original_score_order():
    from app.retrieval.cross_encoder import OnnxCrossEncoder

    seen_batches = []

    class FakeTokenizer:
        def encode_batch(self, pairs):
            documents = [document for _query, document in pairs]
            seen_batches.append(documents)
            return [
                SimpleNamespace(
                    ids=[len(document)], attention_mask=[1], type_ids=[0],
                )
                for document in documents
            ]

    class FakeSession:
        def run(self, _outputs, feeds):
            return [feeds["input_ids"].astype(np.float32)]

    runtime = OnnxCrossEncoder.__new__(OnnxCrossEncoder)
    runtime.batch_size = 2
    runtime.tokenizer = FakeTokenizer()
    runtime.session = FakeSession()
    runtime.input_names = {"input_ids", "attention_mask", "token_type_ids"}

    scores = runtime.score("query", ["x" * 20, "y", "z" * 5])

    assert seen_batches == [["y", "z" * 5], ["x" * 20]]
    assert scores.tolist() == [20.0, 1.0, 5.0]
