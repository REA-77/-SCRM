"""Zhihu login helper — opens browser for QR code login.

Usage:
    python login.py [--no-headless]
"""

import argparse
import sys

from loguru import logger

from zhihu.service import ZhihuService


def main():
    parser = argparse.ArgumentParser(description="知乎登录工具")
    parser.add_argument(
        "--no-headless", action="store_true", default=True,
        help="显示浏览器界面 (默认)"
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="无头模式 (不推荐，无法看到二维码)"
    )
    args = parser.parse_args()

    headless = args.headless
    if not headless:
        logger.info("将显示浏览器窗口，请准备好知乎 App 扫码")

    service = ZhihuService()

    # Direct login flow: open browser → show QR → wait for scan → save cookies
    logger.info("正在打开知乎登录页面...")
    logger.info("浏览器窗口弹出后，请使用知乎 App 扫描二维码")
    logger.info("等待扫码登录 (最长 240 秒)...")

    result = service.login_wait(headless=headless, timeout=240)

    if result["success"]:
        logger.info("✅ 登录成功! Cookies 已保存。")
        logger.info("现在可以启动 MCP 服务: python main.py")
    else:
        logger.error(f"❌ 登录失败: {result['message']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
