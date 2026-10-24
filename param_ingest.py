# -*- coding: utf-8 -*-
"""参数摄取管道 · 读源 → LLM 提取 → model_facts(state='pending')

解决"提取参数"痛点：官网产品页多为前端动态渲染，scrm.py 里的正则解析
(_product_specs_from_page) 经常提不到字段。本管道把页面/文档原文交给
DeepSeek 做结构化提取，先写入 model_facts 且 state='pending'，人工在
知识库管理页(/scrm/kb/model_facts)确认后一键翻 usable。
reply-service 检索只取 state='usable'，pending 数据对回复链路不可见，
因此服务端代码零改动。

用法:
  python param_ingest.py --url https://www.haier.com/xxx.shtml            # 试提取,不落库
  python param_ingest.py --url https://www.haier.com/xxx.shtml --apply    # 提取并落 pending
  python param_ingest.py --feishu https://xxx.feishu.cn/wiki/Abc123 --apply
  python param_ingest.py --file 参数脚本.txt --brand 海尔 --category 洗护 --apply
  python param_ingest.py --text "型号 EYFW22U1 额定功率 480W ..." --apply

前置: --feishu 依赖 config.FEISHU_APP_ID/SECRET 对应自建应用已获云文档权限
      (与商单页"飞书文档同步"同一套授权;无权限时改用 --file/--text 粘贴原文)。
注意: 对已 usable 的型号重复摄取会把 state 重新翻回 pending, 强制人工复核。
"""
import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime
from html import unescape
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

from config import (DB_CONFIG, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL,
                    DEEPSEEK_MODEL, FEISHU_APP_ID, FEISHU_APP_SECRET)

logger = logging.getLogger("param_ingest")

CATEGORIES = ["空调", "冰箱", "洗护", "清洁", "净水", "空净"]

# 与 KB 管理页 specEditor 的参数键保持一致, 便于确认时直接可视化
SPEC_KEYS = {
    "series": "系列", "installation": "安装方式", "capacity": "容量",
    "drying": "干燥方式", "sterilization": "消毒方式", "rated_voltage": "额定电压",
    "rated_frequency": "额定频率", "water_consumption": "耗水量",
    "rated_power": "额定功率", "shell_material": "外壳材质", "dimensions": "外形尺寸",
    "cabinet_cutout": "橱柜预留尺寸", "programs": "洗涤程序",
}

EXTRACT_PROMPT = """你是家电产品资料结构化助手。从下面的资料原文中提取型号事实字段, 只输出 JSON 对象。

字段要求:
- brand: 品牌中文名, 如 海尔/卡萨帝/美的; 原文没有则空字符串
- model_name: 完整产品型号串(原文写法), 不要用营销名替代
- aliases: 该型号的俗称/缩写/系列名数组, 没有则 []
- category: 必须是 ["空调","冰箱","洗护","清洁","净水","空净"] 之一, 判断不了给最接近的
- launch_date: 上市日期 YYYY-MM-DD, 没有则 null
- specs: 核心参数对象。键优先用: {key_hint}; 其他参数用小写下划线英文键。每项为
  {{"value": 数值或文字, "unit": "单位, 无则省略", "condition": "适用条件/工况, 如 额定/50Hz, 无则省略"}}
  只提取原文确实写明的参数, 严禁编造或推算; 数值型参数尽量带原始数值和单位
- known_issues: 原文明确承认的已知问题数组, 每条带出现条件; 没有则 []
- notes: 资料中与参数相关但上面字段放不下的补充说明, 没有则空字符串

资料原文:
---
{content}
---"""

# 飞书 tenant_access_token 进程内缓存
_FEISHU_TOKEN = {"value": "", "expire": 0}

# 统一关闭环境代理探测, 避免本机残留 http_proxy 把请求塞进死代理
SESSION = requests.Session()
SESSION.trust_env = False


