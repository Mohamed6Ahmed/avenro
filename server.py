#!/usr/bin/env python3
"""Avenro — zero-dependency server (Python 3.9+ stdlib only).
   python server.py [--port 8000] [--reset-db] [--open]
   Settings come from environment variables or a .env file (see .env.example)."""
import hashlib, hmac, html, http.cookies, json, os, re, secrets, sqlite3, sys, threading, time, traceback, urllib.error, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "db", "avenro.db")
PUBLIC, ASSETS = os.path.join(ROOT, "public"), os.path.join(ROOT, "assets")

# ───────────────────────── config ─────────────────────────
def load_env():
    f = os.path.join(ROOT, ".env")
    if os.path.isfile(f):
        for line in open(f, encoding="utf-8-sig"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if not os.environ.get(k): os.environ[k] = v      # a real value wins over an empty one
load_env()
def env(k, d=""): return os.environ.get(k, d).strip()

CURRENCY = env("CURRENCY", "EGP").upper()
SHIPPING_FEE = float(env("SHIPPING_FEE", "0") or 0)
ENABLE_COD = env("ENABLE_COD", "1") == "1"            # cash on delivery
COOKIE_SECURE = env("COOKIE_SECURE", "0") == "1"      # set to 1 once the site runs on https
HOST = env("HOST", "127.0.0.1")
PAYMOB = {k: env("PAYMOB_" + k.upper()) for k in ("secret_key", "public_key", "integration_id", "hmac_secret")}
PAYMOB_BASE = env("PAYMOB_BASE_URL", "https://accept.paymob.com").rstrip("/")
# ONLINE_PAYMENTS: paymob (real) | mock (fake test page, no money) | off.  Default: paymob if all 4 keys are set, otherwise mock.
ONLINE = env("ONLINE_PAYMENTS") or ("paymob" if all(PAYMOB.values()) else "mock")
if ONLINE == "paymob" and not all(PAYMOB.values()): sys.exit("ONLINE_PAYMENTS=paymob but a PAYMOB_* key is missing in .env")
PUBLIC_URL = env("PUBLIC_URL")                        # filled in at startup if empty
# Google sign-in (OAuth 2.0 authorization-code flow). Both values come from console.cloud.google.com -> APIs & Services -> Credentials.
GOOGLE = {"client_id": env("GOOGLE_CLIENT_ID"), "client_secret": env("GOOGLE_CLIENT_SECRET")}
GOOGLE_ON = all(GOOGLE.values())
GOOGLE_AUTH_URL = env("GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
GOOGLE_TOKEN_URL = env("GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")
GOOGLE_USERINFO_URL = env("GOOGLE_USERINFO_URL", "https://openidconnect.googleapis.com/v1/userinfo")
# Extra payment methods. Vodafone Cash: automatic through a Paymob wallet integration, otherwise a manual transfer to your own number.
# InstaPay: manual transfer to your InstaPay address (a PSP such as Kashier is needed for automatic InstaPay).
PAYMOB_WALLET_ID = env("PAYMOB_WALLET_INTEGRATION_ID")
VF_NUMBER, VF_NAME = env("VODAFONE_CASH_NUMBER"), env("VODAFONE_CASH_NAME")
IPA_ADDRESS, IPA_NAME = env("INSTAPAY_ADDRESS"), env("INSTAPAY_NAME")
ADMIN_TOKEN = env("ADMIN_TOKEN")                      # protects /admin.html (confirming manual payments)
MANUAL_HOLD_MINUTES = int(env("MANUAL_HOLD_MINUTES", "180") or 180)
SESSION_COOKIE, SESSION_DAYS, PENDING_MINUTES = "avenro_session", 30, 30

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "application/javascript", ".json": "application/json", ".svg": "image/svg+xml",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".mp4": "video/mp4", ".webm": "video/webm",
        ".woff2": "font/woff2", ".woff": "font/woff", ".ico": "image/x-icon"}   # fixed table: Windows registry mime types are unreliable

def method_kind(m):
    """'online' (customer is sent to Paymob), 'manual' (customer transfers money, you confirm it), 'cod', or None if not available."""
    if m == "card": return "online" if ONLINE != "off" else None
    if m == "vodafone_cash": return "online" if ONLINE == "paymob" and PAYMOB_WALLET_ID else "manual" if VF_NUMBER else None
    if m == "instapay": return "manual" if IPA_ADDRESS else None
    if m == "cod": return "cod" if ENABLE_COD else None
    return None

def methods_cfg():
    out = []
    for key, label in (("card", "Visa / Mastercard"), ("vodafone_cash", "Vodafone Cash"), ("instapay", "InstaPay"), ("cod", "Cash on delivery")):
        k = method_kind(key)
        if not k: continue
        m = {"key": key, "label": label, "kind": k}
        if k == "manual": m.update(account=VF_NUMBER if key == "vodafone_cash" else IPA_ADDRESS, holder=VF_NAME if key == "vodafone_cash" else IPA_NAME)
        out.append(m)
    return out

class ApiError(Exception):
    def __init__(self, code, msg): self.code, self.msg = code, msg

# ───────────────────────── database ─────────────────────────
def conn():
    c = sqlite3.connect(DB, timeout=15, isolation_level=None)   # autocommit; transactions are explicit (BEGIN IMMEDIATE)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA journal_mode=WAL")
    return c

NEW_ORDER_COLS = {"user_id": "INTEGER REFERENCES users(id)", "reference": "TEXT", "ship_name": "TEXT", "ship_phone": "TEXT", "ship_address": "TEXT",
                  "ship_city": "TEXT", "notes": "TEXT", "payment_method": "TEXT NOT NULL DEFAULT 'cod'", "payment_status": "TEXT NOT NULL DEFAULT 'unpaid'",
                  "currency": "TEXT", "shipping": "REAL NOT NULL DEFAULT 0", "provider_ref": "TEXT", "updated_at": "TEXT"}

def migrate(c):
    """Upgrades a database created by an older version (keeps existing orders/subscribers)."""
    have = {r["name"] for r in c.execute("PRAGMA table_info(orders)")}
    for name, ddl in NEW_ORDER_COLS.items():
        if name not in have: c.execute("ALTER TABLE orders ADD COLUMN %s %s" % (name, ddl))
    c.execute("UPDATE orders SET reference='AV-L'||id WHERE reference IS NULL")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS orders_reference ON orders(reference)")
    c.execute("CREATE INDEX IF NOT EXISTS orders_user ON orders(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS orders_provider_ref ON orders(provider_ref)")
    c.execute("CREATE INDEX IF NOT EXISTS sessions_exp ON sessions(expires_at)")
    c.execute("DROP TABLE IF EXISTS videos")                        # video feature was removed

def init_db(reset=False):
    if reset:
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(DB + ext): os.remove(DB + ext)
    c = conn()
    c.executescript(open(os.path.join(ROOT, "db", "schema.sql"), encoding="utf-8").read())
    migrate(c)
    c.executescript(open(os.path.join(ROOT, "db", "seed.sql"), encoding="utf-8").read())
    c.close()

class tx:
    """with tx(c): ...  → BEGIN IMMEDIATE / COMMIT (ROLLBACK on error). Immediate = stock checks can't race."""
    def __init__(self, c): self.c = c
    def __enter__(self): self.c.execute("BEGIN IMMEDIATE")
    def __exit__(self, et, ev, tb): self.c.execute("ROLLBACK" if et else "COMMIT")

# ───────────────────────── auth helpers ─────────────────────────
PBKDF2_N = 600_000
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_N, salt.hex(), hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_N).hex())
def check_pw(pw, stored):
    try: _, n, salt, d = stored.split("$"); calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(n)).hex()
    except ValueError: return False
    return hmac.compare_digest(calc, d)
