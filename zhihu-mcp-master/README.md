# zhihu-mcp

知乎 MCP 服务 — 让你的 AI 助手直接操作知乎。

基于 [xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp) 的设计思路，使用 Python + Playwright + FastMCP 实现。

## 功能总览

| # | MCP 工具 | 说明 |
|---|---------|------|
| 1 | `check_login_status` | 检查知乎登录状态 |
| 2 | `get_login_qrcode` | 获取登录二维码 |
| 3 | `delete_cookies` | 删除 cookies，重置登录状态 |
| 4 | `publish_article` | 发布图文文章 |
| 5 | `publish_video` | 发布视频内容 |
| 6 | `search_content` | 搜索知乎内容（结果自动保存为 md 文件） |
| 7 | `get_recommend_list` | 获取首页推荐列表 |
| 8 | `get_feed_detail` | 获取帖子详情（含正文和评论，自动保存为 md 文件） |
| 9 | `post_comment` | 发表评论到帖子 |
| 10 | `get_user_profile` | 获取用户个人主页 |
| 11 | `reply_comment` | 回复评论 |

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

安装 Playwright 浏览器：

```bash
python -m playwright install chromium
```

### 2. 登录知乎

首次使用需要扫码登录，会弹出浏览器窗口：

```bash
python login.py
```

登录成功后 cookies 自动保存到 `cookies/cookies.json`。

### 3. 启动 MCP 服务

```bash
# 默认：端口 18060，显示浏览器
python main.py

# 无头模式（不显示浏览器）
python main.py --headless

# 自定义端口
python main.py --port 8080
```

MCP 服务运行在：`http://localhost:18060/mcp`

### 4. 配置代理（可选）

```bash
set ZHIHU_PROXY=http://user:pass@proxy:port   # Windows
export ZHIHU_PROXY=http://user:pass@proxy:port # Linux/Mac
python main.py
```

## MCP 客户端接入

### Claude Code

```bash
claude mcp add --transport http zhihu-mcp http://localhost:18060/mcp
```

### Open Code

```bash
opencode mcp add
# 名称: zhihu-mcp
# 类型: Remote
# URL: http://localhost:18060/mcp
```

### Cursor

在项目根目录创建 `.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "zhihu-mcp": {
      "url": "http://localhost:18060/mcp"
    }
  }
}
```

### VS Code

在项目根目录创建 `.vscode/mcp.json`：

```json
{
  "servers": {
    "zhihu-mcp": {
      "url": "http://localhost:18060/mcp",
      "type": "http"
    }
  }
}
```

### MCP Inspector（调试用）

```bash
npx @modelcontextprotocol/inspector
# 连接地址填 http://localhost:18060/mcp
```

## OpenCode 技能（Skills）

项目包含两个 OpenCode 技能文件，安装后可让 AI 自动按流程操作知乎：

### 安装技能

将技能文件复制到 OpenCode 技能目录：

```bash
# Windows
copy skills\zhihu-auth\SKILL.md %USERPROFILE%\.agents\skills\zhihu-auth\SKILL.md
copy skills\zhihu-publish\SKILL.md %USERPROFILE%\.agents\skills\zhihu-publish\SKILL.md

# Linux / Mac
mkdir -p ~/.agents/skills/zhihu-auth ~/.agents/skills/zhihu-publish
cp skills/zhihu-auth/SKILL.md ~/.agents/skills/zhihu-auth/SKILL.md
cp skills/zhihu-publish/SKILL.md ~/.agents/skills/zhihu-publish/SKILL.md
```

安装后重启 OpenCode 生效。

### 技能说明

| 技能 | 用途 | 触发词 |
|------|------|--------|
| `zhihu-auth` | 登录状态检查、扫码登录、退出登录 | "登录知乎"、"检查登录"、"退出登录" |
| `zhihu-publish` | 文章发布、视频发布、搜索、详情、评论 | "发布到知乎"、"搜索知乎"、"查看详情" |

## 文件输出

搜索和获取详情的结果会自动保存到 `search_results/` 目录：

```
search_results/
├── ai岗位_20260328_173948.md          # 搜索结果列表
├── 某篇文章标题_20260328_174020.md     # 帖子详情（含评论）
└── ...
```

## 项目结构

```
zhihu-mcp/
├── browser/
│   ├── __init__.py
│   └── manager.py              # Playwright 浏览器管理 + 反检测
├── zhihu/
│   ├── __init__.py
│   ├── actions.py              # 知乎浏览器自动化操作
│   ├── cookies.py              # Cookie 持久化
│   └── service.py              # 服务层（桥接 actions 与 MCP）
├── server/
│   ├── __init__.py
│   └── tools.py                # MCP 工具定义与注册
├── skills/
│   ├── zhihu-auth/
│   │   └── SKILL.md             # 认证管理技能（登录/退出）
│   └── zhihu-publish/
│       └── SKILL.md             # 内容发布技能（发布/搜索/评论）
├── cookies/                    # cookies 存储目录
├── search_results/             # 搜索结果输出目录
├── main.py                     # MCP 服务入口
├── login.py                    # 登录工具
├── requirements.txt            # Python 依赖
├── pyproject.toml              # 项目配置
├── README.md
└── .gitignore
```

## 使用示例

在 AI 助手中直接使用自然语言：

```
帮我搜索知乎上关于 "春招" 的内容
```

```
查看第 3 条的详情
```

```
获取知乎首页推荐
```

```
帮我发布一篇知乎文章，标题是 "AI 学习笔记"，内容是（标题和内容是必要字段，注意：知乎发布内容要求字数大于9个字。） ...
```

```
对这条帖子发表评论
```

## 注意事项

- **登录**：知乎同一账号不能在多处网页端同时登录，会互相踢下线
- **Cookie 过期**：如果操作提示未登录，重新运行 `python login.py` 扫码
- **频率控制**：建议每天发布不超过 5-10 篇，避免触发风控
- **发布按钮**：知乎编辑器的"发布设置"和"发布"是两个不同的按钮，程序会精确匹配"发布"
- **URL**：知乎文章编辑器地址为 `https://zhuanlan.zhihu.com/write`，不是 `https://www.zhihu.com/write`

## 风险提示

- 该项目仅供学习目的，禁止用于违法行为
- 自动化操作可能触发知乎的风控机制，建议合理控制频率
- 请妥善保管 `cookies/cookies.json` 文件，不要泄露给他人