# ---------- 读源 ----------
def html_to_text(html):
    """轻量去壳: 去脚本样式与标签, 留正文文本(含动态页 SSR 出来的参数表)。"""
    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?is)<br\s*/?>|</(p|div|tr|li|h[1-6]|td|th)>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = "\n".join(line.strip() for line in unescape(text).splitlines())
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def fetch_url(url):
    """下载网页并转正文文本; 手动跟随重定向, 上限 2MB。返回(最终链接, 标题, 正文)。"""
    # 显式只声明 gzip/deflate: requests 会自动解压; brotli 未装包时避免拿到 br 乱码
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SCRM-Param-Ingest/1.0)",
               "Accept": "text/html,application/xhtml+xml",
               "Accept-Encoding": "gzip, deflate"}
    for _ in range(4):
        resp = SESSION.get(url, headers=headers, timeout=(5, 20),
                           allow_redirects=False)
        if 300 <= resp.status_code < 400 and resp.headers.get("Location"):
            url = urljoin(url, resp.headers["Location"])
            continue
        resp.raise_for_status()
        content = resp.content  # 走 requests 自动解压; resp.raw.read 拿到的可能是压缩字节
        if len(content) > 2 * 1024 * 1024:
            raise ValueError("页面超过 2MB, 请改用 --file 传入需要的片段")
        encoding = resp.encoding or ""
        if not encoding or encoding.lower() == "iso-8859-1":  # requests 对无 charset 的 text/* 默认拉丁编码
            encoding = resp.apparent_encoding or "utf-8"
        html = content.decode(encoding, errors="replace")
        title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        title = unescape(title_match.group(1)).strip() if title_match else ""
        return url, title, html_to_text(html)
    raise ValueError("重定向次数过多")


def fetch_roborock_mall_product(url):
    """读取石头商城 SPA 商品页的公开商品接口，避免只拿到空壳 HTML。

    商城的 /goods/<id> 页面本身没有服务端正文，但前端会请求公开的
    product.info 与商品详情文章接口。这里直接复用该接口，并对详情图 OCR。
    不匹配石头商城链接时返回 None，交给通用网页流程处理。
    """
    parsed = urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    match = re.fullmatch(r"/goods/(\d+)/?", parsed.path or "")
    if host not in ("mall.roborock.com", "www.mall.roborock.com") or not match:
        return None
    goods_id = match.group(1)
    headers = {"plat": "pc", "User-Agent": _UA_DESKTOP}
    response = SESSION.get("https://api.roborock.com/v2/rock.api.product.info",
                           params={"id": goods_id}, headers=headers, timeout=(5, 30))
    response.raise_for_status()
    payload = response.json()
    product = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(product, dict) or payload.get("error_code") not in (None, 0):
        raise ValueError("石头商城未返回有效商品资料")

    parts = [_flatten_json_text(json.dumps(product, ensure_ascii=False), cap=12000)]
    article_id = product.get("details_web_article_id") or product.get("details_mobile_article_id")
    if article_id:
        article = SESSION.get("https://api.roborock.com/v2/ecapi.article.getArticleContent",
                              params={"id": article_id}, headers=headers, timeout=(5, 30))
        article.raise_for_status()
        article_data = article.json().get("data") or {}
        detail_html = str(article_data.get("content") or "")
        detail_text = html_to_text(detail_html)
        if detail_text:
            parts.append(detail_text[:5000])
        # 产品规格通常以长图呈现。优先识别详情末尾的图片，最多 5 张以控制耗时。
        image_urls = re.findall(r'<img[^>]+src=["\']([^"\']+)', detail_html, re.I)
        ocr_parts = []
        for raw in reversed(image_urls[-5:]):
            try:
                if raw.startswith("data:image/"):
                    encoded = raw.split(",", 1)[1]
                    import base64
                    image_bytes = base64.b64decode(encoded)
                else:
                    image_bytes = SESSION.get(raw, headers={"User-Agent": _UA_DESKTOP}, timeout=15).content
                if not image_bytes or len(image_bytes) > 8 * 1024 * 1024:
                    continue
                text = ocr_image_bytes(image_bytes)
                if text.strip():
                    ocr_parts.append(text.strip())
                    if sum(len(item) for item in ocr_parts) >= 12000:
                        break
            except Exception:
                continue
        if ocr_parts:
            parts.append("[石头商城详情图 OCR]\n" + "\n\n".join(ocr_parts))
    content = "\n\n".join(part for part in parts if part and part.strip()).strip()
    return url, str(product.get("name") or "").strip(), content


