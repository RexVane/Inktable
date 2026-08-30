from __future__ import annotations

import urllib.request

import pytest

from app.config.endpoints import (
    EndpointPolicyError,
    EndpointRedirectError,
    _SameOriginRedirectHandler,
    credential_scope,
    normalize_model_endpoint,
)


def test_credential_scope_includes_scheme_and_effective_port() -> None:
    assert credential_scope("openai", "https://api.example.com/v1") == (
        "cloud", "https", "api.example.com", 443,
    )
    assert credential_scope("openai", "https://api.example.com:443/v2") == (
        "cloud", "https", "api.example.com", 443,
    )
    assert credential_scope("anthropic", "https://api.example.com/v1") == (
        "cloud", "https", "api.example.com", 443,
    )
    assert credential_scope("ollama", "http://127.0.0.1:11434") == (
        "ollama", "http", "127.0.0.1", 11434,
    )
    assert credential_scope("openai", "http://127.0.0.1/v1") != (
        "cloud", "https", "127.0.0.1", 443,
    )


def test_model_redirects_are_limited_to_the_original_origin() -> None:
    handler = _SameOriginRedirectHandler()
    request = urllib.request.Request(
        "https://api.example.com/v1/models",
        headers={"Authorization": "Bearer secret"},
    )

    same_origin = handler.redirect_request(
        request, None, 302, "Found", {}, "/v2/models",
    )
    assert same_origin.full_url == "https://api.example.com/v2/models"
    assert same_origin.get_header("Authorization") == "Bearer secret"

    with pytest.raises(EndpointRedirectError, match="跨来源重定向"):
        handler.redirect_request(
            request, None, 302, "Found", {}, "https://evil.example/models",
        )


def test_endpoint_rejects_malformed_ports() -> None:
    with pytest.raises(EndpointPolicyError, match="端口无效"):
        normalize_model_endpoint("https://api.example.com:not-a-port/v1")
