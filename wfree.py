# -*- coding: utf-8 -*-
"""微信免签(个人收款码 + 监听软件回调)。

监听软件在手机/电脑上检测到微信收款后,向本店回调接口 POST 一条
JSON,本店按订单号或金额匹配未支付订单并自动发货。回调签名:
HMAC-SHA256,明文为排序后的 k=v&... 串(剔除 sign 与空值),密钥为
后台配置的回调密钥。监听软件需由店主自行部署并保持在线。
"""
import hashlib
import hmac
import json
import re
from decimal import Decimal, InvalidOperation


def sign_payload(params, secret):
    items = [(str(k), str(v)) for k, v in params.items()
             if k != "sign" and v is not None and str(v) != ""]
    items.sort(key=lambda kv: kv[0])
    base = "&".join(f"{k}={v}" for k, v in items)
    return hmac.new(secret.encode("utf-8"), base.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def verify_payload(params, secret):
    sign = params.get("sign", "")
    if not sign or not secret:
        return False
    return hmac.compare_digest(sign_payload(params, secret).lower(), str(sign).lower())


def dedup_key(params):
    txid = str(params.get("txid", "") or "").strip()
    if txid:
        return "txid:" + txid
    raw = "|".join([str(params.get("amount", "")),
                    str(params.get("paid_at", "")),
                    str(params.get("payer", ""))])
    return "hash:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def canonical_json(params):
    return json.dumps(params, ensure_ascii=False, sort_keys=True)


# ---------- 通知转发(短信转发器等)文本解析 ----------
_AMOUNT_RES = [
    re.compile(r"收款[^0-9￥¥]{0,8}[￥¥]?\s*(\d+(?:\.\d{1,2})?)"),
    re.compile(r"到账[^0-9￥¥]{0,8}[￥¥]?\s*(\d+(?:\.\d{1,2})?)"),
    re.compile(r"[￥¥]\s*(\d+(?:\.\d{1,2})?)"),
    re.compile(r"(\d+\.\d{2})\s*元"),
]


def is_payment_text(text):
    """通知文本是否像一笔收款(标题+内容合并后判断)。"""
    text = text or ""
    return ("收款" in text) or ("到账" in text)


def extract_amount_cents(text):
    """从收款通知文本里提取金额(分)。累计/合计金额会被跳过,提不到返回 None。"""
    text = re.sub(r"(?<=\d),(?=\d)", "", text or "")
    for rx in _AMOUNT_RES:
        for m in rx.finditer(text):
            pre = text[max(0, m.start(1) - 6):m.start(1)]
            if "累计" in pre or "合计" in pre or "共计" in pre:
                continue
            try:
                d = Decimal(m.group(1))
            except InvalidOperation:
                continue
            if d <= 0 or d != d.quantize(Decimal("0.01")):
                continue
            return int(d * 100)
    return None
