"""飞书 Bitable 访问边界：鉴权、分页读取、字段值转换。"""
import re
from decimal import Decimal

import requests


BASE_URL = "https://open.feishu.cn/open-apis"


def cell_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(value, list):
        return ",".join(str(item.get("text") or item.get("name") or "").strip() if isinstance(item, dict) else str(item).strip() for item in value if item)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "").strip()
    return str(value).strip()


def cell_timestamp(value):
    return int(value // 1000) if isinstance(value, (int, float)) and value > 0 else None


def cell_money(value):
    match = re.search(r"-?\d+(?:\.\d+)?", cell_text(value).replace(",", "").replace("¥", "").replace("￥", ""))
    try:
        return Decimal(match.group(0)) if match else None
    except ArithmeticError:
        return None


class FeishuBitableClient:
    def __init__(self, app_id, app_secret, session=None):
        self.app_id, self.app_secret = app_id, app_secret
        self.session = session or requests.Session()

    def tenant_access_token(self):
        response = self.session.post(f"{BASE_URL}/auth/v3/tenant_access_token/internal", json={"app_id": self.app_id, "app_secret": self.app_secret}, timeout=20)
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0 or not body.get("tenant_access_token"):
            raise RuntimeError(f"获取飞书授权失败: {body.get('msg')}")
        return body["tenant_access_token"]

    def list_records(self, app_token, table_id):
        headers = {"Authorization": f"Bearer {self.tenant_access_token()}"}
        url = f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records"
        records, page_token = [], None
        while True:
            params = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            response = self.session.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            body = response.json()
            if body.get("code") != 0:
                raise RuntimeError(f"读取飞书数据表失败: {body.get('msg')}")
            data = body.get("data") or {}
            records.extend(data.get("items") or [])
            if not data.get("has_more"):
                return records
            page_token = data.get("page_token")
