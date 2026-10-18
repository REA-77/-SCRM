"""小红书签名直连 API 客户端 —— 独立精简版。

从 CreatorHub 项目拆出，脱离原项目单独使用：
    - XhsApiClient         签名直连客户端（client.py）
    - XhsApiError          接口异常（responses.py）
    - cookie_str_from_state  Playwright storage_state JSON -> Cookie 串（cookies.py）
    - has_a1               登录态校验（cookies.py）

示例:
    from xhs_direct import XhsApiClient, cookie_str_from_state, has_a1
"""
from .client import XhsApiClient
from .responses import XhsApiError
from .cookies import cookie_str_from_state, has_a1

__all__ = [
    "XhsApiClient",
    "XhsApiError",
    "cookie_str_from_state",
    "has_a1",
]