DUMMY_HASH = hash_pw("dummy-password")                    # so unknown emails cost the same time as wrong passwords
sha = lambda s: hashlib.sha256(s.encode()).hexdigest()
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
PHONE_RE = re.compile(r"\+?\d{8,15}")

FAILS, FAILS_LOCK = {}, threading.Lock()                  # login throttle: 8 failures / 10 min per (ip, email)
def throttled(key):
    with FAILS_LOCK:
        FAILS[key] = [t for t in FAILS.get(key, []) if t > time.time() - 600]
        return len(FAILS[key]) >= 8
def note_fail(key):
    with FAILS_LOCK: FAILS.setdefault(key, []).append(time.time())

def clean(v, lo, hi, what):
    v = re.sub(r"\s+", " ", str(v or "")).strip()
    if len(v) < lo: raise ApiError(400, "%s is required" % what)
    if len(v) > hi: raise ApiError(400, "%s is too long" % what)
    return v
def public_user(u): return {"id": u["id"], "name": u["name"], "email": u["email"], "phone": u["phone"], "created_at": u["created_at"], "has_password": u["password_hash"] != "!"}

# ───────────────────────── orders & payments ─────────────────────────
def cancel_order(c, oid):
    """Releases reserved stock of an unpaid online order. Caller holds a transaction."""
    o = c.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or o["status"] != "pending_payment": return
    for it in c.execute("SELECT variant_id,quantity FROM order_items WHERE order_id=?", (oid,)).fetchall():
        c.execute("UPDATE variants SET stock=stock+? WHERE id=?", (it["quantity"], it["variant_id"]))
    c.execute("UPDATE orders SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE id=?", (oid,))

def sweep_expired(c):
    man = [m for m in ("vodafone_cash", "instapay") if method_kind(m) == "manual"]
    base = "SELECT id FROM orders WHERE status='pending_payment' AND payment_status!='verifying' AND created_at < datetime('now', ?)"
    ph = ",".join("?" * len(man))
    ids = [r["id"] for r in c.execute(base + (" AND payment_method NOT IN (%s)" % ph if man else ""), ("-%d minutes" % PENDING_MINUTES,) + tuple(man))]
    if man: ids += [r["id"] for r in c.execute(base + " AND payment_method IN (%s)" % ph, ("-%d minutes" % MANUAL_HOLD_MINUTES,) + tuple(man))]
    for oid in ids: cancel_order(c, oid)

def order_view(c, o):
    items = [dict(r) for r in c.execute("SELECT p.name,p.slug,p.image,v.size,i.quantity,i.unit_price FROM order_items i JOIN variants v ON v.id=i.variant_id JOIN products p ON p.id=v.product_id WHERE i.order_id=?", (o["id"],))]
    return {"reference": o["reference"], "status": o["status"], "payment_method": o["payment_method"], "payment_status": o["payment_status"], "total": o["total"],
            "shipping": o["shipping"], "currency": o["currency"] or CURRENCY, "created_at": o["created_at"], "items": items,
            "ship": {"name": o["ship_name"], "phone": o["ship_phone"], "address": o["ship_address"], "city": o["ship_city"]}}

