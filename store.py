# -*- coding: utf-8 -*-
"""SQLite 数据层:分类、商品、卡密、订单、优惠券、设置。

金额一律用整数分存储,避免浮点误差。写操作统一加全局锁,
并在事务内复查库存,防止超卖。"""
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta

WRITE_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sort INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    name TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    image_url TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    sort INTEGER NOT NULL DEFAULT 0,
    recommend INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    min_qty INTEGER NOT NULL DEFAULT 1,
    max_qty INTEGER NOT NULL DEFAULT 100,
    fulfill_mode TEXT NOT NULL DEFAULT 'auto',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'stock',
    status TEXT NOT NULL DEFAULT 'unused',
    order_id INTEGER,
    created_at TEXT NOT NULL,
    used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cards_product_status ON cards(product_id, status);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    out_trade_no TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL,
    total_cents INTEGER NOT NULL,
    pay_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unpaid',
    trade_no TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    client_ip TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    paid_at TEXT,
    notified_at TEXT
);
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL,
    product_name TEXT NOT NULL,
    qty INTEGER NOT NULL,
    unit_price_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS coupons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'unused',
    source_order_id INTEGER,
    locked_order_id INTEGER,
    used_order_id INTEGER,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_coupons_email ON coupons(email);
CREATE TABLE IF NOT EXISTS payment_callbacks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    matched_order TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "site_name": "我的发卡网",
    "announcement": "",
    "contact_text": "客服",
    "contact_url": "",
    "help_text": "",
}


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            cols = [r[1] for r in conn.execute("PRAGMA table_info(products)").fetchall()]
            if "fulfill_mode" not in cols:
                conn.execute("ALTER TABLE products ADD COLUMN fulfill_mode TEXT NOT NULL DEFAULT 'auto'")
            ocols = [r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
            if "notified_at" not in ocols:
                conn.execute("ALTER TABLE orders ADD COLUMN notified_at TEXT")
            if "coupon_id" not in ocols:
                conn.execute("ALTER TABLE orders ADD COLUMN coupon_id INTEGER")
            if "discount_cents" not in ocols:
                conn.execute("ALTER TABLE orders ADD COLUMN discount_cents INTEGER NOT NULL DEFAULT 0")
            ccols = [r[1] for r in conn.execute("PRAGMA table_info(cards)").fetchall()]
            if "source" not in ccols:
                conn.execute("ALTER TABLE cards ADD COLUMN source TEXT NOT NULL DEFAULT 'stock'")
            conn.execute("PRAGMA journal_mode=WAL")
            for k, v in DEFAULT_SETTINGS.items():
                conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def get_settings(self):
        with self._conn() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        out = dict(DEFAULT_SETTINGS)
        out.update({r["key"]: r["value"] for r in rows})
        return out

    def set_setting(self, key, value):
        with WRITE_LOCK, self._conn() as conn:
            conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, value))

    def list_categories(self):
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM categories ORDER BY sort ASC, id ASC").fetchall()]

    def add_category(self, name, sort=0):
        with WRITE_LOCK, self._conn() as conn:
            cur = conn.execute("INSERT INTO categories(name, sort, created_at) VALUES (?, ?, ?)",
                               (name, sort, now_str()))
            return cur.lastrowid

    def update_category(self, cid, name, sort):
        with WRITE_LOCK, self._conn() as conn:
            conn.execute("UPDATE categories SET name=?, sort=? WHERE id=?", (name, sort, cid))

    def delete_category(self, cid):
        with WRITE_LOCK, self._conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM products WHERE category_id=?", (cid,)).fetchone()[0]
            if n:
                raise ValueError("该分类下还有商品,先删除或移动商品")
            conn.execute("DELETE FROM categories WHERE id=?", (cid,))

    def _stock_sql(self):
        return ("(SELECT COUNT(*) FROM cards c WHERE c.product_id = p.id "
                "AND c.status='unused') AS stock")

    def list_products(self, include_disabled=False):
        with self._conn() as conn:
            q = (f"SELECT p.*, {self._stock_sql()} FROM products p "
                 + ("" if include_disabled else "WHERE p.enabled=1 ")
                 + "ORDER BY p.sort ASC, p.id ASC")
            return [dict(r) for r in conn.execute(q).fetchall()]

    def get_product(self, pid):
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT p.*, {self._stock_sql()} FROM products p WHERE p.id=?", (pid,)).fetchone()
            return dict(row) if row else None

    def add_product(self, category_id, name, price_cents, image_url="", description="",
                    sort=0, recommend=0, enabled=1, min_qty=1, max_qty=100, fulfill_mode="auto"):
        if fulfill_mode not in ("auto", "manual"):
            raise ValueError("发货方式只能是 auto 或 manual")
        with WRITE_LOCK, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO products(category_id, name, price_cents, image_url, description,"
                " sort, recommend, enabled, min_qty, max_qty, fulfill_mode, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (category_id, name, price_cents, image_url, description,
                 sort, recommend, enabled, min_qty, max_qty, fulfill_mode, now_str()))
            return cur.lastrowid

    def update_product(self, pid, **fields):
        if "fulfill_mode" in fields and fields["fulfill_mode"] not in ("auto", "manual"):
            raise ValueError("发货方式只能是 auto 或 manual")
        allowed = {"category_id", "name", "price_cents", "image_url", "description",
                   "sort", "recommend", "enabled", "min_qty", "max_qty", "fulfill_mode"}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        vals.append(pid)
        with WRITE_LOCK, self._conn() as conn:
            conn.execute(f"UPDATE products SET {', '.join(sets)} WHERE id=?", vals)

    def delete_product(self, pid):
        with WRITE_LOCK, self._conn() as conn:
            used = conn.execute(
                "SELECT COUNT(*) FROM cards WHERE product_id=? AND status='used'",
                (pid,)).fetchone()[0]
            ordered = conn.execute(
                "SELECT COUNT(*) FROM order_items WHERE product_id=?", (pid,)).fetchone()[0]
            if used or ordered:
                raise ValueError("该商品已有售出记录,不能删除(可下架)")
            conn.execute("DELETE FROM cards WHERE product_id=?", (pid,))
            conn.execute("DELETE FROM products WHERE id=?", (pid,))

    def add_cards(self, product_id, lines):
        lines = [ln.strip() for ln in lines if ln.strip()]
        if not lines:
            return 0
        with WRITE_LOCK, self._conn() as conn:
            conn.executemany(
                "INSERT INTO cards(product_id, content, created_at) VALUES (?, ?, ?)",
                [(product_id, ln, now_str()) for ln in lines])
            return len(lines)

    def list_cards(self, product_id, status=None, limit=200, offset=0):
        with self._conn() as conn:
            q = "SELECT * FROM cards WHERE product_id=?"
            args = [product_id]
            if status:
                q += " AND status=?"
                args.append(status)
            q += " ORDER BY id DESC LIMIT ? OFFSET ?"
            args += [limit, offset]
            rows = conn.execute(q, args).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM cards WHERE product_id=?" +
                (" AND status=?" if status else ""),
                [product_id] + ([status] if status else [])).fetchone()[0]
            return [dict(r) for r in rows], total

    def delete_cards(self, card_ids):
        if not card_ids:
            return 0
        with WRITE_LOCK, self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM cards WHERE id IN (%s) AND status='unused'"
                % ",".join("?" * len(card_ids)), card_ids)
            return cur.rowcount

    def stock_of(self, product_id):
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM cards WHERE product_id=? AND status='unused'",
                (product_id,)).fetchone()[0]

    def generate_out_trade_no(self):
        # 订单号即取卡凭证:后缀必须不可猜(64 位随机),否则可被枚举盗卡。
        base = time.strftime("%Y%m%d%H%M%S")
        return f"FK{base}{secrets.token_hex(8)}"

    # ---------- 优惠券 ----------
    def generate_coupon_code(self):
        # 12 位无歧义字符(去掉 0/O/1/I/L),约 2^60,猜不到
        alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
        return "YHQ-" + "".join(secrets.choice(alphabet) for _ in range(12))

    @staticmethod
    def normalize_coupon_code(code):
        return (code or "").strip().upper()

    def get_coupon_by_code(self, code):
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM coupons WHERE code=?",
                               (self.normalize_coupon_code(code),)).fetchone()
            return dict(row) if row else None

    def get_coupon(self, coupon_id):
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM coupons WHERE id=?", (coupon_id,)).fetchone()
            return dict(row) if row else None

    def create_coupon(self, email, amount_cents, source_order_id=None, note=""):
        # 发一张券。amount_cents 必须 > 0。
        amount_cents = int(amount_cents)
        if amount_cents <= 0:
            raise ValueError("优惠券金额必须大于 0")
        with WRITE_LOCK, self._conn() as conn:
            for _ in range(5):
                code = self.generate_coupon_code()
                try:
                    cur = conn.execute(
                        "INSERT INTO coupons(code, email, amount_cents, status,"
                        " source_order_id, note, created_at) VALUES (?, ?, ?, 'unused', ?, ?, ?)",
                        (code, email.strip(), amount_cents, source_order_id, note, now_str()))
                    break
                except sqlite3.IntegrityError:
                    continue
            else:
                raise ValueError("券码生成失败,请重试")
            row = conn.execute("SELECT * FROM coupons WHERE id=?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def validate_coupon(self, code, email):
        # 下单前校验。返回 (coupon, error):error 非空时 coupon 为 None。
        c = self.get_coupon_by_code(code)
        if not c:
            return None, "优惠券不存在"
        if c["status"] == "used":
            return None, "优惠券已使用"
        if c["status"] == "void":
            return None, "优惠券已作废"
        if c["status"] == "locked":
            return None, "优惠券已绑定一笔待支付订单,先支付或取消那笔订单"
        if c["email"].strip().lower() != (email or "").strip().lower():
            return None, "优惠券与下单邮箱不符"
        if c["amount_cents"] <= 0:
            return None, "优惠券金额异常"
        return c, None

    def list_coupons(self, status=None, q=None, limit=30, offset=0):
        with self._conn() as conn:
            cond, args = [], []
            if status:
                cond.append("status=?")
                args.append(status)
            if q:
                cond.append("(code LIKE ? OR email LIKE ?)")
                args += [f"%{q}%", f"%{q}%"]
            where = ("WHERE " + " AND ".join(cond)) if cond else ""
            rows = conn.execute(
                f"SELECT * FROM coupons {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                args + [limit, offset]).fetchall()
            total = conn.execute(f"SELECT COUNT(*) FROM coupons {where}", args).fetchone()[0]
            return [dict(r) for r in rows], total

    def list_coupons_by_email(self, email, limit=20):
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM coupons WHERE email=? AND status != 'void'"
                " ORDER BY id DESC LIMIT ?",
                (email, limit)).fetchall()]

    def get_coupon_by_source_order(self, order_id):
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM coupons WHERE source_order_id=? ORDER BY id DESC LIMIT 1",
                               (order_id,)).fetchone()
            return dict(row) if row else None

    def void_coupon(self, coupon_id):
        # 作废。只对未使用/已锁定的券生效;已使用的券不能作废。
        with WRITE_LOCK, self._conn() as conn:
            row = conn.execute("SELECT status FROM coupons WHERE id=?", (coupon_id,)).fetchone()
            if not row:
                raise ValueError("优惠券不存在")
            if row["status"] == "used":
                raise ValueError("已使用的优惠券不能作废")
            conn.execute("UPDATE coupons SET status='void', locked_order_id=NULL WHERE id=?",
                         (coupon_id,))

    def create_order(self, email, pay_type, items, client_ip="", coupon_code=None):
        with WRITE_LOCK:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                total = 0
                rows = []
                for it in items:
                    p = conn.execute("SELECT * FROM products WHERE id=?", (it["product_id"],)).fetchone()
                    if not p:
                        raise ValueError("商品不存在")
                    if not p["enabled"]:
                        raise ValueError(f"商品已下架:{p['name']}")
                    qty = int(it["qty"])
                    if qty < p["min_qty"] or qty > p["max_qty"]:
                        raise ValueError(f"{p['name']} 购买数量需在 {p['min_qty']}~{p['max_qty']} 之间")
                    if p["fulfill_mode"] != "manual":
                        stock = conn.execute(
                            "SELECT COUNT(*) FROM cards WHERE product_id=? AND status='unused'",
                            (p["id"],)).fetchone()[0]
                        if stock < qty:
                            raise ValueError(f"{p['name']} 库存不足,仅剩 {stock} 件")
                    total += p["price_cents"] * qty
                    rows.append((dict(p), qty))
                if total <= 0:
                    raise ValueError("订单金额异常")
                # 优惠券:同一事务内校验并锁定,防一张券同时下两单
                coupon_id = None
                discount = 0
                if coupon_code:
                    code = self.normalize_coupon_code(coupon_code)
                    crow = conn.execute("SELECT * FROM coupons WHERE code=?", (code,)).fetchone()
                    if not crow:
                        raise ValueError("优惠券不存在")
                    if crow["status"] == "used":
                        raise ValueError("优惠券已使用")
                    if crow["status"] == "void":
                        raise ValueError("优惠券已作废")
                    if crow["status"] == "locked":
                        raise ValueError("优惠券已绑定一笔待支付订单,先支付或取消那笔订单")
                    if crow["email"].strip().lower() != email.strip().lower():
                        raise ValueError("优惠券与下单邮箱不符")
                    if crow["amount_cents"] <= 0:
                        raise ValueError("优惠券金额异常")
                    coupon_id = crow["id"]
                    discount = min(crow["amount_cents"], total)
                for _ in range(5):
                    no = self.generate_out_trade_no()
                    try:
                        cur = conn.execute(
                            "INSERT INTO orders(out_trade_no, email, total_cents, pay_type,"
                            " status, client_ip, created_at, coupon_id, discount_cents)"
                            " VALUES (?, ?, ?, ?, 'unpaid', ?, ?, ?, ?)",
                            (no, email, total, pay_type, client_ip, now_str(),
                             coupon_id, discount))
                        break
                    except sqlite3.IntegrityError:
                        continue
                else:
                    raise ValueError("订单号生成失败,请重试")
                oid = cur.lastrowid
                if coupon_id:
                    conn.execute("UPDATE coupons SET status='locked', locked_order_id=?"
                                 " WHERE id=? AND status='unused'", (oid, coupon_id))
                for p, qty in rows:
                    conn.execute(
                        "INSERT INTO order_items(order_id, product_id, product_name, qty,"
                        " unit_price_cents) VALUES (?, ?, ?, ?, ?)",
                        (oid, p["id"], p["name"], qty, p["price_cents"]))
                conn.commit()
                return self.get_order_by_no(no)
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def get_order(self, order_id):
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row:
                return None
            order = dict(row)
            order["items"] = [dict(r) for r in conn.execute(
                "SELECT * FROM order_items WHERE order_id=? ORDER BY id", (order_id,)).fetchall()]
            return order

    def claim_notify(self, order_id):
        """抢占邮件通知名额(防重复发送)。成功返回 True。"""
        with WRITE_LOCK, self._conn() as conn:
            cur = conn.execute(
                "UPDATE orders SET notified_at=? WHERE id=? AND (notified_at IS NULL OR notified_at='')",
                (now_str(), order_id))
            return cur.rowcount == 1

    def clear_notified(self, order_id):
        with WRITE_LOCK, self._conn() as conn:
            conn.execute("UPDATE orders SET notified_at=NULL WHERE id=?", (order_id,))

    def get_order_by_no(self, out_trade_no):
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM orders WHERE out_trade_no=?", (out_trade_no,)).fetchone()
            if not row:
                return None
            order = dict(row)
            order["items"] = [dict(r) for r in conn.execute(
                "SELECT * FROM order_items WHERE order_id=? ORDER BY id", (order["id"],)).fetchall()]
            return order

    def get_order_cards(self, order_id):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT c.*, p.name AS product_name FROM cards c"
                " JOIN products p ON p.id=c.product_id"
                " WHERE c.order_id=? ORDER BY c.product_id, c.id", (order_id,)).fetchall()
        groups = {}
        for r in rows:
            groups.setdefault(r["product_name"], []).append(r["content"])
        return groups

    def fulfill_order(self, order_id, trade_no="", include_manual=False, manual_contents=None):
        """发货。include_manual=False(支付回调/查单)时只发自动发货商品,
        手动发货商品留待后台处理;include_manual=True(后台手动发货)时全发。
        manual_contents={product_id:[内容行]}:手动发货商品优先用填入的内容直接发货
        (一行一条,无需库存);没填内容时回退到库存分配。按已发卡数补发剩余数量,
        缺货时状态为 paid_unfulfilled。"""
        with WRITE_LOCK:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
                if not order:
                    raise ValueError("订单不存在")
                if order["status"] == "cancelled":
                    raise ValueError("订单已取消")
                if order["status"] == "paid":
                    conn.rollback()
                    return dict(order)
                items = conn.execute(
                    "SELECT oi.*, COALESCE(p.fulfill_mode, 'auto') AS fulfill_mode"
                    " FROM order_items oi LEFT JOIN products p ON p.id = oi.product_id"
                    " WHERE oi.order_id=? ORDER BY oi.id", (order_id,)).fetchall()
                short = []
                manual_pending = False
                for it in items:
                    allocated = conn.execute(
                        "SELECT COUNT(*) FROM cards WHERE order_id=? AND product_id=?",
                        (order_id, it["product_id"])).fetchone()[0]
                    need = it["qty"] - allocated
                    if it["fulfill_mode"] == "manual" and not include_manual:
                        if need > 0:
                            manual_pending = True
                        continue
                    if need <= 0:
                        continue
                    if it["fulfill_mode"] == "manual" and include_manual:
                        lines = (manual_contents or {}).get(it["product_id"]) or []
                        for ln in lines:
                            conn.execute(
                                "INSERT INTO cards(product_id, content, source, status, order_id,"
                                " created_at, used_at) VALUES (?, ?, 'manual', 'used', ?, ?, ?)",
                                (it["product_id"], ln, order_id, now_str(), now_str()))
                        if lines:
                            continue
                    cards = conn.execute(
                        "SELECT id FROM cards WHERE product_id=? AND status='unused'"
                        " ORDER BY id LIMIT ?", (it["product_id"], need)).fetchall()
                    for c in cards:
                        conn.execute(
                            "UPDATE cards SET status='used', order_id=?, used_at=? WHERE id=?",
                            (order_id, now_str(), c["id"]))
                    if len(cards) < need:
                        short.append(f"{it['product_name']} 缺 {need - len(cards)} 件")
                if short:
                    status = "paid_unfulfilled"
                    note = "; ".join(short)
                elif manual_pending:
                    status = "paid_manual"
                    note = "含手动发货商品,等待商家发货"
                else:
                    status = "paid"
                    note = ""
                conn.execute(
                    "UPDATE orders SET status=?, trade_no=?, note=?, paid_at=? WHERE id=?",
                    (status, trade_no or order["trade_no"], note,
                     order["paid_at"] or now_str(), order_id))
                # 订单进入已付状态,核销锁定的优惠券(只核销锁给本单的那张)
                if order["coupon_id"]:
                    conn.execute(
                        "UPDATE coupons SET status='used', used_at=?, used_order_id=?"
                        " WHERE id=? AND status='locked' AND locked_order_id=?",
                        (now_str(), order_id, order["coupon_id"], order_id))
                conn.commit()
                return self.get_order_by_no(order["out_trade_no"])
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def cancel_order(self, order_id):
        # 取消订单。返回 {"prev_status": ..., "coupon": ...}:
        # 已取消的订单重复取消是空操作(绝不重复发券);取消已付订单(退款)时
        # 自动发一张等额优惠券(金额 = 实付金额),供买家重新下单抵扣。
        # 取消待支付订单时,释放该单锁定的优惠券。
        with WRITE_LOCK:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
                if not order:
                    conn.rollback()
                    return {"prev_status": None, "coupon": None}
                prev_status = order["status"]
                if prev_status == "cancelled":
                    conn.rollback()
                    return {"prev_status": prev_status, "coupon": None}
                # 手动现填的内容是专属的,取消后直接删除,绝不能回流库存被二次出售;
                # 只有预导入的库存卡才释放回未售出。
                conn.execute("DELETE FROM cards WHERE order_id=? AND source='manual'", (order_id,))
                conn.execute(
                    "UPDATE cards SET status='unused', order_id=NULL, used_at=NULL"
                    " WHERE order_id=? AND source='stock'",
                    (order_id,))
                coupon = None
                if order["coupon_id"]:
                    # 本单锁定的券:待支付取消时释放回未使用;已付订单的券已核销,
                    # WHERE 条件保证核销过的券不会被误释放。
                    conn.execute(
                        "UPDATE coupons SET status='unused', locked_order_id=NULL"
                        " WHERE id=? AND status='locked' AND locked_order_id=?",
                        (order["coupon_id"], order_id))
                if prev_status in ("paid", "paid_unfulfilled", "paid_manual"):
                    paid_cents = order["total_cents"] - (order["discount_cents"] or 0)
                    if paid_cents > 0:
                        existing = conn.execute(
                            "SELECT id FROM coupons WHERE source_order_id=? LIMIT 1",
                            (order_id,)).fetchone()
                        if not existing:
                            for _ in range(5):
                                code = self.generate_coupon_code()
                                try:
                                    cur = conn.execute(
                                        "INSERT INTO coupons(code, email, amount_cents, status,"
                                        " source_order_id, note, created_at)"
                                        " VALUES (?, ?, ?, 'unused', ?, ?, ?)",
                                        (code, order["email"], paid_cents, order_id,
                                         "订单退款补偿", now_str()))
                                    break
                                except sqlite3.IntegrityError:
                                    continue
                            else:
                                raise ValueError("券码生成失败,请重试")
                            coupon = dict(conn.execute(
                                "SELECT * FROM coupons WHERE id=?", (cur.lastrowid,)).fetchone())
                conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
                conn.commit()
                return {"prev_status": prev_status, "coupon": coupon}
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def delete_order(self, order_id):
        with WRITE_LOCK, self._conn() as conn:
            row = conn.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row:
                raise ValueError("订单不存在")
            if row["status"] in ("paid", "paid_unfulfilled", "paid_manual"):
                raise ValueError("已支付订单不能删除,请先取消(会释放卡密)")
            conn.execute("DELETE FROM order_items WHERE order_id=?", (order_id,))
            conn.execute("DELETE FROM orders WHERE id=?", (order_id,))

    def list_orders(self, status=None, q=None, limit=30, offset=0):
        with self._conn() as conn:
            cond, args = [], []
            if status:
                cond.append("status=?")
                args.append(status)
            if q:
                cond.append("(out_trade_no LIKE ? OR email LIKE ?)")
                args += [f"%{q}%", f"%{q}%"]
            where = ("WHERE " + " AND ".join(cond)) if cond else ""
            rows = conn.execute(
                f"SELECT * FROM orders {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                args + [limit, offset]).fetchall()
            total = conn.execute(f"SELECT COUNT(*) FROM orders {where}", args).fetchone()[0]
            return [dict(r) for r in rows], total

    def search_orders_by_email(self, email, limit=20):
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM orders WHERE email=? ORDER BY id DESC LIMIT ?",
                (email, limit)).fetchall()]

    # ---------- 免签回调记录 ----------
    def log_callback(self, channel, dedup_key, payload, result, matched_order=""):
        """记录一次监听回调。同一 dedup_key 首次记录;之后只有 'matched'
        结果可以覆盖先前的未匹配记录(防重放:一旦标记为已匹配,永不改回)。"""
        with WRITE_LOCK, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO payment_callbacks"
                "(channel, dedup_key, payload, result, matched_order, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(dedup_key) DO UPDATE SET result=excluded.result,"
                " matched_order=excluded.matched_order, created_at=excluded.created_at"
                " WHERE excluded.result='matched' AND payment_callbacks.result != 'matched'",
                (channel, dedup_key, payload, result, matched_order, now_str()))
            return cur.rowcount == 1

    def callback_matched(self, dedup_key):
        """该回调是否已成功匹配并发货。返回匹配的订单号,未匹配返回 None。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT matched_order FROM payment_callbacks"
                " WHERE dedup_key=? AND result='matched'", (dedup_key,)).fetchone()
            return row["matched_order"] if row else None

    def callback_seen(self, dedup_key):
        with self._conn() as conn:
            row = conn.execute("SELECT id FROM payment_callbacks WHERE dedup_key=?",
                               (dedup_key,)).fetchone()
            return row is not None

    def find_unpaid_by_amount(self, pay_type, total_cents, window_minutes=30):
        """窗口内指定金额的未支付订单(新的在前),供免签金额匹配用。
        截止时间在 Python 里按本地时间算,与 created_at 的时钟一致。"""
        try:
            window_minutes = int(window_minutes)
        except (TypeError, ValueError):
            window_minutes = 30
        window_minutes = max(1, min(window_minutes, 1440))
        cutoff = (datetime.now() - timedelta(minutes=window_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM orders WHERE pay_type=? AND status='unpaid'"
                " AND total_cents - COALESCE(discount_cents, 0) = ? AND created_at >= ?"
                " ORDER BY id DESC",
                (pay_type, total_cents, cutoff)).fetchall()]

    def stats(self):
        with self._conn() as conn:
            today = time.strftime("%Y-%m-%d")
            paid = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(total_cents),0) FROM orders"
                " WHERE status IN ('paid','paid_unfulfilled','paid_manual') AND substr(paid_at,1,10)=?",
                (today,)).fetchone()
            total = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(total_cents),0) FROM orders"
                " WHERE status IN ('paid','paid_unfulfilled','paid_manual')").fetchone()
            return {
                "today_orders": paid[0], "today_cents": paid[1],
                "total_orders": total[0], "total_cents": total[1],
                "products": conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
                "stock": conn.execute("SELECT COUNT(*) FROM cards WHERE status='unused'").fetchone()[0],
                "unpaid": conn.execute("SELECT COUNT(*) FROM orders WHERE status='unpaid'").fetchone()[0],
                "unfulfilled": conn.execute(
                    "SELECT COUNT(*) FROM orders WHERE status='paid_unfulfilled'").fetchone()[0],
                "manual_pending": conn.execute(
                    "SELECT COUNT(*) FROM orders WHERE status='paid_manual'").fetchone()[0],
                "coupons_unused": conn.execute(
                    "SELECT COUNT(*) FROM coupons WHERE status='unused'").fetchone()[0],
            }


def seed_demo(store):
    if store.list_products(include_disabled=True):
        return
    c1 = store.add_category("账号专区", 1)
    c2 = store.add_category("充值卡密", 2)
    c3 = store.add_category("软件服务", 3)
    p1 = store.add_product(c1, "【演示】测试账号·含邮箱+2FA", 100,
                           description="演示商品,下单后自动发货。\n卡密格式:账号----密码----2FA密钥",
                           sort=1, recommend=1)
    store.add_cards(p1, [f"demo-user-{i:03d}@example.com----Pass1234----JBSWY3DPEHPK3PXP"
                         for i in range(1, 6)])
    p2 = store.add_product(c1, "【演示】测试账号·普通版", 50,
                           description="演示商品,便宜好用。", sort=2)
    store.add_cards(p2, [f"demo-normal-{i:03d}----Pass{i:03d}" for i in range(1, 11)])
    p3 = store.add_product(c2, "【演示】100 元充值卡", 9800,
                           description="演示商品:卡号+卡密,到账后自行充值。", sort=1)
    store.add_cards(p3, [f"8888-0000-000{i}----DEMO{i:04d}" for i in range(1, 4)])
    p4 = store.add_product(c3, "【演示】月度会员激活码", 1500,
                           description="演示商品:激活码一枚。手动发货示例。", sort=1,
                           fulfill_mode="manual")
    store.add_cards(p4, [f"VIP-DEMO-{i:04d}-XXXX" for i in range(1, 8)])
    store.set_setting("site_name", "我的发卡网(演示)")
    store.set_setting("announcement", "演示数据:请先在后台「支付」里配好支付通道,支付成功后自动发卡(手动发货商品除外)。")