def _feishu_token():
    now = time.time()
    if _FEISHU_TOKEN["value"] and now < _FEISHU_TOKEN["expire"]:
        return _FEISHU_TOKEN["value"]
    resp = SESSION.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=15)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取飞书授权失败: {data.get('msg')}")
    _FEISHU_TOKEN["value"] = data["tenant_access_token"]
    _FEISHU_TOKEN["expire"] = now + int(data.get("expire", 7200)) - 60
    return _FEISHU_TOKEN["value"]


def _feishu_api(path, params=None):
    resp = SESSION.get(f"https://open.feishu.cn/open-apis{path}",
                       headers={"Authorization": f"Bearer {_feishu_token()}"},
                       params=params or {}, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"飞书接口失败({path}): {payload.get('msg')} "
                           f"(请确认应用已获该文档的云文档阅读权限)")
    return payload.get("data") or {}


def fetch_feishu(url):
    """读飞书 docx/wiki 文档原文(复用商单页同款授权链路)。返回(链接, 标题, 正文)。"""
    parsed = urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "feishu.cn" or host.endswith(".feishu.cn")):
        raise ValueError("请提供 feishu.cn 域名的文档链接")
    docx_match = re.search(r"/docx/([A-Za-z0-9]+)", parsed.path)
    wiki_match = re.search(r"/wiki/([A-Za-z0-9]+)", parsed.path)
    if not docx_match and not wiki_match:
        raise ValueError("链接中未识别到 /docx/ 或 /wiki/ 文档编号")
    title, document_id = "", docx_match.group(1) if docx_match else None
    if document_id is None:
        node = _feishu_api("/wiki/v2/spaces/get_node", {"token": wiki_match.group(1)}).get("node") or {}
        document_id = node.get("obj_token")
        title = str(node.get("title") or "").strip()
        if node.get("obj_type") not in (None, "", "docx"):
            raise ValueError(f"该 wiki 节点类型为 {node.get('obj_type')}, 暂只支持 docx 文档")
    if not title:
        try:
            doc = _feishu_api(f"/docx/v1/documents/{document_id}").get("document") or {}
            title = str(doc.get("title") or "").strip()
        except RuntimeError:
            title = ""  # 标题拿不到不阻塞, 正文是刚需
    raw = _feishu_api(f"/docx/v1/documents/{document_id}/raw_content").get("content") or ""
    return parsed.path, title, raw.strip()


_UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 接口 JSON 里出现"数字+单位"才认为可能藏着参数, 滤掉导航/轮播图/二维码等无效响应
_SPEC_UNIT_RE = re.compile(r"\d+(\.\d+)?\s*(mm|cm|ml|l|w|kw|v|ah|mah|min|kpa|pa|rpm|db|kg|hz|分钟|瓦)", re.I)


def _flatten_json_text(body, cap=30000):
    """大 JSON(如商品详情富文本) → 可读文本: 递归取值, 剥 HTML, 滤 URL/图片。"""
    import html as html_lib
    try:
        data = json.loads(body)
    except ValueError:
        return body[:cap]
    out = []

    def walk(o, key=""):
        if len(out) >= 500:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, str(k))
        elif isinstance(o, list):
            for v in o[:100]:
                walk(v, key)
        elif isinstance(o, str):
            s = html_lib.unescape(re.sub(r"<[^>]+>", " ", o))
            s = re.sub(r"\s+", " ", s).strip()
            if len(s) < 2 or s.startswith(("http", "data:", "//")):
                return
            out.append(f"{key}: {s}" if key and not key.isdigit() else s)
        elif isinstance(o, (int, float)) and o not in (0, ""):
            out.append(f"{key}: {o}" if key and not key.isdigit() else str(o))

    walk(data)
    return "\n".join(out)[:cap]