def apply_txn(c, order, t):
    """Records a payment notification and updates the order. Safe to call many times for the same transaction (webhooks are retried)."""
    if t["refunded"]: st = "refunded"
    elif t["voided"]: st = "voided"
    elif t["pending"]: st = "pending"
    elif t["success"]: st = "paid"
    else: st = "failed"
    if st == "paid" and (t["amount_cents"] != round(order["total"] * 100) or t["currency"].upper() != (order["currency"] or CURRENCY).upper()):
        print("!! payment amount/currency mismatch for", order["reference"], t); st = "mismatch"
    with tx(c):
        c.execute("INSERT INTO payments(order_id,provider,provider_txn_id,amount_cents,currency,status,raw) VALUES(?,?,?,?,?,?,?) "
                  "ON CONFLICT(provider,provider_txn_id) DO UPDATE SET status=excluded.status,raw=excluded.raw",
                  (order["id"], t["provider"], t["txn_id"], t["amount_cents"], t["currency"], st, json.dumps(t.get("raw"))[:20000]))
        cur = c.execute("SELECT status,payment_status FROM orders WHERE id=?", (order["id"],)).fetchone()
        if st == "paid" and cur["payment_status"] != "paid":
            if cur["status"] == "cancelled":                       # paid after our 30-min hold expired: take the stock again
                for it in c.execute("SELECT variant_id,quantity FROM order_items WHERE order_id=?", (order["id"],)).fetchall():
                    c.execute("UPDATE variants SET stock=MAX(stock-?,0) WHERE id=?", (it["quantity"], it["variant_id"]))
            c.execute("UPDATE orders SET payment_status='paid',status='confirmed',updated_at=CURRENT_TIMESTAMP WHERE id=?", (order["id"],))
        elif st in ("refunded", "voided"):
            c.execute("UPDATE orders SET payment_status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (st, order["id"]))
        elif st == "failed" and cur["payment_status"] != "paid":   # the customer may retry inside the same checkout, so the order stays open until the hold expires
            c.execute("UPDATE orders SET payment_status='failed',updated_at=CURRENT_TIMESTAMP WHERE id=?", (order["id"],))
    return st

# Paymob ---------------------------------------------------------------------------------------------
HMAC_KEYS = ["amount_cents", "created_at", "currency", "error_occured", "has_parent_transaction", "id", "integration_id", "is_3d_secure", "is_auth",
             "is_capture", "is_refunded", "is_standalone_payment", "is_voided", "order.id", "owner", "pending", "source_data.pan", "source_data.sub_type",
             "source_data.type", "success"]
def _s(v): return "true" if v is True else "false" if v is False else "" if v is None else str(v)

def paymob_check(values, given):
    """values: {hmac_key: value}. Paymob signs the 20 fields above, concatenated in this order, with HMAC-SHA512 and your HMAC secret."""
    msg = "".join(_s(values.get(k)) for k in HMAC_KEYS)
    exp = hmac.new(PAYMOB["hmac_secret"].encode(), msg.encode(), hashlib.sha512).hexdigest()
    return hmac.compare_digest(exp, (given or "").lower())

def paymob_from_post(payload):                                # "Transaction processed callback" (server → server, JSON)
    o = payload.get("obj") or {}
    flat = {k: o.get(k) for k in HMAC_KEYS if "." not in k}
    flat["order.id"] = (o.get("order") or {}).get("id")
    for k in ("pan", "sub_type", "type"): flat["source_data." + k] = (o.get("source_data") or {}).get(k)
    return flat, o, str((o.get("order") or {}).get("merchant_order_id") or ""), str((o.get("order") or {}).get("id") or "")

def paymob_from_get(q):                                       # "Transaction response callback" (browser redirect, query string)
    g = lambda *names: next((q[n][0] for n in names if n in q), None)
    flat = {k: g(k) for k in HMAC_KEYS if "." not in k}
    flat["order.id"] = g("order", "order_id")
    for k in ("pan", "sub_type", "type"): flat["source_data." + k] = g("source_data." + k, "source_data_" + k)
    return flat, {k: v[0] for k, v in q.items()}, str(g("merchant_order_id") or ""), str(g("order", "order_id") or "")

def paymob_txn(flat, raw):
    b = lambda v: str(v).lower() == "true"
    return {"provider": "paymob", "txn_id": str(flat["id"]), "success": b(flat["success"]), "pending": b(flat["pending"]), "voided": b(flat["is_voided"]),
            "refunded": b(flat["is_refunded"]), "amount_cents": int(flat["amount_cents"] or 0), "currency": str(flat["currency"] or ""), "raw": raw}

def find_order(c, merchant_ref, paymob_order_id):
    o = c.execute("SELECT * FROM orders WHERE reference=?", (merchant_ref,)).fetchone() if merchant_ref else None
    return o or (c.execute("SELECT * FROM orders WHERE provider_ref=?", (paymob_order_id,)).fetchone() if paymob_order_id else None)

def paymob_checkout_url(order, items, user, integration_id=None):
    """Creates a Paymob 'intention' (their current API) and returns the hosted checkout URL."""
    first, _, last = order["ship_name"].partition(" ")
    lines = [{"name": n[:50], "amount": round(p * 100), "description": n[:100], "quantity": q} for n, p, q in items]
    if order["shipping"] > 0: lines.append({"name": "Shipping", "amount": round(order["shipping"] * 100), "description": "Shipping", "quantity": 1})
    body = {"amount": sum(l["amount"] * l["quantity"] for l in lines), "currency": CURRENCY, "payment_methods": [int(integration_id or PAYMOB["integration_id"])], "items": lines,
            "billing_data": {"first_name": first or "NA", "last_name": last or "NA", "email": order["email"], "phone_number": order["ship_phone"], "country": "EG",
                             "city": order["ship_city"], "street": order["ship_address"][:100], "building": "NA", "floor": "NA", "apartment": "NA", "state": "NA"},
            "special_reference": order["reference"], "notification_url": PUBLIC_URL + "/api/payments/paymob/webhook",
            "redirection_url": PUBLIC_URL + "/api/payments/paymob/return"}
    req = urllib.request.Request(PAYMOB_BASE + "/v1/intention/", json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Token " + PAYMOB["secret_key"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r: res = json.load(r)
    except urllib.error.HTTPError as e:
        print("!! Paymob refused the intention:", e.code, e.read()[:500]); raise ApiError(502, "The payment provider rejected the request. Try again or pay on delivery.")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        print("!! Paymob unreachable:", e); raise ApiError(502, "The payment provider is unreachable. Try again or pay on delivery.")
    if not res.get("client_secret"): print("!! Paymob reply without client_secret:", res); raise ApiError(502, "The payment provider returned an unexpected answer.")
    return str(res.get("intention_order_id") or res.get("id") or ""), "%s/unifiedcheckout/?%s" % (PAYMOB_BASE, urlencode({"publicKey": PAYMOB["public_key"], "clientSecret": res["client_secret"]}))

def http_json(url, form=None, headers=None):
    req = urllib.request.Request(url, urlencode(form).encode() if form else None, headers=headers or {}, method="POST" if form else "GET")
    with urllib.request.urlopen(req, timeout=15) as r: return json.load(r)

# ───────────────────────── HTTP ─────────────────────────
MOCK_PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Test payment</title>
<body style="font:15px Jost,Helvetica,sans-serif;background:#f2efe8;color:#0e0e0e;display:grid;place-items:center;min-height:100vh;margin:0"><main style="max-width:420px;padding:32px;border:1px solid #d3cdc2">
<p style="font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:#6b6359">Test mode — no real money</p><h1 style="font:400 30px Georgia,serif;margin:0 0 8px">Pay %(total)s</h1>
<p>Order %(ref)s. This page stands in for the real card page until you add your Paymob keys to <code>.env</code>.</p>
<button onclick="go('success')" style="background:#0e0e0e;color:#fff;border:0;padding:14px 22px;cursor:pointer">Simulate successful payment</button>
<button onclick="go('failed')" style="background:none;border:1px solid #0e0e0e;padding:14px 22px;cursor:pointer;margin-left:8px">Simulate failure</button></main>
<script>async function go(r){await fetch('/api/payments/mock/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reference:%(refjs)s,result:r})});location='/?order='+encodeURIComponent(%(refjs)s)}</script>"""

class H(BaseHTTPRequestHandler):
    # --- plumbing
    def send_json(self, obj, code=200, headers=()):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.send_header("Cache-Control", "no-store")
        for k, v in headers: self.send_header(k, v)
        self.end_headers(); self.wfile.write(b)

    def redirect(self, to, headers=()):
        self.send_response(303); self.send_header("Location", to); self.send_header("Content-Length", "0")
        for k, v in headers: self.send_header(k, v)
        self.end_headers()

    def read_body(self, limit):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit: raise ApiError(413, "Request too large")
        return self.rfile.read(n)

    def json_body(self, limit=20000):
        if not (self.headers.get("Content-Type") or "").startswith("application/json"): raise ApiError(415, "Send JSON")
        try: d = json.loads(self.read_body(limit) or b"{}")
        except ValueError: raise ApiError(400, "Bad request")
        if not isinstance(d, dict): raise ApiError(400, "Bad request")
        return d

    def check_origin(self):                                   # CSRF: browsers always send Origin on cross-site POSTs
        o = self.headers.get("Origin")
        if o and urlparse(o).netloc != self.headers.get("Host"): raise ApiError(403, "Cross-site request blocked")

    def current_user(self, c):
        try: tok = http.cookies.SimpleCookie(self.headers.get("Cookie", "")).get(SESSION_COOKIE)
        except http.cookies.CookieError: return None
        if not tok: return None
        return c.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?", (sha(tok.value), int(time.time()))).fetchone()

    def need_user(self, c):
        u = self.current_user(c)
        if not u: raise ApiError(401, "Please sign in first")
        return u

    def cookie(self, value, max_age):
        return ("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d%s" % (SESSION_COOKIE, value, max_age, "; Secure" if COOKIE_SECURE else ""))

    def start_session(self, c, uid):
        tok = secrets.token_urlsafe(32); exp = int(time.time()) + SESSION_DAYS * 86400
        c.execute("DELETE FROM sessions WHERE expires_at<?", (int(time.time()),))
        c.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)", (sha(tok), uid, exp))
        return self.cookie(tok, SESSION_DAYS * 86400)

    def dispatch(self, fn):
        try: fn()
        except ApiError as e: self.send_json({"error": e.msg}, e.code)
        except (ConnectionResetError, BrokenPipeError): pass
        except Exception: traceback.print_exc(); self.send_json({"error": "Server error"}, 500)

    # --- GET
    def do_GET(self): self.dispatch(self._get)
    def _get(self):
        u, p = urlparse(self.path), urlparse(self.path).path
        if p == "/api/config":
            return self.send_json({"currency": CURRENCY, "shipping_fee": SHIPPING_FEE, "cod": ENABLE_COD, "card": ONLINE != "off", "test_mode": ONLINE == "mock",
                                   "methods": methods_cfg(), "google": GOOGLE_ON, "manual_hold_minutes": MANUAL_HOLD_MINUTES})
        c = conn()
        try:
            if p == "/api/products":
                rows = [dict(r) for r in c.execute("SELECT id,slug,name,price,image,collection,runway FROM products WHERE active=1 ORDER BY sort,id")]
                for r in rows: r["variants"] = [dict(v) for v in c.execute("SELECT id,size,sku,stock FROM variants WHERE product_id=?", (r["id"],))]
                return self.send_json(rows)
            if p == "/api/collections":                              # tabs of the shop: only collections that have something on sale
                return self.send_json([dict(r) for r in c.execute("SELECT k.slug,k.title FROM collections k WHERE EXISTS(SELECT 1 FROM products p WHERE p.collection=k.slug AND p.active=1) ORDER BY k.sort,k.slug")])
            if p == "/api/looks":                                    # carousel + "In this look" panel
                out = []
                for l in c.execute("SELECT * FROM looks WHERE active=1 ORDER BY sort,id").fetchall():
                    items = [dict(r) for r in c.execute("SELECT p.slug,p.image,i.pin_x,i.pin_y,i.is_new FROM look_items i JOIN products p ON p.id=i.product_id WHERE i.look_id=? AND p.active=1 ORDER BY i.sort,p.id", (l["id"],))]
                    if items: out.append({"slug": l["slug"], "name": l["name"], "tagline": l["tagline"], "image": l["image"] or items[0]["image"], "items": items})
                return self.send_json(out)
            if p == "/api/auth/google/start": return self.google_start()
            if p == "/api/auth/google/callback": return self.google_callback(c, parse_qs(u.query))
            if p == "/api/admin/orders": return self.admin_orders(c)
            if p == "/api/auth/me":
                me = self.current_user(c)
                return self.send_json({"user": public_user(me) if me else None, "google": bool(me and c.execute("SELECT 1 FROM identities WHERE user_id=? AND provider='google'", (me["id"],)).fetchone())})
            if p == "/api/account/addresses":
                me = self.need_user(c)
                return self.send_json([dict(r) for r in c.execute("SELECT id,label,name,phone,address,city,is_default FROM addresses WHERE user_id=? ORDER BY is_default DESC,id DESC", (me["id"],))])
            if p == "/api/orders/mine":
                me = self.need_user(c)
                with tx(c): sweep_expired(c)
                return self.send_json([order_view(c, o) for o in c.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 50", (me["id"],))])
            if p == "/api/payments/paymob/return":                  # the customer's browser comes back from Paymob here
                dest = "/"
                if ONLINE == "paymob":
                    flat, raw, mref, pid = paymob_from_get(parse_qs(u.query))
                    o = find_order(c, mref, pid)
                    if o:
                        dest = "/?order=" + o["reference"]
                        if flat.get("id") and paymob_check(flat, (parse_qs(u.query).get("hmac") or [""])[0]): apply_txn(c, o, paymob_txn(flat, raw))
                        # if the signature does not verify we change nothing: the webhook (or the next page load) decides
                return self.redirect(dest)
            if p == "/pay/mock" and ONLINE == "mock":
                me = self.need_user(c); ref = (parse_qs(u.query).get("ref") or [""])[0]
                o = c.execute("SELECT * FROM orders WHERE reference=? AND user_id=? AND status='pending_payment'", (ref, me["id"])).fetchone()
                if not o: raise ApiError(404, "Order not found")
                page = (MOCK_PAGE % {"total": html.escape("%.2f %s" % (o["total"], CURRENCY)), "ref": html.escape(ref), "refjs": json.dumps(ref).replace("<", "\\u003c")}).encode()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(page))); self.end_headers()
                return self.wfile.write(page)
        finally: c.close()
        if p.startswith("/assets/"): return self.static(ASSETS, p[len("/assets/"):])
        if p.startswith("/api/"): raise ApiError(404, "Not found")
        return self.static(PUBLIC, "index.html" if p == "/" else p.lstrip("/"))

    # --- POST
    def do_POST(self): self.dispatch(self._post)
    def _post(self):
        p = urlparse(self.path).path
        if p == "/api/payments/paymob/webhook": return self.paymob_webhook()      # no cookie/origin: authenticated by HMAC instead
        self.check_origin(); d = self.json_body()
        c = conn()
        try:
            if p == "/api/subscribe":
                email = str(d.get("email", "")).strip()[:254]
                if not EMAIL_RE.fullmatch(email): raise ApiError(400, "Valid email required")
                c.execute("INSERT OR IGNORE INTO subscribers(email) VALUES(?)", (email,)); return self.send_json({"ok": True})
            if p == "/api/auth/register":
                name, email = clean(d.get("name"), 2, 80, "Name"), str(d.get("email", "")).strip().lower()
                phone, pw = re.sub(r"[\s-]", "", str(d.get("phone") or "")), str(d.get("password") or "")
                if not EMAIL_RE.fullmatch(email) or len(email) > 254: raise ApiError(400, "Valid email required")
                if phone and not PHONE_RE.fullmatch(phone): raise ApiError(400, "Phone number looks wrong")
                if not 8 <= len(pw) <= 128: raise ApiError(400, "Password must be 8–128 characters")
                try: uid = c.execute("INSERT INTO users(email,name,phone,password_hash) VALUES(?,?,?,?)", (email, name, phone or None, hash_pw(pw))).lastrowid
                except sqlite3.IntegrityError: raise ApiError(409, "An account with this email already exists")
                ck = self.start_session(c, uid)
                return self.send_json({"user": public_user(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())}, 201, [ck])
            if p == "/api/auth/login":
                email, pw = str(d.get("email", "")).strip().lower(), str(d.get("password") or "")[:128]
                key = "%s|%s" % (self.client_address[0], email)
                if throttled(key): raise ApiError(429, "Too many attempts. Wait a few minutes and try again.")
                u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                if not check_pw(pw, u["password_hash"] if u else DUMMY_HASH) or not u:
                    note_fail(key); raise ApiError(401, "This account uses Google sign-in. Use “Continue with Google”." if u and u["password_hash"] == "!" else "Wrong email or password")
                return self.send_json({"user": public_user(u)}, 200, [self.start_session(c, u["id"])])
            if p == "/api/auth/logout":
                try: tok = http.cookies.SimpleCookie(self.headers.get("Cookie", "")).get(SESSION_COOKIE)
                except http.cookies.CookieError: tok = None
                if tok: c.execute("DELETE FROM sessions WHERE token_hash=?", (sha(tok.value),))
                return self.send_json({"ok": True}, 200, [self.cookie("", 0)])
            if p == "/api/orders": return self.create_order(c, self.need_user(c), d)
            if p.startswith("/api/account/"): return self.account_post(c, self.need_user(c), p, d)
            if p == "/api/orders/proof": return self.submit_proof(c, self.need_user(c), d)
            if p in ("/api/admin/orders/confirm", "/api/admin/orders/reject"): return self.admin_decide(c, d, p.endswith("confirm"))
            if p == "/api/payments/mock/confirm" and ONLINE == "mock":
                me = self.need_user(c)
                o = c.execute("SELECT * FROM orders WHERE reference=? AND user_id=?", (str(d.get("reference", "")), me["id"])).fetchone()
                if not o or o["payment_method"] != "card": raise ApiError(404, "Order not found")
                ok = d.get("result") == "success"
                st = apply_txn(c, o, {"provider": "mock", "txn_id": "mock-" + secrets.token_hex(6), "success": ok, "pending": False, "voided": False, "refunded": False,
                                      "amount_cents": round(o["total"] * 100), "currency": o["currency"] or CURRENCY, "raw": {"mock": True}})
                return self.send_json({"ok": True, "payment_status": st})
        finally: c.close()
        raise ApiError(404, "Not found")

    def create_order(self, c, me, d):
        name, phone = clean(d.get("name"), 2, 80, "Full name"), re.sub(r"[\s-]", "", str(d.get("phone") or ""))
        address, city, notes = clean(d.get("address"), 5, 200, "Address"), clean(d.get("city"), 2, 60, "City"), clean(d.get("notes"), 0, 300, "Notes")
        if not PHONE_RE.fullmatch(phone): raise ApiError(400, "Valid phone number required")
        method = d.get("payment_method"); kind = method_kind(method)
        if not kind: raise ApiError(400, "Choose a payment method")
        want = {}
        for it in (d.get("items") or [])[:50]:
            try: vid, q = int(it["id"]), int(it.get("quantity", 1))
            except (KeyError, TypeError, ValueError): raise ApiError(400, "Invalid item")
            if not 1 <= q <= 20: raise ApiError(400, "Invalid quantity")
            want[vid] = min(20, want.get(vid, 0) + q)
        if not want: raise ApiError(400, "Your bag is empty")
        ref = "AV" + secrets.token_hex(4).upper(); lines = []
        with tx(c):
            sweep_expired(c)
            for vid, q in want.items():
                r = c.execute("SELECT v.stock,v.size,p.name,p.price FROM variants v JOIN products p ON p.id=v.product_id WHERE v.id=? AND p.active=1", (vid,)).fetchone()
                if not r: raise ApiError(400, "An item in your bag is no longer available")
                if r["stock"] < q: raise ApiError(409, "Only %d left of %s (size %s)" % (r["stock"], r["name"], r["size"]) if r["stock"] else "%s (size %s) is sold out" % (r["name"], r["size"]))
                c.execute("UPDATE variants SET stock=stock-? WHERE id=?", (q, vid)); lines.append((vid, q, r["price"], r["name"]))
            cents = sum(round(p * 100) * q for _, q, p, _ in lines) + round(SHIPPING_FEE * 100)
            oid = c.execute("INSERT INTO orders(email,total,status,user_id,reference,ship_name,ship_phone,ship_address,ship_city,notes,payment_method,currency,shipping) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (me["email"], cents / 100, "confirmed" if kind == "cod" else "pending_payment", me["id"], ref, name, phone, address, city, notes, method, CURRENCY, SHIPPING_FEE)).lastrowid
            c.executemany("INSERT INTO order_items(order_id,variant_id,quantity,unit_price) VALUES(?,?,?,?)", [(oid, v, q, p) for v, q, p, _ in lines])
        out = {"ok": True, "reference": ref, "total": cents / 100, "currency": CURRENCY}
        out["payment_method"], out["kind"] = method, kind
        if kind == "online":
            o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            try:
                if ONLINE == "paymob":
                    pref, out["payment_url"] = paymob_checkout_url(o, [(n, p, q) for _, q, p, n in lines], me, PAYMOB_WALLET_ID if method == "vodafone_cash" else None)
                    c.execute("UPDATE orders SET provider_ref=? WHERE id=?", (pref, oid))
                else: out["payment_url"] = "/pay/mock?ref=" + ref
            except ApiError:
                with tx(c): cancel_order(c, oid)                 # give the stock back, the customer can retry
                raise
        return self.send_json(out, 201)

    # --- profile page: name/phone, password, saved addresses
    def account_post(self, c, me, p, d):
        if p == "/api/account/profile":
            name, phone = clean(d.get("name"), 2, 80, "Name"), re.sub(r"[\s-]", "", str(d.get("phone") or ""))
            if phone and not PHONE_RE.fullmatch(phone): raise ApiError(400, "Phone number looks wrong")
            c.execute("UPDATE users SET name=?,phone=? WHERE id=?", (name, phone or None, me["id"]))
            return self.send_json({"user": public_user(c.execute("SELECT * FROM users WHERE id=?", (me["id"],)).fetchone())})
        if p == "/api/account/password":
            cur, new = str(d.get("current") or "")[:128], str(d.get("new") or "")
            if not 8 <= len(new) <= 128: raise ApiError(400, "New password must be 8–128 characters")
            if me["password_hash"] != "!":                                   # Google-only accounts have no password yet, so they may simply set one
                key = "pw|%d" % me["id"]
                if throttled(key): raise ApiError(429, "Too many attempts. Wait a few minutes and try again.")
                if not check_pw(cur, me["password_hash"]): note_fail(key); raise ApiError(403, "Current password is wrong")
            c.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_pw(new), me["id"]))
            tok = self.get_cookie(SESSION_COOKIE)                           # sign out every other device
            c.execute("DELETE FROM sessions WHERE user_id=? AND token_hash!=?", (me["id"], sha(tok or "")))
            return self.send_json({"ok": True})
        if p == "/api/account/addresses/save":
            name, phone = clean(d.get("name"), 2, 80, "Full name"), re.sub(r"[\s-]", "", str(d.get("phone") or ""))
            address, city, label = clean(d.get("address"), 5, 200, "Address"), clean(d.get("city"), 2, 60, "City"), clean(d.get("label"), 0, 30, "Label")
            if not PHONE_RE.fullmatch(phone): raise ApiError(400, "Valid phone number required")
            with tx(c):
                aid = d.get("id")
                if aid:
                    if not c.execute("SELECT 1 FROM addresses WHERE id=? AND user_id=?", (aid, me["id"])).fetchone(): raise ApiError(404, "Address not found")
                    c.execute("UPDATE addresses SET label=?,name=?,phone=?,address=?,city=? WHERE id=?", (label, name, phone, address, city, aid))
                else:
                    if c.execute("SELECT COUNT(*) FROM addresses WHERE user_id=?", (me["id"],)).fetchone()[0] >= 10: raise ApiError(400, "You can save up to 10 addresses")
                    first = not c.execute("SELECT 1 FROM addresses WHERE user_id=?", (me["id"],)).fetchone()
                    aid = c.execute("INSERT INTO addresses(user_id,label,name,phone,address,city,is_default) VALUES(?,?,?,?,?,?,?)", (me["id"], label, name, phone, address, city, int(first))).lastrowid
                if d.get("is_default"):
                    c.execute("UPDATE addresses SET is_default=(id=?) WHERE user_id=?", (aid, me["id"]))
            return self.send_json({"ok": True, "id": aid})
        if p == "/api/account/addresses/delete":
            with tx(c):
                c.execute("DELETE FROM addresses WHERE id=? AND user_id=?", (d.get("id"), me["id"]))
                if not c.execute("SELECT 1 FROM addresses WHERE user_id=? AND is_default=1", (me["id"],)).fetchone():
                    c.execute("UPDATE addresses SET is_default=1 WHERE id=(SELECT MIN(id) FROM addresses WHERE user_id=?)", (me["id"],))
            return self.send_json({"ok": True})
        raise ApiError(404, "Not found")

    # --- manual payments (Vodafone Cash / InstaPay transfer): the customer sends the money, submits the transfer reference, you confirm it in /admin.html
    def submit_proof(self, c, me, d):
        o = c.execute("SELECT * FROM orders WHERE reference=? AND user_id=?", (str(d.get("reference", "")), me["id"])).fetchone()
        if not o or method_kind(o["payment_method"]) != "manual": raise ApiError(404, "Order not found")
        if o["status"] != "pending_payment": raise ApiError(409, "This order is no longer waiting for payment")
        if o["payment_status"] == "verifying": raise ApiError(409, "We already have your transfer details and are checking them")
        sender, txn = clean(d.get("sender"), 3, 60, "Your sending number or InstaPay address"), clean(d.get("txn"), 4, 40, "Transfer reference").upper()
        if not re.fullmatch(r"[A-Z0-9\-_/.]{4,40}", txn): raise ApiError(400, "Transfer reference may contain only letters, numbers and - _ / .")
        prov = o["payment_method"] + "_manual"
        if c.execute("SELECT 1 FROM payments WHERE provider_txn_id=? AND provider LIKE '%\\_manual' ESCAPE '\\'", (txn,)).fetchone(): raise ApiError(409, "This transfer reference was already used for another order")
        try:
            with tx(c):
                c.execute("INSERT INTO payments(order_id,provider,provider_txn_id,amount_cents,currency,status,raw) VALUES(?,?,?,?,?,?,?)",
                          (o["id"], prov, txn, round(o["total"] * 100), o["currency"] or CURRENCY, "verifying", json.dumps({"sender": sender})))
                c.execute("UPDATE orders SET payment_status='verifying',updated_at=CURRENT_TIMESTAMP WHERE id=?", (o["id"],))
        except sqlite3.IntegrityError: raise ApiError(409, "This transfer reference was already used for another order")
        return self.send_json({"ok": True, "payment_status": "verifying"})

    def need_admin(self):
        if not ADMIN_TOKEN: raise ApiError(404, "Not found")
        key = "admin|" + self.client_address[0]
        if throttled(key): raise ApiError(429, "Too many attempts. Wait a few minutes.")
        if not hmac.compare_digest((self.headers.get("X-Admin-Token") or "").encode(), ADMIN_TOKEN.encode()): note_fail(key); raise ApiError(401, "Wrong admin token")

    def admin_orders(self, c):
        self.need_admin()
        rows = c.execute("SELECT * FROM orders WHERE payment_status='verifying' OR (status='pending_payment' AND payment_method IN ('vodafone_cash','instapay')) ORDER BY id DESC LIMIT 100").fetchall()
        out = []
        for o in rows:
            v = order_view(c, o); v.update(email=o["email"], name=o["ship_name"], phone=o["ship_phone"], address=o["ship_address"], city=o["ship_city"], notes=o["notes"],
                payments=[{"provider": r["provider"], "txn": r["provider_txn_id"], "status": r["status"], "raw": r["raw"]} for r in c.execute("SELECT * FROM payments WHERE order_id=?", (o["id"],))])
            out.append(v)
        return self.send_json(out)

    def admin_decide(self, c, d, ok):
        self.need_admin()
        o = c.execute("SELECT * FROM orders WHERE reference=?", (str(d.get("reference", "")),)).fetchone()
        if not o or method_kind(o["payment_method"]) != "manual": raise ApiError(404, "Order not found")
        if o["status"] != "pending_payment": raise ApiError(409, "Order is already %s" % o["status"])
        with tx(c):
            if ok:
                c.execute("UPDATE payments SET status='paid' WHERE order_id=? AND status='verifying'", (o["id"],))
                c.execute("UPDATE orders SET payment_status='paid',status='confirmed',updated_at=CURRENT_TIMESTAMP WHERE id=?", (o["id"],))
            else:
                c.execute("UPDATE payments SET status='rejected' WHERE order_id=? AND status='verifying'", (o["id"],))
                cancel_order(c, o["id"]); c.execute("UPDATE orders SET payment_status='failed' WHERE id=?", (o["id"],))
        return self.send_json({"ok": True})

    # --- Google sign-in (authorization-code flow: Google's answer is fetched server-to-server over TLS, the browser only carries a one-time code)
    def get_cookie(self, name):
        try: m = http.cookies.SimpleCookie(self.headers.get("Cookie", "")).get(name)
        except http.cookies.CookieError: return None
        return m.value if m else None

    def google_start(self):
        if not GOOGLE_ON: raise ApiError(404, "Google sign-in is not set up")
        state = secrets.token_urlsafe(24)
        q = urlencode({"client_id": GOOGLE["client_id"], "redirect_uri": PUBLIC_URL + "/api/auth/google/callback", "response_type": "code", "scope": "openid email profile",
                       "state": state, "prompt": "select_account"})
        ck = "avenro_oauth=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=600%s" % (state, "; Secure" if COOKIE_SECURE else "")
        return self.redirect(GOOGLE_AUTH_URL + "?" + q, [("Set-Cookie", ck)])

    def google_callback(self, c, q):
        if not GOOGLE_ON: raise ApiError(404, "Google sign-in is not set up")
        clear = ("Set-Cookie", "avenro_oauth=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        state, code, saved = (q.get("state") or [""])[0], (q.get("code") or [""])[0], self.get_cookie("avenro_oauth") or ""
        if not code or not state or not hmac.compare_digest(state, saved): return self.redirect("/?auth_error=google", [clear])
        try:
            tok = http_json(GOOGLE_TOKEN_URL, form={"code": code, "client_id": GOOGLE["client_id"], "client_secret": GOOGLE["client_secret"],
                                                    "redirect_uri": PUBLIC_URL + "/api/auth/google/callback", "grant_type": "authorization_code"})
            info = http_json(GOOGLE_USERINFO_URL, headers={"Authorization": "Bearer " + tok["access_token"]})
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
            print("!! Google sign-in failed:", e); return self.redirect("/?auth_error=google", [clear])
        sub, email = str(info.get("sub") or ""), str(info.get("email") or "").strip().lower()
        if not sub or not EMAIL_RE.fullmatch(email) or len(email) > 254 or info.get("email_verified") not in (True, "true"): return self.redirect("/?auth_error=google", [clear])
        with tx(c):
            u = c.execute("SELECT u.* FROM identities i JOIN users u ON u.id=i.user_id WHERE i.provider='google' AND i.subject=?", (sub,)).fetchone()
            if not u:
                u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()        # Google verified this address, so it is safe to link to an existing account
                if not u:
                    name = re.sub(r"\s+", " ", str(info.get("name") or "")).strip()[:80]
                    uid = c.execute("INSERT INTO users(email,name,phone,password_hash) VALUES(?,?,?,?)", (email, name if len(name) >= 2 else email.split("@")[0][:80], None, "!")).lastrowid
                    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
                c.execute("INSERT INTO identities(provider,subject,user_id) VALUES('google',?,?)", (sub, u["id"]))
        return self.redirect("/?signed_in=google", [self.start_session(c, u["id"]), clear])

    def paymob_webhook(self):
        if ONLINE != "paymob": raise ApiError(404, "Not found")
        try: payload = json.loads(self.read_body(200000) or b"{}")
        except ValueError: raise ApiError(400, "Bad request")
        if not isinstance(payload, dict) or not isinstance(payload.get("obj"), dict): return self.send_json({"ok": True})   # e.g. token callbacks: nothing to do
        flat, raw, mref, pid = paymob_from_post(payload)
        if not paymob_check(flat, (parse_qs(urlparse(self.path).query).get("hmac") or [""])[0]): raise ApiError(401, "Bad signature")
        c = conn()
        try:
            o = find_order(c, mref, pid)
            if o: apply_txn(c, o, paymob_txn(flat, raw))
            else: print("!! webhook for unknown order", mref, pid)
        finally: c.close()
        self.send_json({"ok": True})                             # always 200 once the signature is valid, otherwise Paymob keeps retrying

    # --- static files
    def static(self, base, rel):
        f = os.path.realpath(os.path.join(base, rel))
        if not f.startswith(os.path.realpath(base) + os.sep) or not os.path.isfile(f):
            return self.send_json({"error": "Not found"}, 404)
        size, rng, start, code = os.path.getsize(f), self.headers.get("Range"), 0, 200
        end = size - 1
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip()) if rng else None
        if m:
            a, b = m.groups()
            if a == "" and b: start = max(0, size - int(b))
            elif a: start = int(a); end = min(int(b), size - 1) if b else size - 1
            if start > end: self.send_response(416); self.send_header("Content-Range", f"bytes */{size}"); self.end_headers(); return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", MIME.get(os.path.splitext(f)[1].lower(), "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes"); self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-cache" if f.endswith(".html") else "public, max-age=3600")
        if code == 206: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(f, "rb") as fh:
            fh.seek(start); left = end - start + 1
            while left > 0:
                chunk = fh.read(min(65536, left))
                if not chunk: break
                try: self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError): break
                left -= len(chunk)
    def log_message(self, *a): pass

