# -*- coding: utf-8 -*-
"""支付宝开放平台直连:当面付(alipay.trade.precreate)。

预下单返回 qr_code 链接:既能生成二维码扫码付款,手机浏览器直接打开
这个链接也会跳转到支付宝 App/收银台付款,所以付款页同时支持扫码和
自动跳转。凭证:开放平台应用的 APPID + 应用私钥 + 支付宝公钥(应用
维度的那个,不是平台总公钥)。签名 RSA2(SHA256withRSA)。响应验签:对
原始 JSON 中响应节点子串验签,防止伪造二维码。
"""
import base64
import binascii
import json
import re
import time
import urllib.parse
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

DEFAULT_GATEWAY = "https://openapi.alipay.com/gateway.do"


class AlipayError(Exception):
    pass


def _normalize_pem(pem_text, default_header):
    """容忍各种粘贴格式:带/不带头尾、单行、带空格。返回标准 PEM。"""
    if not pem_text:
        raise ValueError("密钥为空")
    text = pem_text.strip()
    header = default_header
    upper = text.upper()
    if "RSA PRIVATE KEY" in upper:
        header = "RSA PRIVATE KEY"
    elif "PRIVATE KEY" in upper:
        header = "PRIVATE KEY"
    elif "PUBLIC KEY" in upper:
        header = "PUBLIC KEY"
    body = re.sub(r"-----BEGIN [^-]+-----|-----END [^-]+-----", "", text)
    body = re.sub(r"\s+", "", body)
    try:
        base64.b64decode(body)
    except (binascii.Error, ValueError):
        raise ValueError("密钥不是有效的 Base64 内容")
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return f"-----BEGIN {header}-----\n" + "\n".join(lines) + f"\n-----END {header}-----\n"


def load_private_key(pem_text):
    try:
        pem = _normalize_pem(pem_text, "PRIVATE KEY")
        return serialization.load_pem_private_key(pem.encode(), password=None)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"应用私钥无法解析: {e}")


def load_public_key(pem_text):
    try:
        pem = _normalize_pem(pem_text, "PUBLIC KEY")
        return serialization.load_pem_public_key(pem.encode())
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"支付宝公钥无法解析: {e}")


def sign_content(content, private_key):
    sig = private_key.sign(content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


def verify_content(content, signature_b64, public_key):
    try:
        sig = base64.b64decode(signature_b64)
        public_key.verify(sig, content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        return True
    except (InvalidSignature, ValueError, binascii.Error):
        return False


def build_sign_content(params):
    items = [(str(k), str(v)) for k, v in params.items()
             if k not in ("sign", "sign_type") and v is not None and str(v) != ""]
    items.sort(key=lambda kv: kv[0])
    return "&".join(f"{k}={v}" for k, v in items)


def sign_params(params, private_pem):
    return sign_content(build_sign_content(params), load_private_key(private_pem))


def verify_params(params, public_pem):
    sign = params.get("sign", "")
    if not sign:
        return False
    return verify_content(build_sign_content(params), str(sign), load_public_key(public_pem))


def _extract_node(raw_text, node_key):
    """从原始 JSON 文本中抠出响应节点的精确子串(验签用)。"""
    key_pat = '"%s"' % node_key
    idx = raw_text.find(key_pat)
    if idx < 0:
        raise AlipayError(f"响应中没有节点 {node_key}")
    brace = raw_text.find("{", idx + len(key_pat))
    if brace < 0:
        raise AlipayError("响应节点格式错误")
    depth = 0
    in_str = False
    esc = False
    for i in range(brace, len(raw_text)):
        ch = raw_text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw_text[brace:i + 1]
    raise AlipayError("响应节点 JSON 不完整")


def verify_response(raw_text, node_key, public_pem):
    """验响应签名。返回解析后的节点 dict;验签失败抛 AlipayError。"""
    try:
        data = json.loads(raw_text)
    except ValueError:
        raise AlipayError("网关返回的不是 JSON")
    node = data.get(node_key)
    if not isinstance(node, dict):
        raise AlipayError(f"响应中没有节点 {node_key}")
    sign = data.get("sign", "")
    if not sign:
        raise AlipayError("响应缺少签名")
    content = _extract_node(raw_text, node_key)
    if not verify_content(content, str(sign), load_public_key(public_pem)):
        raise AlipayError("响应签名校验失败")
    return node


def _open(req, timeout):
    """本地地址直连(绕过代理环境变量),其余走默认 opener(尊重代理设置)。"""
    host = urllib.parse.urlparse(req.full_url).hostname or ""
    if host in ("localhost", "127.0.0.1", "::1"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _call(cfg, method, biz_content, timeout=20):
    gateway = (cfg.get("gateway") or DEFAULT_GATEWAY).rstrip("/")
    params = {
        "app_id": cfg["appid"],
        "method": method,
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":")),
    }
    if cfg.get("notify_url"):
        params["notify_url"] = cfg["notify_url"]
    params["sign"] = sign_params(params, cfg["private_key"])
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(gateway, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with _open(req, timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        raise AlipayError(f"请求支付宝网关失败: {e}")
    node_key = method.replace(".", "_") + "_response"
    return verify_response(raw, node_key, cfg["public_key"])


def precreate(cfg, out_trade_no, total_amount, subject, notify_url, timeout=20):
    """当面付预下单,返回 qr_code 链接。失败抛 AlipayError。"""
    cfg = dict(cfg)
    cfg["notify_url"] = notify_url
    node = _call(cfg, "alipay.trade.precreate", {
        "out_trade_no": out_trade_no,
        "total_amount": f"{total_amount:.2f}",
        "subject": subject,
        "timeout_express": "30m",
    }, timeout=timeout)
    if node.get("code") != "10000":
        raise AlipayError(node.get("sub_msg") or node.get("msg") or f"预下单失败 code={node.get('code')}")
    qr = node.get("qr_code")
    if not qr:
        raise AlipayError("预下单成功但未返回二维码")
    return qr


def query(cfg, out_trade_no, timeout=15):
    """查单,返回响应节点 dict。"""
    return _call(cfg, "alipay.trade.query", {"out_trade_no": out_trade_no}, timeout=timeout)


def query_is_paid(node):
    return node.get("trade_status") in ("TRADE_SUCCESS", "TRADE_FINISHED")


def verify_notify(params, public_pem, app_id):
    """异步通知验签 + app_id 核对。params 为解码后的表单参数。"""
    if params.get("app_id") != app_id:
        return False
    return verify_params(params, public_pem)
