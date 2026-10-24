"""douyin_commenter — 抖音评论发送最小调用集(从 creatorhub 抽取)。

两条路径:
- API 直连:DouyinClient.post_comment(cookie + a_bogus 本地签名)
- 浏览器:post_comment_browser(页面模拟输入,拦截 publish 接口判成功)

成功判据统一以抖音 comment/publish 接口 status_code==0 为准;
结果未知时一律返回错误,由调用方决定是否重试(重试前必须人工核对,防重复发)。
"""
from .client import DouyinClient, cookie_from_state
from .browser_post import post_comment_browser

__all__ = ["DouyinClient", "cookie_from_state", "post_comment_browser"]
