# -*- coding: utf-8 -*-
"""可调用的知乎评论回复入口（依赖用户提供的 zhihu.service）。

Python:
    from zhihu_reply import reply_comment
    result = reply_comment(url, comment_author, content, headless=False)

命令行（会实际发送）:
    py -X utf8 zhihu_reply.py --url URL --comment-author 昵称 --content 回复内容

将包含 zhihu/service.py 的项目根目录加入 PYTHONPATH 后使用。
本文件不会自动安装同名第三方包，也不会在导入时打开浏览器或发送回复。
"""

import argparse
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlsplit


def reply_comment(url, comment_author, content, headless=False, *, service=None, bit_window_id=None, bit_api_url="http://127.0.0.1:54345", target_content=""):
    """发送一次回复，返回 success/message；service 可注入现有服务实例。

    不自动重试：服务异常时可能已经发送，请核对页面后再决定是否重试。
    目标评论定位与发送确认由 ZhihuService 负责，本封装不猜测发送结果。
    """
    for name, value in (("url", url), ("comment_author", comment_author), ("content", content)):
        if not isinstance(value, str) or not value.strip():
            return {"success": False, "message": f"{name} 必须为非空字符串", "code": "invalid_argument"}
    if not isinstance(headless, bool):
        return {"success": False, "message": "headless 必须为布尔值", "code": "invalid_argument"}
    try:
        parsed = urlsplit(url.strip())
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "https" or not (host == "zhihu.com" or host.endswith(".zhihu.com"))
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.path in ("", "/")):
            raise ValueError("invalid URL")
    except ValueError:
        return {"success": False, "message": "请提供有效的知乎 HTTPS 帖子链接", "code": "invalid_argument"}

    if bit_window_id is not None and (not isinstance(bit_window_id, str) or not bit_window_id.strip()):
        return {"success": False, "message": "比特窗口ID不能为空", "code": "invalid_argument"}

    if service is None:
        bundled = Path(__file__).resolve().parent / "zhihu-mcp-master"
        if (bundled / "zhihu" / "service.py").is_file():
            for location in (bundled, bundled / ".deps"):
                if str(location) not in sys.path:
                    sys.path.insert(0, str(location))
        try:
            from zhihu.service import ZhihuService
        except ImportError:
            return {"success": False, "code": "dependency_unavailable",
                    "message": "无法导入 zhihu.service.ZhihuService，请配置提供该接口的项目及其依赖"}
        try:
            service = ZhihuService()
        except Exception as exc:
            return {"success": False, "code": "initialization_failed",
                    "message": f"知乎服务初始化失败（{type(exc).__name__}）"}
    try:
        session = nullcontext()
        if bit_window_id:
            from browser.manager import use_bitbrowser
            session = use_bitbrowser(bit_window_id.strip(), bit_api_url)
        with session:
            result = service.reply_comment(
                url=url.strip(), comment_author=comment_author.strip(),
                content=content, headless=headless, target_content=target_content,
            )
    except Exception as exc:
        detail = str(exc).strip().replace("\n", " ")[:500]
        return {"success": False, "code": "send_status_unknown",
                "message": f"回复调用异常（{type(exc).__name__}）：{detail or '无详细信息'}；发送状态未知，请核对页面，勿直接重复发送"}
    if not isinstance(result, dict) or type(result.get("success")) is not bool:
        return {"success": False, "code": "send_status_unknown",
                "message": "服务未返回有效的 success 布尔状态，请核对页面，勿直接重复发送"}
    return result


def main():
    parser = argparse.ArgumentParser(description="知乎评论回复（执行即实际发送）")
    parser.add_argument("--url", required=True, help="知乎帖子 URL")
    parser.add_argument("--comment-author", required=True, help="被回复人昵称")
    parser.add_argument("--content", required=True, help="回复内容")
    parser.add_argument("--headless", action="store_true", help="无头浏览器；默认显示窗口")
    parser.add_argument("--bit-window-id", help="比特浏览器窗口ID，复用该窗口登录态，无需安装 Chromium")
    parser.add_argument("--bit-api-url", default="http://127.0.0.1:54345", help="比特浏览器本地API地址")
    args = parser.parse_args()
    result = reply_comment(args.url, args.comment_author, args.content, args.headless, bit_window_id=args.bit_window_id, bit_api_url=args.bit_api_url)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
