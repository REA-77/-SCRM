# -*- coding: utf-8 -*-
"""The only HTTP boundary from the operations system to reply-service."""
import logging
import uuid

import requests

import config

logger = logging.getLogger("reply_service_client")

# Reply-service is deployed on the local network.  It must not inherit a
# desktop/system HTTP proxy: those proxies often cannot route private IPs and
# turn an otherwise healthy service call into a misleading 502 response.
_session = requests.Session()
_session.trust_env = False


def _water_tank_reply_fallback(context, result):
    """Prevent a safe, actionable water-tank question from being silently dropped.

    The remote service may elect ``no_reply`` when an exact tank-capacity value
    is absent from the knowledge base.  That is appropriate for an unverifiable
    numeric answer, but not for this recurring pre-purchase question: a useful
    answer can safely explain the usage factors without claiming a capacity.
    """
    analysis = result.get("analysis") or {}
    text = str(context.get("comment") or "")
    asks_about_water_tank = any(word in text for word in ("污水箱", "倒污水", "倒水", "拖一半"))
    if not (
        result.get("action") == "no_reply"
        and analysis.get("has_purchase_intent") is True
        and analysis.get("is_negative") is False
        and asks_about_water_tank
    ):
        return result
    result = dict(result)
    result["action"] = "reply"
    result["reply"] = (
        "水箱版日常拖地通常够用；实际倒水频率会受户型、用水档位和地面脏污程度影响。"
        "如果更在意少倒水，建议优先看上下水版。"
    )
    result["fallback_reason"] = "water_tank_pre_purchase_question"
    logger.warning("回复服务误判 no_reply，已使用水箱咨询安全回复兜底")
    return result


def generate_reply(context, mode, timeout=None, analysis_required=False):
    """Return a normalized result; transport failures never create a local reply."""
    base_url = str(getattr(config, "REPLY_SERVICE_URL", "") or "").rstrip("/")
    if not base_url:
        return {"available": False, "error": "回复服务未配置"}
    payload = dict(context)
    payload["mode"] = mode
    if analysis_required:
        payload["analysis_required"] = True
    headers = {"Content-Type": "application/json", "X-Idempotency-Key": str(uuid.uuid4())}
    api_key = str(getattr(config, "REPLY_SERVICE_API_KEY", "") or "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = _session.post(
            base_url + "/v1/replies/generate", json=payload, headers=headers,
            timeout=timeout or int(getattr(config, "REPLY_SERVICE_TIMEOUT_SECONDS", 70)),
        )
        data = response.json()
    except requests.ConnectionError as exc:
        logger.warning("回复服务连接失败（%s）: %s", base_url, exc)
        return {"available": False, "error": "无法连接回复服务，请检查服务地址和 5100 端口是否已启动"}
    except (requests.Timeout, requests.RequestException, ValueError) as exc:
        logger.warning("回复服务请求失败: %s", exc)
        return {"available": False, "error": "回复服务不可用"}
    if not isinstance(data, dict):
        return {"available": False, "error": "回复服务返回了无效响应"}
    if not response.ok:
        message = ((data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else "") or "回复服务请求失败"
        return {"available": False, "error": message, "status_code": response.status_code}
    if data.get("action") not in {"reply", "no_reply", "human_review"}:
        return {"available": False, "error": "回复服务返回了无效决策"}
    data["available"] = True
    return data


def evaluate_comment(context, mode, timeout=None):
    """Get one authoritative analysis-and-reply decision from reply-service.

    The service owns model invocation and must return `analysis.is_negative` and
    `analysis.has_purchase_intent` for every successful decision, including
    `no_reply` and `human_review` outcomes.
    """
    result = generate_reply(context, mode, timeout, analysis_required=True)
    if not result.get("available"):
        return result
    analysis = result.get("analysis")
    if not isinstance(analysis, dict) or not isinstance(analysis.get("is_negative"), bool) \
            or not isinstance(analysis.get("has_purchase_intent"), bool):
        return {"available": False, "error": "回复服务未返回有效分析结果，请升级至分析回复一体化协议"}
    result["analysis"] = {
        "is_negative": analysis["is_negative"],
        "has_purchase_intent": analysis["has_purchase_intent"],
        "comment_type": str(analysis.get("comment_type") or "")[:64],
        "confidence": analysis.get("confidence"),
        "reasons": analysis.get("reasons") if isinstance(analysis.get("reasons"), list) else [],
    }
    return _water_tank_reply_fallback(context, result)
