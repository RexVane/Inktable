"""CE 模型注册表的契约测试。

注册表存在的理由是「换更便宜的 CE」要能与旧模型**同库对照评测**，而不是改代码
切换。它带来一个具体的坑，这里守住：默认模型若写死成新模型，已经装了旧模型的
用户会因为新模型不存在而 `is_available()` 返回 False，`auto` 静默退回一级本地
打分器 —— 表现是「升级之后检索变差了」，且没有任何报错。
"""

from __future__ import annotations

import pytest

from app.retrieval import cross_encoder as ce


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("ORDO_RERANK_MODEL", raising=False)
    monkeypatch.delenv("ORDO_RERANK_MODEL_DIR", raising=False)


def _installed(monkeypatch, *keys: str) -> None:
    """只把指定的模型伪装成「已装」。"""
    allowed = set(keys)
    monkeypatch.setattr(
        ce, "_spec_installed", lambda spec: spec.key in allowed,
    )


def test_registry_has_both_measured_models():
    assert set(ce.MODELS) == {"bge-base", "mminilm-l12-h384"}
    for spec in ce.MODELS.values():
        assert spec.model_sha256 and spec.tokenizer_sha256, "资产必须钉指纹"
        assert spec.model_size > 0 and spec.tokenizer_size > 0


def test_prefers_the_cheaper_model_when_both_installed(monkeypatch):
    _installed(monkeypatch, "bge-base", "mminilm-l12-h384")
    assert ce.active_spec().key == "mminilm-l12-h384"


def test_falls_back_to_the_older_model_instead_of_degrading(monkeypatch):
    """只装了旧模型时必须用旧模型，而不是报「没装」。

    少了这条，换默认那一刻所有老用户的检索都会静默退回纯本地
    （Recall@5 96.2% → 92.5%），而界面上什么都不会说。
    """
    _installed(monkeypatch, "bge-base")
    assert ce.active_spec().key == "bge-base"
    assert ce.is_available() is True


def test_unavailable_only_when_nothing_is_installed(monkeypatch):
    _installed(monkeypatch)
    assert ce.is_available() is False


def test_explicit_choice_overrides_preference(monkeypatch):
    _installed(monkeypatch, "bge-base", "mminilm-l12-h384")
    monkeypatch.setenv("ORDO_RERANK_MODEL", "bge-base")
    assert ce.active_spec().key == "bge-base"


def test_explicit_choice_reports_unavailable_when_that_one_is_missing(monkeypatch):
    """显式指定了哪个就只看那个 —— 否则「我指定了 A」却跑着 B，评测结论会错配。"""
    _installed(monkeypatch, "mminilm-l12-h384")
    monkeypatch.setenv("ORDO_RERANK_MODEL", "bge-base")
    assert ce.active_spec().key == "bge-base"
    assert ce.is_available() is False


def test_unknown_key_falls_back_instead_of_raising(monkeypatch):
    """拼错模型名不能让检索整条挂掉，退到偏好首选即可。"""
    monkeypatch.setenv("ORDO_RERANK_MODEL", "typo-model")
    assert ce.active_spec().key == ce._PREFERRED[0]


def test_model_dir_follows_the_active_spec(monkeypatch):
    _installed(monkeypatch, "bge-base", "mminilm-l12-h384")
    assert ce.model_dir().name == ce.MODELS["mminilm-l12-h384"].model_id
    monkeypatch.setenv("ORDO_RERANK_MODEL", "bge-base")
    assert ce.model_dir().name == ce.MODELS["bge-base"].model_id


def test_model_dir_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("ORDO_RERANK_MODEL_DIR", str(tmp_path))
    assert ce.model_dir() == tmp_path


def test_no_module_level_model_id():
    """别再放一个「那个模型 id」的常量出来。

    原先有 `MODEL_ID`，`health.py` 一直在 import 它。注册表化之后实际跑着
    mMiniLM 时 /health 仍报 bge —— 唯一对外说明「用的是哪个 CE」的字段撒谎，
    且不报错。删掉常量是唯一能让这类误用在 import 期就炸掉的办法。
    """
    assert not hasattr(ce, "MODEL_ID")


def test_health_reports_the_active_model_not_a_constant(monkeypatch):
    from app import health

    _installed(monkeypatch, "bge-base", "mminilm-l12-h384")
    report = health._check_reranker()
    assert report["model"] == ce.MODELS["mminilm-l12-h384"].model_id
    assert report["installed"] == ["bge-base", "mminilm-l12-h384"]

    monkeypatch.setenv("ORDO_RERANK_MODEL", "bge-base")
    assert health._check_reranker()["model"] == ce.MODELS["bge-base"].model_id


def test_health_lists_which_models_are_installed(monkeypatch):
    """available=true 推不出「跑的是默认那个」—— 得能看出装了哪几个。"""
    from app import health

    _installed(monkeypatch, "bge-base")
    report = health._check_reranker()
    assert report["available"] is True
    assert report["installed"] == ["bge-base"]
    assert report["model"] == ce.MODELS["bge-base"].model_id

    _installed(monkeypatch)
    missing = health._check_reranker()
    assert missing["available"] is False
    assert missing["installed"] == []
