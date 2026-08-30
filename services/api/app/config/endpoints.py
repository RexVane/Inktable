"""Shared model-endpoint policy for every provider and proxy path."""

from __future__ import annotations

import urllib.request
from urllib.parse import urljoin, urlparse

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class EndpointPolicyError(ValueError):
    pass


class EndpointRedirectError(EndpointPolicyError):
    pass


def endpoint_origin(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise EndpointPolicyError("模型接口重定向地址无效")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise EndpointPolicyError("模型接口端口无效") from exc
    return parsed.scheme, parsed.hostname.lower().rstrip("."), port


def normalize_model_endpoint(endpoint: str, *, allow_empty: bool = False) -> str:
    value = (endpoint or "").strip().rstrip("/")
    if not value:
        if allow_empty:
            return ""
        raise EndpointPolicyError("接口地址不能为空")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise EndpointPolicyError("接口地址必须是有效的 http:// 或 https:// URL")
    if parsed.username is not None or parsed.password is not None:
        raise EndpointPolicyError("接口地址不能包含用户名或密码")
    try:
        parsed.port
    except ValueError as exc:
        raise EndpointPolicyError("模型接口端口无效") from exc
    if parsed.query or parsed.fragment:
        raise EndpointPolicyError("接口地址不能包含查询参数或片段")
    host = parsed.hostname.lower().rstrip(".")
    if parsed.scheme == "http" and host not in LOOPBACK_HOSTS:
        raise EndpointPolicyError("远程模型接口必须使用 https://（本机服务可用 http）")
    return value


def credential_scope(provider: str, endpoint: str) -> tuple[str, str, str, int]:
    scheme, host, port = endpoint_origin(normalize_model_endpoint(endpoint))
    return provider.strip().lower(), scheme, host, port


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow path redirects, but never forward credentials cross-origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        if endpoint_origin(req.full_url) != endpoint_origin(target):
            raise EndpointRedirectError("模型接口拒绝跨来源重定向")
        return super().redirect_request(req, fp, code, msg, headers, target)


def open_model_request(req, *, timeout: float):
    opener = urllib.request.build_opener(_SameOriginRedirectHandler())
    return opener.open(req, timeout=timeout)
