"""代理 URL 规范化（精简自 CreatorHub app/browser/proxy.py 的 ProxyPlan）。

裸 host:port -> http://host:port；保留账号密码；无法解析则原样返回。
例：'1.2.3.4:8080' -> 'http://1.2.3.4:8080'
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote, unquote, urlparse


class ProxyConfigError(ValueError):
    pass


class ProxyPlan:
    def __init__(self, scheme: str, host: str, port: int,
                 username: str = "", password: str = ""):
        self.scheme = scheme
        self.host = host
        self.port = port
        self.username = username
        self.password = password

    @classmethod
    def parse(cls, raw: str) -> Optional["ProxyPlan"]:
        value = str(raw or "").strip()
        if not value:
            return None
        try:
            parsed = urlparse(value if "://" in value else f"http://{value}")
            scheme = (parsed.scheme or "http").lower()
            if scheme == "socks":
                scheme = "socks5"
            if scheme not in {"http", "https", "socks5"}:
                raise ProxyConfigError("不支持的代理协议")
            host = parsed.hostname or ""
            port = parsed.port
        except ProxyConfigError:
            raise
        except (TypeError, ValueError):
            raise ProxyConfigError("代理配置无效：端口格式错误") from None
        if not host or port is None or not (1 <= int(port) <= 65535):
            raise ProxyConfigError("代理配置无效：必须包含主机和端口")
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        return cls(scheme, host, int(port), username, password)

    @property
    def authenticated(self) -> bool:
        return bool(self.username or self.password)

    @property
    def normalized(self) -> str:
        if not self.authenticated:
            return f"{self.scheme}://{self.host}:{self.port}"
        user = quote(self.username, safe="")
        password = quote(self.password, safe="")
        return (
            f"{self.scheme}://{user}:{password}@"
            f"{self.host}:{self.port}"
        )


def try_proxy_plan(raw: str) -> Optional[ProxyPlan]:
    """Compatibility parser: invalid values are represented as None."""
    try:
        return ProxyPlan.parse(raw)
    except ProxyConfigError:
        return None


def normalize_proxy(s: str) -> str:
    """把用户输入规范成带协议头的代理 URL（curl_cffi 必须带 scheme）。"""
    s = (s or "").strip()
    plan = try_proxy_plan(s)
    return plan.normalized if plan else s
