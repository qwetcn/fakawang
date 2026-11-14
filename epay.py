# -*- coding: utf-8 -*-
"""易支付 V1 协议(兼容彩虹易支付及各兼容网关)。

签名规则(多家官方文档一致):
1. 剔除 sign、sign_type 和值为空的参数;
2. 按参数名 ASCII 升序排列,拼成 k1=v1&k2=v2,值不做 URL 编码;
3. 末尾直接追加商户密钥(无分隔符),MD5 取 32 位小写。
异步通知验签用的是 URL 解码后的参数值;发货只以异步通知为准。
"""
import hashlib
import hmac
import json
import urllib.parse
import urllib.request

SIGN_TYPE = "MD5"


def sign_params(params, key):
    """按易支付规则计算签名。params 为 dict,值会被转成 str。"""
    items = [(str(k), str(v)) for k, v in params.items()
             if k not in ("sign", "sign_type") and v is not None and str(v) != ""]
    items.sort(key=lambda kv: kv[0])
    base = "&".join(f"{k}={v}" for k, v in items)
    return hashlib.md5((base + key).encode("utf-8")).hexdigest()


def verify_params(params, key):
    """验签。params 需为解码后的参数 dict(含 sign)。"""
    sign = params.get("sign", "")
    if not sign:
        return False
    expect = sign_params(params, key)
    return hmac.compare_digest(expect.lower(), str(sign).lower())


def build_pay_url(gateway, pid, key, pay_type, out_trade_no,
                  notify_url, return_url, name, money):
    """生成 submit.php 跳转地址。money 为 '12.28' 格式字符串。"""
    params = {
        "pid": pid,
        "type": pay_type,
        "out_trade_no": out_trade_no,
        "notify_url": notify_url,
        "return_url": return_url,
        "name": name,
        "money": money,
    }
    params["sign"] = sign_params(params, key)
    params["sign_type"] = SIGN_TYPE
    base = gateway.rstrip("/")
    return base + "/submit.php?" + urllib.parse.urlencode(params)


def query_order(gateway, pid, key, out_trade_no, timeout=15, act="query"):
    """查单:GET {gateway}/api.php?act=query&pid=&key=&out_trade_no=。
    成功返回网关 JSON dict,失败/异常返回 None。调用方自行判断 trade_status。"""
    qs = urllib.parse.urlencode({
        "act": act, "pid": pid, "key": key, "out_trade_no": out_trade_no})
    url = gateway.rstrip("/") + "/api.php?" + qs
    req = urllib.request.Request(url, headers={"User-Agent": "fakawang/1.0"})
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        if host in ("localhost", "127.0.0.1", "::1"):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            resp = opener.open(req, timeout=timeout)
        else:
            resp = urllib.request.urlopen(req, timeout=timeout)
        with resp:
            body = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def query_is_paid(data):
    """判断查单结果是否已支付。兼容 code/status 两种返回风格。"""
    if not data:
        return False
    if data.get("trade_status") == "TRADE_SUCCESS":
        return True
    # 个别网关查单成功时直接给 trade_status 之外的字段,保守起见只认 TRADE_SUCCESS
    return False
