# -*- coding: utf-8 -*-
"""发卡网:商品 + 卡密库存 + 在线支付 + 自动发货 + 后台管理。

支付通道(三选一或多选,配置都在后台「支付」里):
1. 支付宝当面付:直连支付宝开放平台,预下单生成二维码,手机打开付款页自动跳转支付宝 App,异步通知发货;
2. 微信免签:个人收款码 + 监听软件回调,无需商户号;
3. 易支付:兼容易支付 V1 协议的聚合网关(网关地址/商户号/密钥)。
部署:见 README.md。
"""
import base64
import hmac
import io
import json
import os
import re
import secrets
import threading
import time
from decimal import Decimal, InvalidOperation

from flask import Flask, abort, jsonify, redirect, render_template, request, session
from markupsafe import escape as _esc
from werkzeug.middleware.proxy_fix import ProxyFix

import alipay
import epay
import mailer
import wfree
from store import Store, seed_demo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_dotenv():
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


load_dotenv()

app = Flask(__name__)
# 反代部署时信任一级代理传来的真实 IP 与协议(限流与回调地址依赖它)。
# 8000 端口只监听本机(见 docker-compose),公网流量必须经反代,伪造无效。
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# ---------- 配置 ----------
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "shop.db"))
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")


def _read_or_create_secret(path, gen):
    """读现有密钥文件;不存在则原子创建(O_EXCL,0600)。返回 (值, 是否本次创建)。
    多 worker 同时启动时,后到的进程会等先到的进程写完(文件存在但为空时
    轮询),绝不互相覆盖;残留的空文件(写一半时进程崩溃)超过 5 秒会被删掉重建。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for _ in range(100):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            v = gen()
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(v)
            return v, True
        except FileExistsError:
            try:
                st = os.stat(path)
                v = open(path, encoding="utf-8").read().strip()
            except OSError:
                v, st = "", None
            if v:
                return v, False
            if st is not None and st.st_size == 0 and time.time() - st.st_mtime > 5:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            time.sleep(0.1)
    raise RuntimeError(f"secret file stuck empty: {path}")


def _admin_password():
    """后台密码:优先 .env;未设或为空时自动生成强密码存入 data/.admin_password
    (0600),并在首次启动日志里打印一次。绝不使用任何内置默认值。"""
    v = os.environ.get("ADMIN_PASSWORD", "").strip()
    if v:
        if len(v) < 8:
            print("[fakawang] 警告:ADMIN_PASSWORD 少于 8 位,建议改成更强的密码。", flush=True)
        return v
    p = os.path.join(BASE_DIR, "data", ".admin_password")
    v, created = _read_or_create_secret(p, lambda: secrets.token_urlsafe(16))
    if created:
        print("[fakawang] 未设置 ADMIN_PASSWORD,已自动生成后台密码(见 data/.admin_password),"
              "请登录后在 .env 里改成自己的密码。", flush=True)
        print(f"[fakawang] 后台初始密码: {v}", flush=True)
    return v


ADMIN_PASSWORD = _admin_password()

PAY_DEFAULTS = {
    "pay_epay_enabled": "0",
    "pay_epay_url": "",
    "pay_epay_pid": "",
    "pay_epay_key": "",
    "pay_epay_types": "alipay:支付宝,wxpay:微信支付",
    "pay_alipay_enabled": "0",
    "pay_alipay_appid": "",
    "pay_alipay_private_key": "",
    "pay_alipay_public_key": "",
    "pay_alipay_gateway": alipay.DEFAULT_GATEWAY,
    "pay_wechat_enabled": "0",
    "pay_wechat_qr_text": "",
    "pay_wechat_qr_image": "",
    "pay_wechat_secret": "",
    "pay_wechat_window": "30",
}


def _secret_key():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    p = os.path.join(BASE_DIR, "data", ".secret")
    v, _ = _read_or_create_secret(p, lambda: secrets.token_hex(32))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return v


app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=SITE_URL.startswith("https://"),
                  PERMANENT_SESSION_LIFETIME=43200,
                  MAX_CONTENT_LENGTH=5 * 1024 * 1024)

store = Store(DB_PATH)


def seed_pay_settings():
    """首次建库时用 .env 里的易支付配置做初始值,之后以管理后台为准。"""
    s = store.get_settings()
    if s.get("pay_seeded") == "1":
        return
    for key, env_key in (("pay_epay_url", "EPAY_URL"), ("pay_epay_pid", "EPAY_PID"),
                         ("pay_epay_key", "EPAY_KEY"), ("pay_epay_types", "EPAY_TYPES")):
        v = os.environ.get(env_key, "").strip()
        if v:
            store.set_setting(key, v)
    if os.environ.get("EPAY_URL") and os.environ.get("EPAY_PID") and os.environ.get("EPAY_KEY"):
        store.set_setting("pay_epay_enabled", "1")
    if not s.get("pay_wechat_secret"):
        store.set_setting("pay_wechat_secret", secrets.token_hex(16))
    store.set_setting("pay_seeded", "1")


seed_pay_settings()
if os.environ.get("DEMO_SEED", "0") == "1":
    seed_demo(store)


def pay_cfg():
    s = store.get_settings()
    cfg = dict(PAY_DEFAULTS)
    for k in PAY_DEFAULTS:
        if s.get(k) not in (None, ""):
            cfg[k] = s[k]
    return cfg


NOTIFY_DEFAULTS = {
    "notify_enabled": "0",
    "notify_email": "",
    "smtp_host": "",
    "smtp_port": "465",
    "smtp_security": "ssl",
    "smtp_user": "",
    "smtp_pass": "",
    "smtp_from": "",
}


def notify_cfg():
    s = store.get_settings()
    cfg = dict(NOTIFY_DEFAULTS)
    for k in NOTIFY_DEFAULTS:
        if s.get(k) not in (None, ""):
            cfg[k] = s[k]
    return cfg


def notify_ready(cfg):
    return bool(cfg["smtp_host"] and cfg["notify_email"]
                and cfg["smtp_user"] and cfg["smtp_pass"])


def _manual_order_email(order):
    items = order.get("items") or []
    total = fmt_cents(due_cents(order))
    lines = [f"订单号:{order['out_trade_no']}", f"买家邮箱:{order['email']}",
             f"金额:¥ {total}", f"下单时间:{order['created_at']}", "商品:"]
    for it in items:
        lines.append(f"  - {it['product_name']} x {it['qty']}")
    admin_url = base_url() + "/admin"
    lines += ["", "该订单含手动发货商品,已付款,待你发货。",
              f"请登录后台处理:{admin_url}"]
    text = "\n".join(lines)
    rows = "".join(f"<tr><td>{_esc(it['product_name'])}</td><td>{it['qty']}</td></tr>" for it in items)
    html = (f"<p>有新的手动发货订单,已付款,待你发货。</p>"
            f"<p>订单号:<b>{_esc(order['out_trade_no'])}</b><br>买家邮箱:{_esc(order['email'])}"
            f"<br>金额:¥ {total}<br>下单时间:{_esc(order['created_at'])}</p>"
            f"<table border='1' cellpadding='6' cellspacing='0'>"
            f"<tr><th>商品</th><th>数量</th></tr>{rows}</table>"
            f"<p><a href='{_esc(admin_url)}'>登录后台处理</a></p>")
    return f"【手动发货】新订单 {order['out_trade_no']} ¥ {total}", text, html


def _send_notify_in_background(cfg, order_id, subject, text, html):
    def run():
        ok, err = mailer.send_mail(cfg["smtp_host"], int(cfg["smtp_port"]), cfg["smtp_security"],
                                   cfg["smtp_user"], cfg["smtp_pass"],
                                   cfg["smtp_from"] or cfg["smtp_user"],
                                   cfg["notify_email"], subject, text, html)
        if not ok:
            store.clear_notified(order_id)
            app.logger.warning("manual order notify failed: %s", err)
    threading.Thread(target=run, daemon=True).start()


def maybe_notify_manual(order):
    """手动发货订单付款后发邮件提醒。绝不影响支付流程本身。"""
    try:
        if not order or order.get("status") != "paid_manual" or order.get("notified_at"):
            return
        cfg = notify_cfg()
        if cfg["notify_enabled"] != "1" or not notify_ready(cfg):
            return
        if not store.claim_notify(order["id"]):
            return
        if not order.get("items"):
            fresh = store.get_order(order["id"])
            if fresh:
                order = fresh
        subject, text, html = _manual_order_email(order)
        _send_notify_in_background(cfg, order["id"], subject, text, html)
    except Exception as e:
        app.logger.warning("manual order notify failed: %s", e)


def fulfill_and_notify(order_id, trade_no=""):
    order = store.fulfill_order(order_id, trade_no=trade_no)
    maybe_notify_manual(order)
    return order


def epay_types(cfg):
    out = []
    for part in (cfg.get("pay_epay_types") or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            code, label = part.split(":", 1)
        else:
            code, label = part, part
        out.append((code.strip(), label.strip()))
    return out


def epay_ready(cfg):
    return cfg["pay_epay_enabled"] == "1" and bool(
        cfg["pay_epay_url"] and cfg["pay_epay_pid"] and cfg["pay_epay_key"])


def alipay_ready(cfg):
    return cfg["pay_alipay_enabled"] == "1" and bool(
        cfg["pay_alipay_appid"] and cfg["pay_alipay_private_key"] and cfg["pay_alipay_public_key"])


def wechat_ready(cfg):
    return cfg["pay_wechat_enabled"] == "1" and bool(
        cfg["pay_wechat_qr_text"] or cfg["pay_wechat_qr_image"]) and bool(cfg["pay_wechat_secret"])


def available_pay_methods(cfg=None):
    cfg = cfg or pay_cfg()
    methods = []
    if alipay_ready(cfg):
        methods.append({"id": "alipay_f2f", "label": "支付宝(当面付)", "kind": "alipay_f2f"})
    if wechat_ready(cfg):
        methods.append({"id": "wechat_free", "label": "微信(免签)", "kind": "wechat_free"})
    if epay_ready(cfg):
        for code, label in epay_types(cfg):
            methods.append({"id": f"epay:{code}", "label": label, "kind": "epay"})
    return methods


def method_kind(method_id):
    if method_id == "alipay_f2f":
        return "alipay_f2f"
    if method_id == "wechat_free":
        return "wechat_free"
    if method_id.startswith("epay:"):
        return "epay"
    return ""


def alipay_cfg(cfg):
    return {"appid": cfg["pay_alipay_appid"], "private_key": cfg["pay_alipay_private_key"],
            "public_key": cfg["pay_alipay_public_key"], "gateway": cfg["pay_alipay_gateway"]}


def base_url():
    if SITE_URL:
        return SITE_URL
    return request.url_root.rstrip("/")


def money_to_cents(s):
    """'12.28' -> 1228。格式不对或 <=0 时抛 ValueError。"""
    try:
        d = Decimal(str(s).strip())
    except (InvalidOperation, ValueError):
        raise ValueError("金额格式错误")
    if d <= 0 or d > Decimal("100000000") or d != d.quantize(Decimal("0.01")):
        raise ValueError("金额格式错误(最多两位小数,大于 0 且不超过 1 亿元)")
    return int(d * 100)


def fmt_cents(cents):
    return f"{cents / 100:.2f}"


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _to_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

# ---------- 简易限流 ----------
_hits = {}


def allow(key, limit, window=60):
    now = time.time()
    if len(_hits) > 5000:
        for k in [k for k, v in _hits.items() if not v or now - v[-1] >= window]:
            _hits.pop(k, None)
    lst = [t for t in _hits.get(key, []) if now - t < window]
    if len(lst) >= limit:
        return False
    lst.append(now)
    _hits[key] = lst
    return True


def qr_data_uri(text):
    """把文本渲染成二维码 PNG data URI;qrcode 库缺失时返回 None。"""
    try:
        import qrcode
    except ImportError:
        return None
    try:
        buf = io.BytesIO()
        qrcode.make(text).save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def payable_cents(order):
    """订单实付金额(分)= 合计 - 优惠券抵扣,最低 0。所有支付金额核对都以它为准。"""
    return max(0, order["total_cents"] - (order.get("discount_cents") or 0))


def due_cents(order):
    """买家实际应付金额(分)= 实付 + 微信随机尾数。非微信订单尾数为 0,与实付相同。
    微信免签的金额核对(订单号核对与金额匹配)都以它为准。"""
    return payable_cents(order) + (order.get("amount_offset_cents") or 0)


def build_next_url(order, cfg):
    """订单的下一步:易支付返回网关地址,当面付/免签返回站内付款页。"""
    if payable_cents(order) <= 0:
        return base_url() + "/order/" + order["out_trade_no"]
    kind = method_kind(order["pay_type"])
    if kind == "epay":
        code = order["pay_type"].split(":", 1)[1]
        names = [it["product_name"] for it in order["items"]]
        name = names[0] + (f" 等 {len(names)} 件商品" if len(names) > 1 else "")
        return epay.build_pay_url(
            cfg["pay_epay_url"], cfg["pay_epay_pid"], cfg["pay_epay_key"], code,
            order["out_trade_no"], base_url() + "/pay/notify", base_url() + "/pay/return",
            name, fmt_cents(payable_cents(order)))
    if kind == "alipay_f2f":
        return base_url() + "/pay/alipay/" + order["out_trade_no"]
    if kind == "wechat_free":
        return base_url() + "/pay/wechat/" + order["out_trade_no"]
    return ""


def refresh_order_payment(order, cfg):
    """主动向支付方查询订单。返回 (changed, error):changed 为 True 表示刚发货。"""
    kind = method_kind(order["pay_type"])
    if kind == "epay":
        if not epay_ready(cfg):
            return False, "易支付未配置"
        data = epay.query_order(cfg["pay_epay_url"], cfg["pay_epay_pid"],
                                cfg["pay_epay_key"], order["out_trade_no"])
        if data is None:
            return False, "网关查询无响应,请稍后重试"
        if not epay.query_is_paid(data):
            return False, ""
        try:
            money_cents = money_to_cents(data.get("money", "0"))
        except ValueError:
            return False, "网关返回金额异常"
        if money_cents != payable_cents(order):
            return False, "网关金额与订单不一致,已拒绝发货"
        fulfill_and_notify(order["id"], trade_no=str(data.get("trade_no", "")))
        return True, None
    if kind == "alipay_f2f":
        if not alipay_ready(cfg):
            return False, "支付宝当面付未配置"
        try:
            node = alipay.query(alipay_cfg(cfg), order["out_trade_no"])
        except alipay.AlipayError as e:
            return False, str(e)
        if not alipay.query_is_paid(node):
            return False, ""
        try:
            money_cents = money_to_cents(node.get("total_amount", "0"))
        except ValueError:
            return False, "支付宝返回金额异常"
        if money_cents != payable_cents(order):
            return False, "支付宝金额与订单不一致,已拒绝发货"
        fulfill_and_notify(order["id"], trade_no=str(node.get("trade_no", "")))
        return True, None
    return False, ""


@app.before_request
def _force_https():
    """站点配了 https 域名时,访客走 http 就 308 跳到 https(保留路径和参数)。
    只认代理明确传来的 X-Forwarded-Proto: http——直连(无此请求头)不跳,避免
    无 TLS 的直连部署陷入重定向循环;本机回环地址也不跳,保证健康检查和本地调试。"""
    if not SITE_URL.startswith("https://"):
        return None
    proto = request.headers.get("X-Forwarded-Proto", "")
    proto = proto.split(",")[-1].strip().lower() if proto else ""
    if proto != "http":
        return None
    host = request.host.split(":")[0].lower()
    if host in ("127.0.0.1", "localhost", "::1") or host.startswith("127."):
        return None
    target_host = request.host[:-3] if request.host.endswith(":80") else request.host
    qs = request.query_string.decode("utf-8", "ignore")
    return redirect("https://" + target_host + request.path + ("?" + qs if qs else ""), code=308)


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if SITE_URL.startswith("https://"):
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.path.startswith(("/admin", "/order/", "/api/order/")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------- 模板上下文 ----------
@app.template_filter("nl2br")
def nl2br(value):
    """转义文本并把换行转成 <br>。"""
    from markupsafe import Markup, escape
    return Markup("<br>").join(escape(part) for part in str(value or "").split("\n"))


@app.context_processor
def inject_common():
    return {"settings": store.get_settings(), "fmt_cents": fmt_cents,
            "pay_methods": available_pay_methods()}


# ---------- 店铺前台 ----------
@app.route("/")
def index():
    cats = store.list_categories()
    products = store.list_products()
    by_cat = {}
    for p in products:
        by_cat.setdefault(p["category_id"], []).append(p)
    return render_template("index.html", categories=cats, by_cat=by_cat)


@app.route("/buy/<int:pid>")
def buy_page(pid):
    p = store.get_product(pid)
    if not p or not p["enabled"]:
        abort(404)
    return render_template("buy.html", product=p)


@app.route("/cart")
def cart_page():
    return render_template("cart.html")


@app.route("/order/<out_trade_no>")
def order_page(out_trade_no):
    if not allow(f"orderview:{request.remote_addr or ''}", 60):
        abort(429)
    order = store.get_order_by_no(out_trade_no)
    if not order:
        abort(404)
    cards = store.get_order_cards(order["id"]) if order["status"] in ("paid", "paid_unfulfilled", "paid_manual") else {}
    refund_coupon = (store.get_coupon_by_source_order(order["id"])
                     if order["status"] == "cancelled" else None)
    used_coupon = store.get_coupon(order["coupon_id"]) if order.get("coupon_id") else None
    return render_template("order.html", order=order, cards=cards,
                           refund_coupon=refund_coupon, used_coupon=used_coupon,
                           payable=payable_cents(order), due=due_cents(order))


@app.route("/search")
def search_page():
    return render_template("search.html")


@app.route("/help")
def help_page():
    return render_template("help.html")


@app.route("/api/health")
def health():
    return jsonify({"ok": True,
                    "pay_methods": [m["id"] for m in available_pay_methods()]})


@app.route("/api/products")
def api_products():
    return jsonify({"products": [
        {"id": p["id"], "category_id": p["category_id"], "name": p["name"],
         "price_cents": p["price_cents"], "image_url": p["image_url"],
         "stock": p["stock"], "recommend": p["recommend"], "min_qty": p["min_qty"],
         "max_qty": p["max_qty"]}
        for p in store.list_products()]})


@app.route("/api/order/create", methods=["POST"])
def api_create_order():
    ip = request.remote_addr or ""
    if not allow(f"order:{ip}", 20):
        return jsonify({"ok": False, "error": "操作太频繁,请稍后再试"}), 429
    data = request.get_json(silent=True) or {}
    coupon_code = str(data.get("coupon_code", "") or "").strip() or None
    methods = available_pay_methods()
    if not methods and not coupon_code:
        return jsonify({"ok": False, "error": "本站尚未配置任何支付方式,请联系站长"}), 503
    email = str(data.get("email", "")).strip()
    if not EMAIL_RE.match(email):
        return jsonify({"ok": False, "error": "邮箱格式不正确"}), 400
    pay_method = str(data.get("pay_type", "")).strip()
    if pay_method not in [m["id"] for m in methods]:
        # 没选支付方式时,只有带券才允许继续(券不够全额抵扣时后面会取消订单并报错)
        if not (coupon_code and pay_method in ("", "coupon")):
            return jsonify({"ok": False, "error": "请选择支付方式"}), 400
        pay_method = "coupon"
    items = data.get("items") or []
    if not isinstance(items, list) or not items or len(items) > 20:
        return jsonify({"ok": False, "error": "请选择商品"}), 400
    clean = []
    for it in items:
        try:
            clean.append({"product_id": int(it["product_id"]), "qty": int(it["qty"])})
        except (KeyError, TypeError, ValueError):
            return jsonify({"ok": False, "error": "商品参数错误"}), 400
        if clean[-1]["qty"] <= 0:
            return jsonify({"ok": False, "error": "购买数量错误"}), 400
    try:
        order = store.create_order(email, pay_method, clean, client_ip=ip,
                                   coupon_code=coupon_code)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if payable_cents(order) <= 0:
        # 优惠券全额抵扣:无需付款,直接发货(券在发货时核销)
        order = fulfill_and_notify(order["id"], trade_no="COUPON")
        return jsonify({"ok": True, "out_trade_no": order["out_trade_no"],
                        "next": base_url() + "/order/" + order["out_trade_no"],
                        "payable_cents": 0, "discount_cents": order["discount_cents"]})
    if pay_method == "coupon":
        # 券不够全额抵扣,又没选支付方式:取消刚建的订单(券锁定随之释放)
        store.cancel_order(order["id"])
        return jsonify({"ok": False, "error": "优惠券不够全额抵扣,请选择支付方式付差价"}), 400
    return jsonify({"ok": True, "out_trade_no": order["out_trade_no"],
                    "next": build_next_url(order, pay_cfg()),
                    "payable_cents": payable_cents(order),
                    "discount_cents": order["discount_cents"]})


@app.route("/api/coupons/validate", methods=["POST"])
def api_coupon_validate():
    if not allow(f"coupon:{request.remote_addr or ''}", 30):
        return jsonify({"ok": False, "error": "操作太频繁,请稍后再试"}), 429
    data = request.get_json(silent=True) or {}
    code = str(data.get("code", "") or "")
    email = str(data.get("email", "") or "").strip()
    if not code.strip() or not EMAIL_RE.match(email):
        return jsonify({"ok": False, "error": "请输入券码和下单邮箱"}), 400
    coupon, err = store.validate_coupon(code, email)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "code": coupon["code"],
                    "amount_cents": coupon["amount_cents"]})


@app.route("/api/order/<out_trade_no>/status")
def api_order_status(out_trade_no):
    if not allow(f"orderview:{request.remote_addr or ''}", 60):
        return jsonify({"ok": False, "error": "操作太频繁,请稍后再试"}), 429
    order = store.get_order_by_no(out_trade_no)
    if not order:
        return jsonify({"ok": False, "error": "订单不存在"}), 404
    out = {"ok": True, "status": order["status"], "total_cents": order["total_cents"],
           "discount_cents": order.get("discount_cents") or 0,
           "payable_cents": payable_cents(order),
           "paid_at": order["paid_at"], "note": order["note"]}
    if order["status"] in ("paid", "paid_unfulfilled", "paid_manual"):
        out["cards"] = store.get_order_cards(order["id"])
    return jsonify(out)


@app.route("/api/order/<out_trade_no>/repay", methods=["POST"])
def api_order_repay(out_trade_no):
    if not allow(f"repay:{request.remote_addr or ''}", 30):
        return jsonify({"ok": False, "error": "操作太频繁,请稍后再试"}), 429
    order = store.get_order_by_no(out_trade_no)
    if not order:
        return jsonify({"ok": False, "error": "订单不存在"}), 404
    if order["status"] != "unpaid":
        return jsonify({"ok": False, "error": "订单已不是待支付状态"}), 400
    nxt = build_next_url(order, pay_cfg())
    if not nxt:
        return jsonify({"ok": False, "error": "该支付方式已停用"}), 503
    return jsonify({"ok": True, "next": nxt})


@app.route("/api/order/<out_trade_no>/refresh", methods=["POST"])
def api_order_refresh(out_trade_no):
    """主动向支付方查单(异步通知丢失时补发货)。微信免签无网关可查,只返回当前状态。"""
    if not allow(f"refresh:{request.remote_addr or ''}", 30):
        return jsonify({"ok": False, "error": "操作太频繁,请稍后再试"}), 429
    order = store.get_order_by_no(out_trade_no)
    if not order:
        return jsonify({"ok": False, "error": "订单不存在"}), 404
    if order["status"] != "unpaid":
        return jsonify({"ok": True, "status": order["status"]})
    changed, err = refresh_order_payment(order, pay_cfg())
    if err:
        return jsonify({"ok": False, "error": err}), 502
    return jsonify({"ok": True, "status": "paid" if changed else "unpaid"})


def _mask_coupon_code(code):
    code = code or ""
    return code[:4] + "****" + code[-4:] if len(code) > 8 else "****"


def _order_summary(order):
    return {"out_trade_no": order["out_trade_no"], "email": order["email"],
            "total_cents": order["total_cents"],
            "discount_cents": order.get("discount_cents") or 0,
            "payable_cents": payable_cents(order), "pay_type": order["pay_type"],
            "status": order["status"], "created_at": order["created_at"],
            "paid_at": order["paid_at"]}


@app.route("/api/orders/search")
def api_orders_search():
    if not allow(f"search:{request.remote_addr or ''}", 30):
        return jsonify({"ok": False, "error": "操作太频繁,请稍后再试"}), 429
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"ok": False, "error": "请输入订单号或邮箱"}), 400
    order = store.get_order_by_no(q)
    if order:
        return jsonify({"ok": True, "orders": [_order_summary(order)], "coupons": []})
    if EMAIL_RE.match(q):
        # 邮箱是谁都能猜到的弱身份,完整券码只在订单页(订单号不可猜)显示;
        # 这里只给脱敏券码,防止知道邮箱的人直接盗走未用券。
        coupons = [{"code": _mask_coupon_code(c["code"]), "amount_cents": c["amount_cents"],
                    "status": c["status"], "created_at": c["created_at"], "note": c["note"]}
                   for c in store.list_coupons_by_email(q)]
        return jsonify({"ok": True,
                        "orders": [_order_summary(o) for o in store.search_orders_by_email(q)],
                        "coupons": coupons})
    return jsonify({"ok": True, "orders": [], "coupons": []})


# ---------- 付款页(支付宝跳转 / 微信免签) ----------
@app.route("/pay/alipay/<out_trade_no>")
def pay_alipay_page(out_trade_no):
    if not allow(f"paypage:{request.remote_addr or ''}", 30):
        abort(429)
    order = store.get_order_by_no(out_trade_no)
    if not order or order["pay_type"] != "alipay_f2f":
        abort(404)
    if order["status"] != "unpaid":
        return redirect(f"/order/{out_trade_no}")
    cfg = pay_cfg()
    error = None
    qr_img = qr_link = None
    if not alipay_ready(cfg):
        error = "支付宝当面付尚未配置,请联系站长"
    else:
        names = [it["product_name"] for it in order["items"]]
        subject = names[0] + (f" 等 {len(names)} 件商品" if len(names) > 1 else "")
        try:
            qr_link = alipay.precreate(
                alipay_cfg(cfg), order["out_trade_no"],
                Decimal(payable_cents(order)) / 100, subject,
                base_url() + "/pay/alipay/notify")
            qr_img = qr_data_uri(qr_link)
        except alipay.AlipayError as e:
            error = f"生成支付二维码失败:{e}"
    in_wechat = "MicroMessenger" in request.headers.get("User-Agent", "")
    return render_template("pay_qr.html", order=order, channel="alipay",
                           qr_img=qr_img, qr_link=qr_link, error=error,
                           in_wechat=in_wechat, payable=payable_cents(order))


@app.route("/pay/wechat/<out_trade_no>")
def pay_wechat_page(out_trade_no):
    if not allow(f"paypage:{request.remote_addr or ''}", 30):
        abort(429)
    order = store.get_order_by_no(out_trade_no)
    if not order or order["pay_type"] != "wechat_free":
        abort(404)
    if order["status"] != "unpaid":
        return redirect(f"/order/{out_trade_no}")
    cfg = pay_cfg()
    error = None
    qr_img = qr_link = None
    if cfg["pay_wechat_qr_image"]:
        qr_img = cfg["pay_wechat_qr_image"]
    elif cfg["pay_wechat_qr_text"]:
        qr_link = cfg["pay_wechat_qr_text"]
        qr_img = qr_data_uri(qr_link)
    else:
        error = "店主尚未配置微信收款码,请联系站长"
    return render_template("pay_qr.html", order=order, channel="wechat",
                           qr_img=qr_img, qr_link=qr_link, error=error,
                           in_wechat=False, payable=due_cents(order))


# ---------- 支付回调 ----------
@app.route("/pay/notify", methods=["GET", "POST"])
def pay_notify():
    cfg = pay_cfg()
    params = {k: v for k, v in request.values.items()}
    if not epay_ready(cfg):
        return "fail", 503
    if params.get("pid") != cfg["pay_epay_pid"] or not epay.verify_params(params, cfg["pay_epay_key"]):
        return "fail", 400
    if params.get("trade_status") != "TRADE_SUCCESS":
        return "success"
    order = store.get_order_by_no(params.get("out_trade_no", ""))
    if not order or not order["pay_type"].startswith("epay:"):
        return "fail", 404
    try:
        money_cents = money_to_cents(params.get("money", "0"))
    except ValueError:
        return "fail", 400
    if money_cents != payable_cents(order):
        return "fail", 400
    fulfill_and_notify(order["id"], trade_no=params.get("trade_no", ""))
    return "success"


@app.route("/pay/alipay/notify", methods=["GET", "POST"])
def pay_alipay_notify():
    cfg = pay_cfg()
    params = {k: v for k, v in request.values.items()}
    if not alipay_ready(cfg):
        return "fail", 503
    if not alipay.verify_notify(params, cfg["pay_alipay_public_key"], cfg["pay_alipay_appid"]):
        return "fail", 400
    if params.get("trade_status") not in ("TRADE_SUCCESS", "TRADE_FINISHED"):
        return "success"
    order = store.get_order_by_no(params.get("out_trade_no", ""))
    if not order or order["pay_type"] != "alipay_f2f":
        return "fail", 404
    try:
        money_cents = money_to_cents(params.get("total_amount", "0"))
    except ValueError:
        return "fail", 400
    if money_cents != payable_cents(order):
        return "fail", 400
    fulfill_and_notify(order["id"], trade_no=params.get("trade_no", ""))
    return "success"


def _parse_callback_body():
    data = request.get_json(silent=True)
    if isinstance(data, dict) and data:
        return {str(k): v for k, v in data.items()}
    raw = request.get_data(cache=True) or b""
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                return {str(k): v for k, v in parsed.items()}
        except ValueError:
            pass
    if request.values:
        return {k: v for k, v in request.values.items()}
    return {}


@app.route("/pay/wechat-free/notify", methods=["POST"])
def pay_wechat_notify():
    """监听软件回调:JSON {amount, paid_at?, payer?, txid?, out_trade_no?, sign}。
    sign 为 HMAC-SHA256,密钥在后台「支付」里。匹配不到时返回 ok:false 供排查,
    不重试也不会误发货。"""
    cfg = pay_cfg()
    if not wechat_ready(cfg):
        return jsonify({"ok": False, "error": "微信免签未配置"}), 503
    params = _parse_callback_body()
    if not wfree.verify_payload(params, cfg["pay_wechat_secret"]):
        return jsonify({"ok": False, "error": "签名校验失败"}), 401
    try:
        amount_cents = money_to_cents(params.get("amount", "0"))
    except ValueError:
        return jsonify({"ok": False, "error": "金额格式错误"}), 400
    key = wfree.dedup_key(params)
    payload = wfree.canonical_json(params)
    matched = store.callback_matched(key)
    if matched:
        return jsonify({"ok": True, "duplicate": True, "matched": matched})
    out_no = str(params.get("out_trade_no", "") or "").strip()
    if out_no:
        order = store.get_order_by_no(out_no)
        if not order or order["pay_type"] != "wechat_free":
            store.log_callback("wechat_free", key, payload, "no_order")
            return jsonify({"ok": False, "error": "订单不存在或不是微信免签订单"})
        if due_cents(order) != amount_cents:
            store.log_callback("wechat_free", key, payload, "amount_mismatch", out_no)
            return jsonify({"ok": False, "error": "金额与订单不一致"})
        fulfill_and_notify(order["id"], trade_no=str(params.get("txid", "")))
        store.log_callback("wechat_free", key, payload, "matched", out_no)
        return jsonify({"ok": True, "matched": out_no})
    try:
        window = int(cfg.get("pay_wechat_window") or 30)
    except ValueError:
        window = 30
    cands = store.find_unpaid_by_amount("wechat_free", amount_cents, window)
    if len(cands) == 1:
        order = cands[0]
        fulfill_and_notify(order["id"], trade_no=str(params.get("txid", "")))
        store.log_callback("wechat_free", key, payload, "matched", order["out_trade_no"])
        return jsonify({"ok": True, "matched": order["out_trade_no"]})
    if not cands:
        store.log_callback("wechat_free", key, payload, "unmatched")
        return jsonify({"ok": False, "error": "无金额匹配的待支付订单"})
    store.log_callback("wechat_free", key, payload, "ambiguous")
    return jsonify({"ok": False, "error": "多笔订单金额相同,需人工确认发货"})


@app.route("/pay/forward/notify", methods=["GET", "POST"])
def pay_forward_notify():
    """通知转发回调(短信转发器等):把安卓通知栏的微信收款提醒转发过来,
    本店从文本里解析金额并按金额匹配发货。鉴权:key(与回调密钥相同)可放在
    JSON/表单字段、URL 参数或 X-Callback-Key 请求头里。接受 JSON、表单、
    URL 参数与原始文本多种形态,字段名兼容 title/msg/content/package 等。"""
    cfg = pay_cfg()
    if not wechat_ready(cfg):
        return jsonify({"ok": False, "error": "微信免签未配置"}), 503
    merged = {k: v for k, v in request.values.items()}
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        for k, v in data.items():
            merged[str(k)] = v
    key = merged.get("key") or request.headers.get("X-Callback-Key", "")
    if not key or not hmac.compare_digest(str(key), cfg["pay_wechat_secret"]):
        return jsonify({"ok": False, "error": "鉴权失败"}), 401

    def pick(*names):
        for n in names:
            v = merged.get(n)
            if v is not None and str(v) != "":
                return str(v)
        return ""
    package = pick("package", "package_name", "PACKAGE_NAME", "pkg")
    title = pick("title", "TITLE")
    msg = pick("msg", "MSG", "content", "text", "message", "body")
    when = pick("time", "receive_time", "RECEIVE_TIME", "timestamp", "post_time")
    if not msg:
        raw = (request.get_data(cache=True) or b"").decode("utf-8", errors="ignore").strip()
        if raw and not raw.startswith("{"):
            msg = raw
    if package and package != "com.tencent.mm":
        return jsonify({"ok": False, "error": "ignored: not wechat"})
    text = f"{title} {msg}".strip()
    if not wfree.is_payment_text(text):
        return jsonify({"ok": False, "error": "ignored: not a payment notification"})
    cents = wfree.extract_amount_cents(text)
    if cents is None:
        return jsonify({"ok": False, "error": "无法从通知中解析金额"})
    import hashlib as _hashlib
    dedup = "fwd:" + _hashlib.sha256(
        "|".join([package, title, msg, when]).encode("utf-8")).hexdigest()[:32]
    matched = store.callback_matched(dedup)
    if matched:
        return jsonify({"ok": True, "duplicate": True, "matched": matched})
    # 日志里绝不存鉴权密钥
    logged = {k: v for k, v in merged.items() if str(k).lower() != "key"}
    log_payload = wfree.canonical_json(logged)
    try:
        window = int(cfg.get("pay_wechat_window") or 30)
    except ValueError:
        window = 30
    cands = store.find_unpaid_by_amount("wechat_free", cents, window)
    if len(cands) == 1:
        order = cands[0]
        fulfill_and_notify(order["id"])
        store.log_callback("wechat_forward", dedup, log_payload, "matched", order["out_trade_no"])
        return jsonify({"ok": True, "matched": order["out_trade_no"]})
    if not cands:
        store.log_callback("wechat_forward", dedup, log_payload, "unmatched")
        return jsonify({"ok": False, "error": "无金额匹配的待支付订单"})
    store.log_callback("wechat_forward", dedup, log_payload, "ambiguous")
    return jsonify({"ok": False, "error": "多笔订单金额相同,需人工确认发货"})


@app.route("/pay/return")
def pay_return():
    cfg = pay_cfg()
    params = {k: v for k, v in request.values.items()}
    verified = bool(epay_ready(cfg) and params.get("pid") == cfg["pay_epay_pid"]
                    and epay.verify_params(params, cfg["pay_epay_key"]))
    if not verified and alipay_ready(cfg) and params.get("app_id"):
        # 支付宝付款后回跳:同步返回参数同样带签名,验签 + app_id 核对
        verified = alipay.verify_notify(params, cfg["pay_alipay_public_key"],
                                        cfg["pay_alipay_appid"])
    order = store.get_order_by_no(params.get("out_trade_no", ""))
    if not order:
        abort(404)
    return render_template("pay_result.html", order=order, verified=verified,
                           trade_status=params.get("trade_status", ""))


# ---------- 后台 ----------
def require_admin():
    if not session.get("admin"):
        abort(401)


def check_csrf():
    token = request.headers.get("X-CSRF-Token", "")
    if not token or not hmac.compare_digest(token, session.get("csrf", "")):
        abort(403)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method != "POST":
        if session.get("admin"):
            return redirect("/admin")
        return render_template("admin_login.html")
    ip = request.remote_addr or ""
    if not allow(f"login:{ip}", 10):
        return jsonify({"ok": False, "error": "尝试太频繁,请稍后再试"}), 429
    data = request.get_json(silent=True) or {}
    pw = str(data.get("password", ""))
    if hmac.compare_digest(pw, ADMIN_PASSWORD):
        session.permanent = True
        session["admin"] = True
        session["csrf"] = secrets.token_hex(16)
        return jsonify({"ok": True, "csrf": session["csrf"]})
    return jsonify({"ok": False, "error": "密码错误"}), 401


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/admin")
def admin_page():
    if not session.get("admin"):
        return redirect("/admin/login")
    cfg = pay_cfg()
    summary = {"epay": epay_ready(cfg), "alipay": alipay_ready(cfg),
               "wechat": wechat_ready(cfg), "epay_url": cfg["pay_epay_url"],
               "epay_pid": cfg["pay_epay_pid"], "alipay_appid": cfg["pay_alipay_appid"]}
    return render_template("admin.html", csrf=session.get("csrf", ""), pay_summary=summary)


@app.route("/admin/api/stats")
def admin_stats():
    require_admin()
    return jsonify({"ok": True, "stats": store.stats()})


@app.route("/admin/api/settings", methods=["GET", "POST"])
def admin_settings():
    require_admin()
    if request.method == "GET":
        return jsonify({"ok": True, "settings": store.get_settings()})
    check_csrf()
    data = request.get_json(silent=True) or {}
    for k in ("site_name", "announcement", "contact_text", "contact_url", "help_text"):
        if k in data:
            val = str(data[k])
            if k == "contact_url":
                val = val.strip()
                if val and not val.startswith(("http://", "https://", "mailto:")):
                    return jsonify({"ok": False, "error": "客服链接需以 http://、https:// 或 mailto: 开头"}), 400
            store.set_setting(k, val)
    return jsonify({"ok": True})


def _masked(value):
    value = value or ""
    if not value:
        return {"configured": False, "hint": ""}
    body = "".join(ln.strip() for ln in value.splitlines() if not ln.strip().startswith("-----"))
    tail = (body or value)[-4:]
    return {"configured": True, "hint": "…" + tail}


@app.route("/admin/api/pay-settings", methods=["GET", "POST"])
def admin_pay_settings():
    require_admin()
    if request.method == "GET":
        cfg = pay_cfg()
        return jsonify({"ok": True, "pay": {
            "epay_enabled": cfg["pay_epay_enabled"] == "1",
            "epay_url": cfg["pay_epay_url"],
            "epay_pid": cfg["pay_epay_pid"],
            "epay_key": _masked(cfg["pay_epay_key"]),
            "epay_types": cfg["pay_epay_types"],
            "alipay_enabled": cfg["pay_alipay_enabled"] == "1",
            "alipay_appid": cfg["pay_alipay_appid"],
            "alipay_gateway": cfg["pay_alipay_gateway"],
            "alipay_private_key": _masked(cfg["pay_alipay_private_key"]),
            "alipay_public_key": _masked(cfg["pay_alipay_public_key"]),
            "wechat_enabled": cfg["pay_wechat_enabled"] == "1",
            "wechat_qr_text": cfg["pay_wechat_qr_text"],
            "wechat_qr_image": cfg["pay_wechat_qr_image"],
            "wechat_secret": cfg["pay_wechat_secret"],
            "wechat_window": cfg["pay_wechat_window"],
        }, "callbacks": {
            "epay_notify": base_url() + "/pay/notify",
            "alipay_notify": base_url() + "/pay/alipay/notify",
            "wechat_notify": base_url() + "/pay/wechat-free/notify",
            "forward_notify": base_url() + "/pay/forward/notify",
        }})
    check_csrf()
    data = request.get_json(silent=True) or {}
    new_private = str(data.get("pay_alipay_private_key", "") or "").strip()
    new_public = str(data.get("pay_alipay_public_key", "") or "").strip()
    if new_private:
        try:
            alipay.load_private_key(new_private)
        except ValueError as e:
            return jsonify({"ok": False, "error": f"应用私钥无效:{e}"}), 400
    if new_public:
        try:
            alipay.load_public_key(new_public)
        except ValueError as e:
            return jsonify({"ok": False, "error": f"支付宝公钥无效:{e}"}), 400
    for key in ("pay_epay_url", "pay_epay_pid", "pay_epay_types", "pay_alipay_appid",
                "pay_alipay_gateway", "pay_wechat_qr_text", "pay_wechat_qr_image",
                "pay_wechat_secret", "pay_wechat_window"):
        if key in data:
            store.set_setting(key, str(data[key]).strip())
    for key in ("pay_epay_enabled", "pay_alipay_enabled", "pay_wechat_enabled"):
        if key in data:
            store.set_setting(key, "1" if data[key] in (True, 1, "1", "true", "on") else "0")
    new_epay_key = str(data.get("pay_epay_key", "") or "").strip()
    if new_epay_key:
        store.set_setting("pay_epay_key", new_epay_key)
    if new_private:
        store.set_setting("pay_alipay_private_key", new_private)
    if new_public:
        store.set_setting("pay_alipay_public_key", new_public)
    cfg = pay_cfg()
    if cfg["pay_wechat_enabled"] == "1" and not cfg["pay_wechat_secret"]:
        store.set_setting("pay_wechat_secret", secrets.token_hex(16))
        cfg = pay_cfg()
    errs = []
    if cfg["pay_epay_enabled"] == "1" and not epay_ready(cfg):
        errs.append("易支付已启用但网关地址/商户号/密钥未填全")
    if cfg["pay_alipay_enabled"] == "1" and not alipay_ready(cfg):
        errs.append("支付宝当面付已启用但 AppID/应用私钥/支付宝公钥未填全")
    if cfg["pay_wechat_enabled"] == "1" and not wechat_ready(cfg):
        errs.append("微信免签已启用但收款码未填")
    if errs:
        return jsonify({"ok": False, "error": "; ".join(errs)}), 400
    return jsonify({"ok": True})


@app.route("/admin/api/notify-settings", methods=["GET", "POST"])
def admin_notify_settings():
    require_admin()
    if request.method == "GET":
        cfg = notify_cfg()
        return jsonify({"ok": True, "notify": {
            "enabled": cfg["notify_enabled"] == "1",
            "email": cfg["notify_email"],
            "host": cfg["smtp_host"],
            "port": cfg["smtp_port"],
            "security": cfg["smtp_security"],
            "user": cfg["smtp_user"],
            "pass": _masked(cfg["smtp_pass"]),
            "from": cfg["smtp_from"],
        }})
    check_csrf()
    data = request.get_json(silent=True) or {}
    enabled = "1" if data.get("enabled") in (True, 1, "1", "true", "on") else "0"
    email = str(data.get("email", "") or "").strip()
    host = str(data.get("host", "") or "").strip()
    try:
        port = int(str(data.get("port", "465") or "465"))
    except ValueError:
        return jsonify({"ok": False, "error": "端口必须是数字"}), 400
    if not 1 <= port <= 65535:
        return jsonify({"ok": False, "error": "端口范围 1~65535"}), 400
    security = str(data.get("security", "ssl") or "ssl").strip()
    if security not in ("ssl", "starttls", "none"):
        return jsonify({"ok": False, "error": "加密方式只能是 ssl/starttls/none"}), 400
    user = str(data.get("user", "") or "").strip()
    sender = str(data.get("from", "") or "").strip()
    if enabled == "1":
        import re as _re
        if not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            return jsonify({"ok": False, "error": "收件邮箱格式不正确"}), 400
        if not host or not user:
            return jsonify({"ok": False, "error": "启用前需填好 SMTP 服务器和账号"}), 400
    store.set_setting("notify_enabled", enabled)
    store.set_setting("notify_email", email)
    store.set_setting("smtp_host", host)
    store.set_setting("smtp_port", str(port))
    store.set_setting("smtp_security", security)
    store.set_setting("smtp_user", user)
    store.set_setting("smtp_from", sender)
    new_pass = str(data.get("pass", "") or "").strip()
    if new_pass:
        store.set_setting("smtp_pass", new_pass)
    if enabled == "1" and not notify_ready(notify_cfg()):
        return jsonify({"ok": False, "error": "启用前需填好 SMTP 密码/授权码"}), 400
    return jsonify({"ok": True})


@app.route("/admin/api/notify-settings/test", methods=["POST"])
def admin_notify_test():
    require_admin()
    check_csrf()
    cfg = notify_cfg()
    if not notify_ready(cfg):
        return jsonify({"ok": False, "error": "请先填好收件邮箱和 SMTP 配置并保存"}), 400
    ok, err = mailer.send_mail(cfg["smtp_host"], int(cfg["smtp_port"]), cfg["smtp_security"],
                               cfg["smtp_user"], cfg["smtp_pass"],
                               cfg["smtp_from"] or cfg["smtp_user"], cfg["notify_email"],
                               "【发卡网】邮件通知测试", "这是一封测试邮件,说明邮件通知配置正确。")
    if not ok:
        return jsonify({"ok": False, "error": f"发送失败:{err}"}), 400
    return jsonify({"ok": True})


@app.route("/admin/api/categories", methods=["GET", "POST"])
def admin_categories():
    require_admin()
    if request.method == "GET":
        return jsonify({"ok": True, "categories": store.list_categories()})
    check_csrf()
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"ok": False, "error": "分类名不能为空"}), 400
    cid = store.add_category(name, _to_int(data.get("sort"), 0))
    return jsonify({"ok": True, "id": cid})


@app.route("/admin/api/categories/<int:cid>", methods=["PUT", "DELETE"])
def admin_category(cid):
    require_admin()
    check_csrf()
    if request.method == "DELETE":
        try:
            store.delete_category(cid)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    store.update_category(cid, str(data.get("name", "")).strip(), _to_int(data.get("sort"), 0))
    return jsonify({"ok": True})


def _product_fields(data, partial=False):
    fields = {}
    if "category_id" in data or not partial:
        fields["category_id"] = int(data.get("category_id", 0))
    if "name" in data or not partial:
        fields["name"] = str(data.get("name", "")).strip()
    if "price" in data or not partial:
        fields["price_cents"] = money_to_cents(data.get("price", "0"))
    for k in ("image_url", "description"):
        if k in data or not partial:
            fields[k] = str(data.get(k, ""))
    defaults = {"sort": 0, "recommend": 0, "enabled": 1, "min_qty": 1, "max_qty": 100}
    for k in ("sort", "recommend", "enabled", "min_qty", "max_qty"):
        if k in data or not partial:
            fields[k] = int(data.get(k, defaults[k]))
    if "fulfill_mode" in data or not partial:
        mode = str(data.get("fulfill_mode", "auto")).strip()
        if mode not in ("auto", "manual"):
            raise ValueError("发货方式只能是 auto 或 manual")
        fields["fulfill_mode"] = mode
    if "name" in fields and not fields["name"]:
        raise ValueError("商品名不能为空")
    if "category_id" in fields and fields["category_id"] <= 0:
        raise ValueError("请选择分类")
    if "min_qty" in fields and "max_qty" in fields:
        if fields["min_qty"] < 1 or fields["max_qty"] < fields["min_qty"]:
            raise ValueError("数量范围不合法")
    return fields


@app.route("/admin/api/products", methods=["GET", "POST"])
def admin_products():
    require_admin()
    if request.method == "GET":
        return jsonify({"ok": True, "products": store.list_products(include_disabled=True)})
    check_csrf()
    data = request.get_json(silent=True) or {}
    try:
        fields = _product_fields(data)
    except (ValueError, TypeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    pid = store.add_product(**fields)
    return jsonify({"ok": True, "id": pid})


@app.route("/admin/api/products/<int:pid>", methods=["PUT", "DELETE"])
def admin_product(pid):
    require_admin()
    check_csrf()
    if request.method == "DELETE":
        try:
            store.delete_product(pid)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    try:
        fields = _product_fields(data, partial=True)
    except (ValueError, TypeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    store.update_product(pid, **fields)
    return jsonify({"ok": True})


@app.route("/admin/api/products/<int:pid>/cards", methods=["GET", "POST"])
def admin_cards(pid):
    require_admin()
    if request.method == "GET":
        status = request.args.get("status") or None
        page = max(1, _to_int(request.args.get("page"), 1))
        rows, total = store.list_cards(pid, status=status, limit=50, offset=(page - 1) * 50)
        return jsonify({"ok": True, "cards": rows, "total": total})
    check_csrf()
    data = request.get_json(silent=True) or {}
    lines = str(data.get("lines", "")).splitlines()[:20000]
    n = store.add_cards(pid, lines)
    return jsonify({"ok": True, "added": n})


@app.route("/admin/api/cards/delete", methods=["POST"])
def admin_cards_delete():
    require_admin()
    check_csrf()
    data = request.get_json(silent=True) or {}
    ids = [_to_int(i, 0) for i in (data.get("ids") or [])]
    ids = [i for i in ids if i > 0]
    n = store.delete_cards(ids)
    return jsonify({"ok": True, "deleted": n})


@app.route("/admin/api/orders")
def admin_orders():
    require_admin()
    status = request.args.get("status") or None
    q = request.args.get("q") or None
    page = max(1, _to_int(request.args.get("page"), 1))
    rows, total = store.list_orders(status=status, q=q, limit=30, offset=(page - 1) * 30)
    return jsonify({"ok": True, "orders": rows, "total": total})


@app.route("/admin/api/orders/<int:oid>")
def admin_order_detail(oid):
    require_admin()
    with store._conn() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "订单不存在"}), 404
        order = dict(row)
        order["items"] = [dict(r) for r in conn.execute(
            "SELECT oi.*, COALESCE(p.fulfill_mode, 'auto') AS fulfill_mode,"
            " (SELECT COUNT(*) FROM cards c WHERE c.order_id=oi.order_id"
            " AND c.product_id=oi.product_id) AS allocated"
            " FROM order_items oi LEFT JOIN products p ON p.id=oi.product_id"
            " WHERE oi.order_id=? ORDER BY oi.id", (oid,)).fetchall()]
        for it in order["items"]:
            it["pending"] = it["qty"] - it["allocated"]
    order["cards"] = store.get_order_cards(oid)
    order["used_coupon"] = store.get_coupon(order["coupon_id"]) if order.get("coupon_id") else None
    order["refund_coupon"] = store.get_coupon_by_source_order(oid)
    order["payable_cents"] = payable_cents(order)
    order["due_cents"] = due_cents(order)
    return jsonify({"ok": True, "order": order})


@app.route("/admin/api/orders/<int:oid>/fulfill", methods=["POST"])
def admin_order_fulfill(oid):
    require_admin()
    check_csrf()
    data = request.get_json(silent=True) or {}
    raw = data.get("manual_cards") or {}
    manual_contents = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                pid = int(k)
            except (TypeError, ValueError):
                continue
            if isinstance(v, str):
                v = v.split("\n")
            if not isinstance(v, list):
                continue
            lines = [str(x).strip() for x in v]
            lines = [x for x in lines if x][:200]
            if lines:
                manual_contents[pid] = lines
    try:
        store.fulfill_order(oid, include_manual=True, manual_contents=manual_contents)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/admin/api/orders/<int:oid>/cancel", methods=["POST"])
def admin_order_cancel(oid):
    require_admin()
    check_csrf()
    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason", "") or "").strip()[:200]
    try:
        result = store.cancel_order(oid, reason=reason)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    out = {"ok": True, "prev_status": result["prev_status"]}
    if result["coupon"]:
        out["coupon"] = {"code": result["coupon"]["code"],
                         "amount_cents": result["coupon"]["amount_cents"]}
    return jsonify(out)


@app.route("/admin/api/orders/<int:oid>", methods=["DELETE"])
def admin_order_delete(oid):
    require_admin()
    check_csrf()
    try:
        store.delete_order(oid)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/admin/api/orders/<int:oid>/refresh", methods=["POST"])
def admin_order_refresh(oid):
    require_admin()
    check_csrf()
    with store._conn() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "订单不存在"}), 404
    order = dict(row)
    if order["status"] != "unpaid":
        return jsonify({"ok": True, "status": order["status"]})
    changed, err = refresh_order_payment(order, pay_cfg())
    if err:
        return jsonify({"ok": False, "error": err}), 502
    return jsonify({"ok": True, "status": "paid" if changed else "unpaid"})


@app.route("/admin/api/coupons")
def admin_coupons():
    require_admin()
    status = request.args.get("status") or None
    q = request.args.get("q") or None
    page = max(1, _to_int(request.args.get("page"), 1))
    rows, total = store.list_coupons(status=status, q=q, limit=30, offset=(page - 1) * 30)
    return jsonify({"ok": True, "coupons": rows, "total": total})


@app.route("/admin/api/coupons", methods=["POST"])
def admin_coupon_issue():
    require_admin()
    check_csrf()
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip()
    if not EMAIL_RE.match(email):
        return jsonify({"ok": False, "error": "邮箱格式不正确"}), 400
    try:
        amount = money_to_cents(str(data.get("amount", "")))
    except ValueError:
        return jsonify({"ok": False, "error": "金额格式不正确"}), 400
    if amount <= 0 or amount > 100000000:
        return jsonify({"ok": False, "error": "金额需在 0.01~1000000 元之间"}), 400
    note = str(data.get("note", "") or "")[:100]
    try:
        coupon = store.create_coupon(email, amount, note=note)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "coupon": coupon})


@app.route("/admin/api/coupons/<int:cid>/void", methods=["POST"])
def admin_coupon_void(cid):
    require_admin()
    check_csrf()
    try:
        store.void_coupon(cid)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


if __name__ == "__main__":
    # 默认只监听本机:公网流量必须经反代(Caddy/nginx),否则攻击者可伪造
    # X-Forwarded-For 绕过按 IP 限流。确需直连时用 HOST=0.0.0.0 显式指定。
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8000")))
