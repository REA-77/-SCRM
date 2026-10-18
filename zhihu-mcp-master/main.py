"""Zhihu MCP Server — main entry point.

Usage:
    python main.py [--port PORT] [--headless] [--no-headless]

MCP endpoint: http://localhost:<port>/mcp
"""

import argparse
import sys

from loguru import logger

from server.tools import create_mcp_server


def main():
    parser = argparse.ArgumentParser(description="知乎 MCP 服务")
    parser.add_argument(
        "--port", "-p", type=int, default=18060,
        help="MCP 服务端口号 (默认: 18060)"
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="无头模式运行浏览器 "
    )
    parser.add_argument(
        "--no-headless", action="store_true", default=True,
        help="显示浏览器界面(默认)"
    )
    args = parser.parse_args()

    headless = not args.no_headless

    logger.info(f"知乎 MCP 服务启动中... (port={args.port}, headless={headless})")

    mcp = create_mcp_server(headless=headless)

    # Run with streamable HTTP transport (stateless to avoid session issues)
    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=args.port,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