if __name__ == "__main__":
    init_db("--reset-db" in sys.argv)
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else int(env("PORT", "8000"))
    srv = None
    for p in range(port, port + 20):                     # use the next free port if 8000 is busy
        try: srv = ThreadingHTTPServer((HOST, p), H); break
        except OSError: continue
    if not srv: sys.exit("No free port found between %d and %d" % (port, port + 19))
    url = "http://localhost:%d" % srv.server_address[1]
    if not PUBLIC_URL: PUBLIC_URL = url
    PUBLIC_URL = PUBLIC_URL.rstrip("/")
    print("Avenro is running at " + url + "   (press Ctrl+C to stop)")
    print("Online payments: %s | cash on delivery: %s | currency: %s" % (ONLINE.upper() + (" (TEST PAGE, no real payments)" if ONLINE == "mock" else ""), "on" if ENABLE_COD else "off", CURRENCY))
    print("Methods: " + ", ".join("%s (%s)" % (m["label"], m["kind"]) for m in methods_cfg()) + " | Google sign-in: " + ("on" if GOOGLE_ON else "off (set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET)") + " | admin page: " + ("on" if ADMIN_TOKEN else "off (set ADMIN_TOKEN)"))
    if ONLINE == "paymob" and PUBLIC_URL.startswith("http://localhost"): print("Note: Paymob cannot reach localhost for webhooks. Set PUBLIC_URL (e.g. an ngrok address) in .env to receive them.")
    if "--open" in sys.argv: webbrowser.open(url)
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\nStopped.")