def fetch_url_rendered(url, wait_ms=4000):
    """无头 Chrome 渲染动态页面(SPA), 返回 (链接, 标题, 正文=DOM文本+接口JSON+整页截图OCR)。

    需要 playwright + 本机 Chrome(channel=chrome), 缺失时抛 RuntimeError。
    很多官网把参数表做成详情图片, DOM 文本拿不到, 截图 OCR 是唯一来源。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("服务器未安装 playwright, 无法渲染动态页面 (pip install playwright)")
    if not url.startswith(("http://", "https://")):
        raise ValueError("链接无效")
    bodies, dom_text, title = [], "", ""
    ocr_text = ""
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)  # 回退 playwright 自带内核
        try:
            page = browser.new_page(user_agent=_UA_DESKTOP, viewport={"width": 1440, "height": 900})

            def on_response(resp):
                if len(bodies) >= 20:
                    return
                try:
                    if resp.request.resource_type in ("xhr", "fetch") \
                            and "json" in (resp.headers.get("content-type") or ""):
                        body = resp.text()
                        # 大响应(如商品详情富文本 700KB)不能丢, 那正是参数所在
                        if body and len(body) < 2_000_000 and not any(
                                k in resp.url for k in ("qr_code", "statistics", "log?", "track")):
                            bodies.append(body)
                except Exception:
                    pass

            page.on("response", on_response)
            page.goto(url, timeout=30000, wait_until="networkidle")
            # 逐屏滚动触发懒加载的详情图/参数表
            for _ in range(20):
                page.mouse.wheel(0, 2000)
                page.wait_for_timeout(350)
                at_bottom = page.evaluate(
                    "window.innerHeight + window.scrollY >= document.body.scrollHeight - 5")
                if at_bottom:
                    break
            page.wait_for_timeout(max(1000, wait_ms))
            title = page.title()
            dom_text = page.evaluate("document.body.innerText") or ""
            # 自动整页截图 + OCR(参数做成图片的页面, 这是唯一来源); 超长页截前 12000px
            try:
                sh = page.evaluate("document.body.scrollHeight") or 0
                clip = {"x": 0, "y": 0,
                        "width": 1440, "height": max(900, min(int(sh), 12000))}
                shot = page.screenshot(clip=clip, full_page=True)
                if len(shot) <= 8 * 1024 * 1024:
                    ocr_text = ocr_image_bytes(shot)
            except Exception:
                pass  # OCR 失败不阻塞 DOM/接口文本
        finally:
            browser.close()
    parts, total, seen = [], 0, set()
    if dom_text.strip():
        parts.append(dom_text.strip())
        total = len(dom_text)
    for body in bodies:
        if total > 50000:
            break
        if '"error_code":-1' in body or '"error_code": -1' in body:
            continue  # 接口报错(如 商品不存在/已下架), 没有提取价值
        if len(body) > 30000:
            text = _flatten_json_text(body)  # 大富文本 JSON 拍平成可读文本
        else:
            text = re.sub(r"\s+", " ", body)
        if not (_SPEC_UNIT_RE.search(text) or re.search(r"参数|规格|spec", text, re.I)):
            continue
        key = text[:200]
        if key in seen:
            continue
        seen.add(key)
        snippet = text[:25000]
        parts.append(snippet)
        total += len(snippet)
    # 详情图 OCR: 官网常把规格表做成详情图片(在接口 JSON 的富文本里), 从后往前 OCR, 凑够参数即停
    img_urls = []
    for body in bodies:
        # JSON 里斜杠是 \/ 转义的, 先还原否则 URL 正则匹配不到
        body_plain = body.replace("\\/", "/")
        for u in re.findall(r'https?://[^"\'\s<>]+?\.(?:png|jpe?g|webp)(?:\?[^"\']*)?', body_plain, re.I):
            u2 = u.replace("\\/", "/")
            if "logo" not in u2.lower() and u2 not in img_urls:
                img_urls.append(u2)
    detail_ocr = []
    total_units = 0
    if img_urls:
        for u in list(reversed(img_urls))[:8]:  # 规格表通常在详情末尾几张
            try:
                resp_img = SESSION.get(u, timeout=15)
                if len(resp_img.content) > 8 * 1024 * 1024:
                    continue
                t = ocr_image_bytes(resp_img.content)
                if t.strip():
                    detail_ocr.append(f"[详情图 {u[-40:]}]\n{t}")
                    total_units += len(_SPEC_UNIT_RE.findall(t))
                    if sum(len(p) for p in detail_ocr) > 15000:
                        break  # 内容已足够
            except Exception:
                continue
    if detail_ocr:
        parts.append("[详情图OCR]\n" + "\n\n".join(detail_ocr))
    elif ocr_text.strip():  # 没有详情图可用时才退回整页截图 OCR
        parts.append("[页面截图OCR]\n" + ocr_text.strip())
    return url, title, "\n\n".join(part for part in parts if part).strip()


def read_file(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return str(path), "", fh.read().strip()


# ---------- OCR ----------
_OCR_ENGINE = None


def ocr_image_bytes(data, max_height=12000, slice_height=2400):
    """图片字节 → 文本(RapidOCR 中文模型, 首次调用加载模型约 1-2 秒)。

    长图(电商详情图常见)切段识别, 否则整图缩放后小字检测不到。
    """
    global _OCR_ENGINE
    if not data:
        raise ValueError("图片内容为空")
    if _OCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR_ENGINE = RapidOCR()
    import io
    import numpy as np
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None  # 官网长图动辄上亿像素, 关掉 decompression bomb 拦截
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as exc:
        raise ValueError(f"图片无法解析: {exc}")
    if img.width > 1400:
        img = img.resize((1400, max(1, round(img.height * 1400 / img.width))))
    if img.height > max_height:
        img = img.crop((0, 0, img.width, max_height))

    def _ocr_arr(arr):
        result, _ = _OCR_ENGINE(arr[:, :, ::-1])  # RapidOCR 期望 BGR
        return [str(line[1]).strip() for line in (result or [])
                if len(line) >= 2 and str(line[1]).strip()]

    lines = []
    if img.height <= slice_height:
        lines = _ocr_arr(np.array(img))
    else:
        for top in range(0, img.height, slice_height):
            seg = img.crop((0, top, img.width, min(top + slice_height + 40, img.height)))
            lines.extend(_ocr_arr(np.array(seg)))
    text = "\n".join(lines)
    if not text.strip():
        raise ValueError("图片中没有识别到文字, 请确认截图包含参数内容")
    return text


# ---------- LLM 提取 ----------
def llm_extract(content, hints):
    """DeepSeek 结构化提取 model_facts 字段, 返回 dict。"""
    if not content.strip():
        raise ValueError("源内容为空, 无法提取")
    if len(content) > 60000:
        content = content[:60000] + "\n…(超长截断)"
    prompt = EXTRACT_PROMPT.format(content=content, key_hint="、".join(
        f"{k}({v})" for k, v in SPEC_KEYS.items()))
    messages = [{"role": "system", "content": "只输出合法 JSON, 不要输出任何解释或代码块标记。"}]
    if hints.get("brand") or hints.get("category") or hints.get("model_name"):
        pre = "参考信息(优先级高于原文歧义): " + json.dumps(
            {k: hints[k] for k in ("brand", "category", "model_name") if hints.get(k)},
            ensure_ascii=False)
        messages.append({"role": "system", "content": pre})
    messages.append({"role": "user", "content": prompt})
    resp = SESSION.post(f"{DEEPSEEK_BASE_URL}/chat/completions",
                        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                        json={"model": DEEPSEEK_MODEL, "messages": messages,
                              "response_format": {"type": "json_object"},
                              "temperature": 0.1, "max_tokens": 3000},
                        timeout=180)
    resp.raise_for_status()
    payload = resp.json()
    text = payload["choices"][0]["message"]["content"]
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"LLM 返回的不是合法 JSON: {text[:200]}") from exc
    if not isinstance(data, dict):
        raise ValueError("LLM 返回的 JSON 不是对象")
    return data


def derive_model_id(brand, model_name, fallback):
    if str(fallback or "").strip():
        return str(fallback).strip()[:64]
    raw = f"{brand or ''}-{model_name or ''}".upper()
    return re.sub(r"[^A-Z0-9]+", "-", raw).strip("-")[:64] or "INGEST-MODEL"


def normalize(data, hints):
    """LLM 输出 → model_facts 行; 缺品牌/型号直接报错, 价格三元组不全则整体丢弃。"""
    brand = str(data.get("brand") or hints.get("brand") or "").strip()[:32]
    model_name = str(data.get("model_name") or hints.get("model_name") or "").strip()[:128]
    if not brand or not model_name:
        raise ValueError("提取结果缺少品牌或产品型号, 请补 --brand/--model-name 或改换资料")

    category = str(data.get("category") or hints.get("category") or "").strip()[:16]
    if category not in CATEGORIES:
        if category:
            logger.warning("品类 %r 不在标准枚举中, 按原文保留, 确认时可改", category)
        category = category or "洗护"

    aliases = data.get("aliases") or []
    if isinstance(aliases, str):
        aliases = re.split(r"[,，;；\n]+", aliases)
    aliases = [str(x).strip()[:128] for x in (aliases or []) if str(x).strip()][:20]

    specs = {}
    for key, item in (data.get("specs") or {}).items():
        key = re.sub(r"\s+", "_", str(key).strip().lower())[:64]
        if not key or not isinstance(item, dict) or item.get("value") in (None, ""):
            continue
        entry = {"value": item["value"] if isinstance(item["value"], (int, float)) else str(item["value"])}
        if str(item.get("unit") or "").strip():
            entry["unit"] = str(item["unit"]).strip()[:16]
        if str(item.get("condition") or "").strip():
            entry["condition"] = str(item["condition"]).strip()[:120]
        specs[key] = entry
    if not specs:
        raise ValueError("未提取到任何核心参数, 请确认资料里确实包含参数表")

    price_min, price_max, price_step = data.get("price_min"), data.get("price_max"), data.get("price_step")
    try:
        price_min, price_max = int(price_min), int(price_max)
        if price_max <= price_min or price_min < 0 or int(price_step) not in (300, 500) \
                or price_min % int(price_step) or price_max % int(price_step):
            raise ValueError
    except (TypeError, ValueError):
        price_min = price_max = price_step = None  # 区间不完整/不对齐则不存价格

    known_issues = data.get("known_issues") or []
    if not isinstance(known_issues, list):
        known_issues = [str(known_issues)]
    launch_date = str(data.get("launch_date") or "").strip() or None
    if launch_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", launch_date):
        launch_date = None

    return {
        "brand": brand, "model_name": model_name, "aliases": aliases,
        "category": category, "launch_date": launch_date,
        "price_min": price_min, "price_max": price_max, "price_step": price_step,
        "specs": specs,
        "known_issues": [str(x).strip()[:255] for x in known_issues if str(x).strip()][:10],
        "notes": str(data.get("notes") or "").strip()[:500],
    }


# ---------- 落库 ----------
def upsert_pending(fields, source, model_id):
    """INSERT/UPDATE model_facts, 一律落 state='pending' 等人工确认。"""
    import pymysql
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT source FROM model_facts WHERE model_id=%s", (model_id,))
            row = cur.fetchone()
            if row:
                try:
                    previous = json.loads(row.get("source") or "null")
                except ValueError:
                    previous = None
                source["previous_source"] = previous
            source = json.dumps(source, ensure_ascii=False)
            # base 顺序: brand,model_name,aliases,category,launch_date,price_min,
            #            price_max,price_step,specs,known_issues,source
            base = (fields["brand"], fields["model_name"],
                    json.dumps(fields["aliases"], ensure_ascii=False), fields["category"],
                    fields["launch_date"], fields["price_min"], fields["price_max"],
                    fields["price_step"], json.dumps(fields["specs"], ensure_ascii=False),
                    json.dumps(fields["known_issues"], ensure_ascii=False), source)
            if row:
                cur.execute("""UPDATE model_facts SET brand=%s, model_name=%s, aliases=%s,
                               category=%s, launch_date=%s, price_min=%s, price_max=%s, price_step=%s,
                               specs=%s, known_issues=%s, source=%s,
                               confidence='low', verified_at=%s, state='pending'
                               WHERE model_id=%s""", base + (fields["verified_at"], model_id))
            else:
                cur.execute("""INSERT INTO model_facts
                               (brand,model_name,aliases,category,launch_date,price_min,price_max,
                                price_step,specs,known_issues,source,tested_by,confidence,verified_at,
                                state,model_id)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'[]','low',%s,'pending',%s)""",
                            base + (fields["verified_at"], model_id))
        conn.commit()
        return "updated" if row else "created"
    finally:
        conn.close()


def ingest(source_type, source_ref, source_title, content, hints, apply):
    fields = normalize(llm_extract(content, hints), hints)
    model_id = derive_model_id(fields["brand"], fields["model_name"], hints.get("model_id"))
    fields["model_id"] = model_id
    fields["verified_at"] = datetime.now().date().isoformat()
    source = {
        "type": "摄取管道",
        "source_type": source_type,           # webpage / feishu_doc / file / text
        "url": source_ref,                    # 原文链接(网页为最终跳转后的链接)
        "title": source_title,
        "extracted_at": datetime.now().isoformat(timespec="seconds"),
        "extractor": DEEPSEEK_MODEL,
    }
    print(json.dumps({"model_id": model_id, "source": source, **fields},
                     ensure_ascii=False, indent=2))
    if not apply:
        print(f"\n[dry-run] 未写库。加 --apply 写入 state='pending'。")
        return
    action = upsert_pending(fields, source, model_id)
    print(f"\n[apply] {action}: {model_id} → state='pending' "
          f"(去 /scrm/kb/model_facts 确认翻 usable)")


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="参数摄取管道: 读源 → LLM 提取 → pending")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="官网产品页等公开网页链接")
    src.add_argument("--feishu", help="飞书 docx/wiki 文档链接")
    src.add_argument("--file", help="本地文本/参数脚本路径")
    src.add_argument("--text", help="直接传入资料原文")
    parser.add_argument("--brand", help="参考品牌(原文歧义时优先)")
    parser.add_argument("--model-name", help="参考产品型号")
    parser.add_argument("--model-id", help="指定型号主键(默认按 品牌-型号 生成)")
    parser.add_argument("--category", choices=CATEGORIES, help="参考品类")
    parser.add_argument("--apply", action="store_true", help="写入 model_facts(state=pending)")
    args = parser.parse_args(argv)

    hints = {"brand": args.brand, "model_name": args.model_name,
             "model_id": args.model_id, "category": args.category}
    try:
        if args.url:
            final_url, title, content = fetch_url(args.url)
            if len(content.strip()) < 200:
                final_url, title, content = fetch_url_rendered(final_url)
            if len(content.strip()) < 200:
                raise ValueError(f"该页面静态与动态渲染后都没有可提取的正文"
                                 "(可能商品已下架/链接无效), 请改用 --file/--text 粘贴参数内容")
            ingest("webpage", final_url, title, content, hints, args.apply)
        elif args.feishu:
            ref, title, content = fetch_feishu(args.feishu)
            ingest("feishu_doc", ref, title, content, hints, args.apply)
        elif args.file:
            ref, title, content = read_file(args.file)
            ingest("file", ref, title, content, hints, args.apply)
        else:
            ingest("text", "", "", args.text, hints, args.apply)
    except (ValueError, RuntimeError) as exc:
        print(f"摄取失败: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"网络请求失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
