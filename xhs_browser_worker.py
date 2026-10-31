"""Run the XHS Playwright browser flow in a fresh main process.

The SCRM dispatcher executes tasks in a worker thread. On Windows, starting
Playwright's asyncio driver from that thread can fail with WinError 5 while
creating overlapped pipes. This small process keeps Playwright on the main
thread and returns one JSON result.
"""
import asyncio
import json
import sys

from xhs_reply import reply_browser


async def main(payload):
    return await reply_browser(**payload)


if __name__ == "__main__":
    payload = json.loads(sys.stdin.read())
    result = asyncio.run(main(payload))
    print(json.dumps(result, ensure_ascii=False))
