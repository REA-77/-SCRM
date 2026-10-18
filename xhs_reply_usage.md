# 小红书可调用回复功能

文件：`xhs_reply.py`。统一异步入口 `reply_comment()`，也可单独调用 `reply_api()`、`reply_browser()`。导入不执行发送，调用一次发送一次，不自动重试或自动切换路径。无需数据库。

## 比特浏览器路径

安装 Python 依赖 `py -m pip install playwright`。使用比特已有内核，无需安装 Playwright 浏览器。启动比特浏览器并开启本地 API，在指定窗口登录小红书。

```python
import asyncio
from xhs_reply import reply_comment

async def main():
    result = await reply_comment(
        note_id="笔记ID",
        content="回复内容",
        mode="browser",
        browser_id="比特浏览器窗口ID",  # 窗口 ID，不是窗口序号
        bit_api_url="http://127.0.0.1:54345",
        target_comment_id="评论ID",
        target_text="",  # 页面没有评论 ID 属性时，填完整评论文本辅助定位
        xsec_token="",   # 如有分享链接 token，建议传入
    )
    print(result)

asyncio.run(main())
```

连接方式：比特 `POST /browser/open` → `data.ws` → Playwright CDP。复用窗口的登录态、UA、代理和指纹，不从数据库创建新画像。新建专用页面，尝试发送后保留该页，断开连接不关闭比特窗口。

按评论 ID 定位，必要时按完整文本辅助定位；目标不唯一、找不到目标或请求参数不匹配时停止。不会将未找到目标的回复改成顶层评论。请求校验适用于页面的标准小红书评论 POST 接口，页面或接口改版时需适配。

默认选择器取自现有页面结构假设，尚未在实际账号中验证。可用 `editor_selector`、`submit_selector` 覆盖输入框和发送按钮选择器；未展开的子评论可能需要先在页面确认目标可见或进一步适配展开逻辑。

命令行（执行即发送）：

```powershell
py -X utf8 xhs_reply.py --mode browser --browser-id "窗口ID" --note-id "笔记ID" --target-comment-id "评论ID" --content "回复内容"
```

## API 路径

已将提供的 `xhs_direct.zip` 中包本体放入同级 `xhs_direct/` 目录，默认直接使用其中的 `XhsApiClient`，不再依赖原项目 `app.platforms`。复制部署时需要一起携带 `xhs_reply.py` 和完整 `xhs_direct/` 目录。

当前项目使用独立环境 `.venv-xhs-reply`。运行示例或命令行时，将 `py` 替换为 `.\.venv-xhs-reply\Scripts\python.exe`。其他环境先执行 `python -m pip install -r requirements-xhs-reply.txt`。POST 回复不需要 Node.js、PyExecJS 或 crypto-js。

```python
import asyncio
from pathlib import Path
from xhs_reply import reply_api

async def main():
    result = await reply_api(
        note_id="笔记ID",
        content="回复内容",
        target_comment_id="评论ID",
        xsec_token="",
        storage_state=Path("storage_state.json").read_text(encoding="utf-8"),
        user_agent="该账号实际的完整 User-Agent",
        proxy="",
    )
    print(result)

asyncio.run(main())
```

仅提取小红书域的 Cookie，要求存在非空 `a1`；这不代表登录态一定有效。内置客户端负责签名、传输和完整业务响应校验，成功后返回 `data`，因此本封装的 `result` 就是该 `data`。平台明确拒绝返回 `failed`；响应不明确、网络错误或服务端故障返回 `uncertain`。不自动重发。

高级用法：`client_factory` 仍可注入兼容客户端，但自定义客户端应返回包含 `success` 或 `code` 的完整响应；缺少明确状态时判为 `uncertain`。

```powershell
.\.venv-xhs-reply\Scripts\python.exe -X utf8 xhs_reply.py --mode api --storage-state storage_state.json --user-agent "账号完整UA" --note-id "笔记ID" --target-comment-id "评论ID" --content "回复内容"
```

## 返回值

```python
{"status": "success", "success": True, "result": {...}, "error": ""}
```

- `success`：业务响应明确成功。
- `failed`：发送前失败、请求被阻止或平台明确拒绝。
- `uncertain`：已尝试发送，但超时、连接中断或响应缺少明确业务状态。先核对平台，不要直接重发。

`target_comment_id=""` 表示顶层评论；非空表示回复。API 和浏览器路径由调用方显式选择，不自动接续，以免重复发送。命令行退出码分别为 0、1、2。

验证范围：实际客户端本地签名、请求内容构造、业务响应解包、平台拒绝与模糊状态分类、Cookie 域隔离及请求目标校验。测试替换网络传输，不向平台发送。线上签名是否仍被平台接受及浏览器页面定位，尚未进行真实账号验证。
