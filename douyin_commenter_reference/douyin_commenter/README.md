# douyin_commenter — 抖音评论发送最小调用集

从 creatorhub 项目抽取的「评论执行发送」相关代码,供其他项目直接调用。只含发送,不含规则调度、任务队列、风控、文案生成。

## 目录结构

```
douyin_commenter/
├── douyin_commenter/
│   ├── __init__.py          # 导出 DouyinClient / cookie_from_state / post_comment_browser
│   ├── client.py            # 抖音 Web 客户端(cookie + a_bogus 本地签名直连 API)
│   ├── browser_post.py      # 浏览器路径:页面模拟输入发送(原 fetcher.py 抽出,原逻辑未改)
│   ├── netfp.py             # curl_cffi TLS 指纹按 UA 匹配 Chrome 版本
│   └── signing/             # a_bogus / msToken / sm3 / rc4 本地签名实现
├── example.py               # 两条路径的调用示例
└── requirements.txt
```

## 两条调用路径

### 1. API 直连(轻量,强 Cookie 依赖)

```python
from douyin_commenter import DouyinClient, cookie_from_state

cookie = cookie_from_state(open("storage_state.json", encoding="utf-8").read())
client = DouyinClient(cookie, UA, timeout=20, proxy="")
async with client.session_scope():
    ok, cid, err = await client.post_comment(aweme_id, text, reply_comment_id="")
```

- POST `https://www.douyin.com/aweme/v1/web/comment/publish/`
- 回复评论时 `reply_comment_id` 同时带 `reply_id` + `reply_comment_id` 双字段(兼容页面不同版本)
- Cookie 失效会返回 `logged_out` / `api_missing_cookie`

### 2. 浏览器路径(稳,默认推荐)

```python
from douyin_commenter import post_comment_browser

ok, err = await post_comment_browser(mgr, identity, aweme_id, text,
                                     reply_to_text="目标评论前20字",  # 留空=顶层评论
                                     headed=True, require_reply=False)
```

- `mgr` 是鸭子类型:有 `open_headed(identity)` / `new_page(identity, block_media=...)` 即可(Playwright/Patchright API 兼容),`identity` 为含登录态的持久 profile
- **成功判据 = 拦截 comment/publish 接口响应 status_code==0**,不是"输入框是否清空"
- `headed=True`:抖音对无头写操作常降级/拦截,有头还能手动过验证码
- 选择器集中在 `browser_post.py` 顶部 `_COMMENT_INPUT` / `_COMMENT_SUBMIT`,抖音改版时改这里

## 错误语义(调用方必须遵守)

| 返回 | 含义 | 调用方动作 |
|---|---|---|
| `ok=True` | status_code=0,已发出 | 记成功 |
| `err` 以 `write_uncertain:` 开头 | 结果未知(请求未回包等) | **禁止自动重试**,人工核对后再决定 |
| `err` 含 `status_code=` 非 0 | 抖音明确拒绝(验证码/频控/风控) | 降频、长冷却(参考 6h),勿立即重试 |
| `logged_out` / `api_missing_cookie` | 登录态失效 | 换 Cookie / 重新登录 |

核心原则:**宁可不确定也不重发**——所有结果未知场景都必须人工核对,防止实际已发出又重复发。

## 依赖

```
pip install -r requirements.txt   # curl_cffi(API),patchright(浏览器)
```

注意:
- `client.py` 内还保留 follow/DM 等方法(懒加载依赖未带入),仅供评论发送时无需理会;只调用 `post_comment` 不会触发额外依赖
- 抖音无官方自动回复 API,网页私有接口随时可能改版;浏览器路径的选择器和 API 路径的签名参数都需要跟随维护
- 请自行实现频控(最小间隔、小时/日配额、失败冷却),本包不做任何节流
