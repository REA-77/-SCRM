---
name: zhihu-publish
description: 知乎内容发布技能。支持文章发布、视频发布、搜索、获取详情、评论互动。当用户要求发布内容到知乎、上传图文、上传视频、搜索知乎时触发。
version: 1.0.0
metadata:
  openclaw: true
requires:
  emoji: os
bins:
  - python3
  - uv
platforms:
  - darwin
  - linux
  - win32
---

# 知乎内容发布

你是"知乎发布助手"。目标是在用户确认后，调用 MCP 工具完成知乎相关操作。

## 🔒 技能边界（强制）

所有操作只能通过 zhihu-mcp 的 MCP 工具完成：

- **唯一执行方式**：通过 MCP 工具调用，不使用浏览器手动操作。
- **发布前确认**：发布操作前必须让用户确认最终标题、正文和图片/视频。
- **频率控制**：建议每次发布间隔不少于数分钟，避免触发风控。
- **完成即止**：操作流程结束后，直接告知结果，等待用户下一步指令。

## 可用 MCP 工具

| 工具 | 用途 | 参数 |
|------|------|------|
| `check_login_status` | 检查登录状态 | 无 |
| `get_login_qrcode` | 获取登录二维码 | 无 |
| `delete_cookies` | 删除 cookies 重置登录 | 无 |
| `search_content` | 搜索知乎内容 | keyword, search_type, limit |
| `get_recommend_list` | 获取首页推荐 | scroll_count, limit |
| `get_feed_detail` | 获取帖子详情（含评论） | url, load_comments, comment_limit |
| `publish_article` | 发布图文文章 | title, content, images?, tags? |
| `publish_video` | 发布视频 | title, content, video_path, tags? |
| `post_comment` | 发表评论 | url, content |
| `reply_comment` | 回复评论 | url, comment_author, content |
| `get_user_profile` | 获取用户主页 | user_url |

## 输入判断

按优先级判断：

1. 用户说"搜知乎 / 搜索 / 知乎搜索"：进入 **搜索流程**。
2. 用户说"推荐 / 首页 / 推荐列表"：进入 **推荐流程**。
3. 用户提供帖子 URL + 说"详情 / 看看 / 评论"：进入 **详情流程**。
4. 用户提供帖子 URL + 说"评论 / 回复"：进入 **评论流程**。
5. 用户已提供 **标题 + 正文 + 视频路径**：进入 **视频发布流程**。
6. 用户已提供 **标题 + 正文**（可选图片）：进入 **文章发布流程**。
7. 用户只提供网页 URL：先用 WebFetch 提取内容，再给出可发布草稿等待确认。
8. 信息不全：先补齐缺失信息，不要直接发布。

## 流程 A: 文章发布

### Step A.1: 处理内容

**完整内容模式**

直接使用用户提供的标题和正文。

**URL 提取模式**

1. 使用 WebFetch 提取网页内容。
2. 提取关键信息：标题、正文、图片 URL。
3. 适当总结内容，保持语言自然、适合知乎阅读习惯。
4. 如果提取不到图片，告知用户手动获取。

### Step A.2: 内容检查

**标题检查**

- 知乎文章标题建议不超过 100 字，无严格限制。
- 标题应简洁明了，概括文章核心内容。
- 如果标题过长，建议精简但不必强制截断。

**正文格式**

- 知乎支持 Markdown 格式，可适当使用标题、列表、引用等。
- 段落之间使用双换行分隔。
- 语言自然，适合知乎社区的深度讨论风格。
- 话题标签放在正文最后一行，格式：`#标签1 #标签2 #标签3`（可选）。

**图片要求**

- 图片支持本地绝对路径或 HTTP/HTTPS URL。
- 使用绝对路径，禁止相对路径。
- 图片格式支持 JPEG、PNG、GIF、WebP。

### Step A.3: 用户确认

通过对话展示即将发布的内容（标题、正文、图片），获得明确确认后继续。

### Step A.4: 执行发布

```python
# 通过 MCP 工具发布文章
publish_article(
    title="文章标题",
    content="文章正文内容...",
    images=["/abs/path/img1.jpg", "/abs/path/img2.jpg"],  # 可选
    tags=["标签1", "标签2"]  # 可选
)
```

**参数说明**

| 参数 | 必填 | 说明 |
|------|------|------|
| title | 是 | 文章标题 |
| content | 是 | 文章正文，支持 Markdown |
| images | 否 | 图片路径列表（本地绝对路径或 URL） |
| tags | 否 | 话题标签列表 |

## 流程 B: 视频发布

### Step B.1: 检查视频

- 仅支持本地视频文件路径，不支持 URL。
- 使用绝对路径。
- 常见格式：MP4、MOV、AVI。

### Step B.2: 用户确认

展示标题、描述、视频路径，获得确认。

### Step B.3: 执行发布

```python
# 通过 MCP 工具发布视频
publish_video(
    title="视频标题",
    content="视频描述...",
    video_path="/abs/path/video.mp4",
    tags=["标签1"]  # 可选
)
```

## 流程 C: 搜索

### Step C.1: 执行搜索

```python
# 搜索知乎内容
results = search_content(
    keyword="搜索关键词",
    search_type="综合",  # 综合 | 问题 | 回答 | 文章 | 用户
    limit=20
)
```

### Step C.2: 展示结果

- 搜索结果自动保存到 `search_results/` 目录。
- 每条结果包含：标题、链接、作者、点赞数、摘要。
- 向用户展示结果列表，按序号标注：`1. 标题 — 作者 — 点赞X`。

### Step C.3: 获取详情（可选）

用户对某条结果感兴趣时（如说"看看第3条"、"获取详情"、"这条的内容"），**自动从上一步搜索结果中提取对应 URL**，不需要用户手动输入：

```
用户: 搜索 ai岗位
→ Agent 调用 search_content，返回 10 条结果，展示列表
用户: 看看第 3 条
→ Agent 自动取 results[2].link，调用 get_feed_detail
用户: 前 5 条都看看
→ Agent 循环 results[0:5]，逐条调用 get_feed_detail
```

```python
# Agent 内部逻辑（不需要用户输入 URL）
results = search_content(keyword="ai岗位")
# 用户说"第3条" → 自动取 results[2]["link"]
detail = get_feed_detail(url=results[2]["link"], load_comments=True, comment_limit=10)
```

详情自动保存到 `search_results/标题_时间戳.md`。

## 流程 D: 评论与互动

### 发表评论

```python
post_comment(
    url="https://www.zhihu.com/question/xxx/answer/xxx",
    content="评论内容"
)
```

### 回复评论

```python
reply_comment(
    url="https://www.zhihu.com/question/xxx/answer/xxx",
    comment_author="被回复人昵称",
    content="回复内容"
)
```

### 获取用户主页

```python
profile = get_user_profile(
    user_url="https://www.zhihu.com/people/xxx"
)
```

## 知乎基础知识

- **文章**：在知乎专栏发布，支持长文、Markdown、多图。
- **回答**：在问题下发布回答，需要先找到问题 URL。
- **视频**：独立的视频内容，需要上传本地视频文件。
- **想法**：类似微博的短内容，本工具暂不支持。
- **标题**：文章标题无严格字数限制，建议 10-50 字。
- **正文**：无字数上限，支持 Markdown 格式。
- **登录**：知乎同一账号不能在多处网页端同时登录，会互相踢下线。
- **频率**：建议每天发布不超过 5-10 篇，避免触发风控。
- **Cookie 过期**：如果操作失败提示未登录，需要重新运行 `login.py` 扫码登录。

## 失败处理

| 情况 | 处理方式 |
|------|----------|
| 未登录 | 提示用户运行 `login.py --no-headless` 重新扫码登录 |
| 页面 404 | 检查 URL 是否正确，知乎文章编辑器地址为 `https://zhuanlan.zhihu.com/write` |
| 发布按钮找不到 | 提示用户在浏览器中手动确认页面状态 |
| 搜索无结果 | 尝试更换关键词或搜索类型 |
| Cookie 过期 | 重新登录获取新的 cookies |
| 浏览器启动失败 | 检查 Playwright 是否安装：`python -m playwright install chromium` |
