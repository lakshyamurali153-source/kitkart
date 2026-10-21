"""KitKart backend: Flask + SQLite. Run: python app.py  ->  http://127.0.0.1:5000"""
import os, re, json, math, base64, secrets, sqlite3
import urllib.request, urllib.error, hmac, hashlib, io
from urllib.parse import quote
from functools import wraps
import segno
from flask import Flask, request, jsonify, g, send_from_directory, session

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB photo limit
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")      # signs login cookies and QR codes
OWNER_PIN = os.environ.get("OWNER_PIN", "1234")                            # staff login PIN (demo)
DEVICE_KEY = os.environ.get("DEVICE_KEY", "")                              # lets an ESP32 post sensor data
RZP_ID = (os.environ.get("RAZORPAY_KEY_ID") or "").strip() or None          # Razorpay TEST keys (rzp_test_...)
RZP_SECRET = (os.environ.get("RAZORPAY_KEY_SECRET") or "").strip() or None
UPI_ID = (os.environ.get("UPI_ID") or "").strip() or None   # direct UPI: YOUR UPI ID (e.g. name@oksbi). Money goes straight to it, no gateway.
UPI_NAME = os.environ.get("UPI_NAME", "KitKart")
if os.environ.get("PAYMENT_MODE") == "upi": RZP_ID = RZP_SECRET = None  # PAYMENT_MODE=upi forces direct UPI even if Razorpay keys exist
if os.environ.get("PAYMENT_MODE") == "demo": RZP_ID = RZP_SECRET = UPI_ID = None
# DEFAULT = built-in FAKE gateway (like a sandbox API): no signup, no real money. Used when no Razorpay keys / UPI_ID are set.
MOCK = os.environ.get("PAYMENT_MODE", "") not in ("demo", "upi") and not (RZP_ID and RZP_SECRET) and not UPI_ID
MOCK_SECRET = hashlib.sha256((app.secret_key + "mock-gateway").encode()).hexdigest()   # PAYMENT_MODE=demo forces the fake payment (backup for demo day)
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kitkart.db")
DEFAULT_LOC = (11.9416, 79.8083)  # Puducherry; the shopper's browser can send its own

# ---------- sample data (id, name, unit, price, grams per piece or None) ----------
ITEMS = [
    ("washer", "Rubber tap washer", "pc", 5, None), ("ptfe", "PTFE thread tape", "pc", 20, None),
    ("cart", "Tap cartridge", "pc", 180, None), ("sw6", "6A switch", "pc", 35, None),
    ("sock", "6A socket", "pc", 45, None), ("mcb", "6A MCB", "pc", 140, None),
    ("wire", "1.5 mm wire", "m", 22, None), ("screw", "1 inch screws", "pc", 1, 2),
    ("plug", "Wall plug", "pc", 2, None), ("paint", "White wall paint 1 L", "pc", 310, None),
    ("brush", "Paint brush 2 inch", "pc", 60, None), ("sand", "Sandpaper sheet", "pc", 15, None),
    ("shelf", "Shelf bracket", "pc", 75, None)]
SHOPS = [("s1", "Sri Murugan Hardware", 11.9390, 79.8100), ("s2", "Annai Electricals", 11.9470, 79.8130),
         ("s3", "Kumar Paints & Hardware", 11.9550, 79.8000)]
START = {
    "s1": dict(washer=40, ptfe=12, cart=0, sw6=10, sock=6, mcb=4, wire=60, screw=500, plug=80, paint=3, brush=6, sand=30, shelf=0),
    "s2": dict(washer=0, ptfe=5, cart=6, sw6=30, sock=20, mcb=12, wire=200, screw=300, plug=0, paint=0, brush=0, sand=0, shelf=4),
    "s3": dict(washer=25, ptfe=8, cart=3, sw6=4, sock=0, mcb=0, wire=30, screw=800, plug=120, paint=10, brush=12, sand=50, shelf=10)}
JOBS = {"tap": ("Fix a leaking tap", {"washer": 2, "ptfe": 1, "cart": 1}),
        "sw": ("Replace a switch", {"sw6": 1, "wire": 1, "screw": 4}),
        "point": ("Add a power socket point", {"sock": 1, "mcb": 1, "wire": 5, "screw": 6}),
        "paint": ("Paint a small wall", {"paint": 2, "brush": 1, "sand": 3}),
        "shelf": ("Hang a shelf", {"shelf": 2, "screw": 8, "plug": 8})}
AL = {"washer": ["washer"], "ptfe": ["ptfe", "thread tape", "tape"], "cart": ["cartridge"], "sw6": ["switch"],
      "sock": ["socket"], "mcb": ["mcb", "breaker"], "wire": ["wire", "cable"], "screw": ["screw"], "plug": ["plug"],
      "paint": ["paint"], "brush": ["brush"], "sand": ["sandpaper", "sand paper"], "shelf": ["bracket", "shelf"]}
JR = {"tap": r"tap|leak", "sw": r"(replace|change|fix).*switch", "point": r"(socket|power).*point|add.*socket",
      "paint": r"paint(?!.*brush)", "shelf": r"hang|shelf(?!.*bracket)"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, name TEXT, unit TEXT, price REAL, piece_w REAL);
CREATE TABLE IF NOT EXISTS shops(id TEXT PRIMARY KEY, name TEXT, lat REAL, lng REAL);
CREATE TABLE IF NOT EXISTS stock(shop_id TEXT, item_id TEXT, actual REAL, expected REAL, PRIMARY KEY(shop_id,item_id));
CREATE TABLE IF NOT EXISTS orders(token TEXT PRIMARY KEY, shop_id TEXT, title TEXT, kit TEXT, status TEXT DEFAULT 'pending', created TEXT DEFAULT CURRENT_TIMESTAMP, rzp TEXT);
CREATE TABLE IF NOT EXISTS sales(id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id TEXT, item_id TEXT, qty REAL, amount REAL, ts TEXT DEFAULT CURRENT_TIMESTAMP);
"""


def init_db(reset=False):
    c = sqlite3.connect(DB)
    if reset:
        c.executescript("DROP TABLE IF EXISTS items;DROP TABLE IF EXISTS shops;DROP TABLE IF EXISTS stock;DROP TABLE IF EXISTS orders;DROP TABLE IF EXISTS sales;")
    c.executescript(SCHEMA)
    for col in ("rzp", "done_at", "pay_id", "ret", "ret_reason"):  # add new columns to an older kitkart.db too
        try: c.execute(f"ALTER TABLE orders ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError: pass
    if not c.execute("SELECT 1 FROM shops").fetchone():
        c.executemany("INSERT INTO items VALUES(?,?,?,?,?)", ITEMS)
        c.executemany("INSERT INTO shops VALUES(?,?,?,?)", SHOPS)
        for sid, st in START.items():
            c.executemany("INSERT INTO stock VALUES(?,?,?,?)", [(sid, k, v, v) for k, v in st.items()])
    c.commit(); c.close()


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d: d.close()


def items_map():
    return {r["id"]: r for r in db().execute("SELECT * FROM items")}


def clean_kit(kit):
    """Keep only real catalogue ids with sensible quantities (never trust the client or the AI)."""
    out, im = {}, items_map()
    for k, q in (kit or {}).items():
        try: q = float(q)
        except (TypeError, ValueError): continue
        if k in im and 0 < q <= 100: out[k] = int(q) if q == int(q) else q
    return out


def haversine(a, b, c, d):
    p = math.pi / 180
    x = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(x))


# ---------- understanding typed text ----------
def parse(t):
    out = {}
    for p in re.split(r",|;|\n|\band\b", t):
        best, bl = None, 0
        for k, words in AL.items():
            for w in words:
                if w in p and len(w) >= bl: best, bl = k, len(w)
        if best:
            m = re.search(r"\d+(\.\d+)?", p)
            out[best] = out.get(best, 0) + (float(m.group()) if m else 1)
    return out


def understand(text):
    t = (text or "").lower().strip()
    if not t: return None, None
    items = parse(t)
    jm = next((k for k, r in JR.items() if re.search(r, t)), None)
    if items and (re.search(r"\d", t) or not jm): return "Your list", items
    if jm: return JOBS[jm][0], dict(JOBS[jm][1])
    return None, None


def respond(title, kit, loc):
    """Build the answer: the kit, and shops ranked (full kit first, then nearest)."""
    kit = clean_kit(kit); im = items_map()
    total = sum(im[k]["price"] * q for k, q in kit.items())
    shops = []
    for s in db().execute("SELECT * FROM shops"):
        st = {r["item_id"]: r["actual"] for r in db().execute("SELECT item_id, actual FROM stock WHERE shop_id=?", (s["id"],))}
        miss = [im[k]["name"] for k, q in kit.items() if st.get(k, 0) < q]
        shops.append(dict(id=s["id"], name=s["name"], km=round(haversine(loc[0], loc[1], s["lat"], s["lng"]), 1),
                          missing=miss, complete=not miss, total=round(total)))
    shops.sort(key=lambda x: (not x["complete"], x["km"]))
    return dict(title=title, kit=[dict(id=k, name=im[k]["name"], unit=im[k]["unit"], qty=q) for k, q in kit.items()], shops=shops)


def loc_of(d):
    try: return float(d.get("lat")), float(d.get("lng"))
    except (TypeError, ValueError): return DEFAULT_LOC


# ---------- shopper endpoints ----------
@app.get("/")
def home():
    return send_from_directory("static", "index (1).html")


@app.get("/api/items")
def api_items():
    return jsonify([dict(id=r["id"], name=r["name"], unit=r["unit"], by_weight=r["piece_w"] is not None) for r in db().execute("SELECT * FROM items")])


@app.get("/api/shops")
def api_shops():
    return jsonify([dict(id=r["id"], name=r["name"]) for r in db().execute("SELECT * FROM shops")])


@app.get("/api/jobs")
def api_jobs():
    return jsonify([v[0] for v in JOBS.values()])


@app.post("/api/search")
def api_search():
    d = request.get_json(force=True, silent=True) or {}
    title, kit = understand(d.get("text"))
    if not kit:
        return jsonify(error="Sorry, we did not understand. Try 'fix a leaking tap' or '2 washers, 5 m wire'.")
    return jsonify(respond(title, kit, loc_of(d)))


def http_json(url, payload, headers=None, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def http_get(url, headers=None, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=timeout) as r:
        return json.loads(r.read())


_gem_model = None


def gemini_model(key, force=False):
    """Use GEMINI_MODEL if you set it. Otherwise ask Google which models this key can use and pick the newest normal Flash model.
    (Google renames and retires models often, so a fixed name can suddenly give HTTP 404.)"""
    global _gem_model
    if os.environ.get("GEMINI_MODEL"): return os.environ["GEMINI_MODEL"]
    if _gem_model and not force: return _gem_model
    best = None
    for m in http_get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200", {"x-goog-api-key": key}).get("models", []):
        n = m.get("name", "").split("/")[-1]
        if "generateContent" not in m.get("supportedGenerationMethods", []) or not n.startswith("gemini-") or "flash" not in n: continue
        if any(x in n for x in ("lite", "image", "tts", "live", "audio", "thinking", "embedding", "robotics", "computer", "omni", "8b", "exp", "veo")): continue
        ver = tuple(int(x) for x in re.findall(r"\d+", n.split("flash")[0]))
        rank = (ver, "preview" not in n)  # newest version first, stable beats preview
        if best is None or rank > best[0]: best = (rank, n)
    if not best: raise RuntimeError("No usable Gemini Flash model found for this API key.")
    _gem_model = best[1]; return _gem_model


def ask_vision(prompt, img, mime):
    """Ask a vision model about the photo and return its text answer.
    AI_PROVIDER = gemini (free tier, default) | ollama (free, runs on your laptop) | anthropic (paid)"""
    p = os.environ.get("AI_PROVIDER", "gemini").lower()
    b64 = base64.b64encode(img).decode()
    if p == "gemini":
        key = os.environ.get("GEMINI_API_KEY")
        if not key: raise RuntimeError("Photo AI is not set up (missing GEMINI_API_KEY).")
        body = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": mime, "data": b64}}]}],
                "generationConfig": {"responseMimeType": "application/json"}}
        url = lambda m: f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
        try:
            r = http_json(url(gemini_model(key)), body, {"x-goog-api-key": key})
        except urllib.error.HTTPError as e:
            if e.code != 404 or os.environ.get("GEMINI_MODEL"): raise
            r = http_json(url(gemini_model(key, force=True)), body, {"x-goog-api-key": key})  # model was retired: pick again
        return r["candidates"][0]["content"]["parts"][0]["text"]

@app.post("/api/photo")
def api_photo():
    f = request.files.get("image")
    if not f: return jsonify(error="No photo received.")
    mt = f.mimetype if f.mimetype in ("image/jpeg", "image/png", "image/webp", "image/gif") else "image/jpeg"
    cat = "; ".join(f"{k} = {r['name']} ({'metres' if r['unit'] == 'm' else 'pieces'})" for k, r in items_map().items())
    hint = (request.form.get("text") or "")[:200]
    prompt = ("You help a hardware shop app. The photo shows a broken or worn household hardware item (tap, switch, socket, wall, shelf and so on). "
              "Work out what is wrong and which parts are needed to fix it. Use ONLY these catalogue ids: " + cat + ". "
              'Reply with only JSON: {"what":"short name of the problem","items":{"id":qty},"confidence":0.0 to 1.0,"note":"one short sentence for the shopper"}. '
              'If the photo is not hardware or you cannot tell, reply {"what":"unknown","items":{},"confidence":0,"note":"why"}.'
              + (f" The shopper also wrote: {hint}" if hint else ""))
    try:
        r = json.loads(re.search(r"\{.*\}", ask_vision(prompt, f.read(), mt), re.S).group())
        kit = clean_kit(r.get("items"))
        if not kit or float(r.get("confidence") or 0) < 0.3:
            return jsonify(error="AI could not tell what part this is. " + str(r.get("note", ""))[:160] + " Please type what you need.")
        out = respond("AI: " + str(r.get("what", "photo"))[:60], kit, loc_of(request.form))
        out["note"] = f"{str(r.get('note', ''))[:160]} (confidence {round(float(r.get('confidence') or 0) * 100)}%)"
        return jsonify(out)
    except RuntimeError as e:
        return jsonify(error=f"{e} Please type what you need.")
    except urllib.error.HTTPError as e:
        try: detail = json.loads(e.read()).get("error", {}).get("message", "")[:140]
        except Exception: detail = ""
        why = "free limit reached, wait a minute" if e.code == 429 else f"HTTP {e.code}: {(detail.rstrip(".") or "check the API key")}"
        return jsonify(error=f"Photo AI problem: {why}. Or type what you need.")
    except urllib.error.URLError:
        return jsonify(error="Cannot reach the AI. If you use Ollama, is it running? Or type what you need.")
    except Exception as e:  # bad JSON from the model, etc.
        return jsonify(error=f"Photo analysis failed ({type(e).__name__}). Try again or type what you need.")


def sign(token):
    return hmac.new(app.secret_key.encode(), token.encode(), hashlib.sha256).hexdigest()[:10].upper()


def qr_data(text):
    buf = io.BytesIO(); segno.make(text, error="m").save(buf, kind="svg", scale=6, border=2)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


def qr_for(token):
    payload = f"KK-{token}-{sign(token)}"
    buf = io.BytesIO(); segno.make(payload, error="m").save(buf, kind="svg", scale=6, border=2)
    return payload, "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


def token_from(text):
    """Accepts the scanned QR text (signature is checked) or the plain 6-character token."""
    t = (text or "").strip().upper()
    m = re.fullmatch(r"KK-([0-9A-F]{6})-([0-9A-F]{10})", t)
    if m: return m.group(1) if hmac.compare_digest(m.group(2), sign(m.group(1))) else None
    return t if re.fullmatch(r"[0-9A-F]{6}", t) else None


def staff(f):
    """Counter / owner endpoints: need the staff login, or the device key (for an ESP32)."""
    @wraps(f)
    def w(*a, **k):
        if session.get("staff") or (DEVICE_KEY and hmac.compare_digest(request.headers.get("X-Device-Key", ""), DEVICE_KEY)):
            return f(*a, **k)
        return jsonify(error="Please login first"), 401
    return w


@app.post("/api/login")
def api_login():
    d = request.get_json(force=True, silent=True) or {}
    if hmac.compare_digest(str(d.get("pin", "")), OWNER_PIN):
        session["staff"] = True; return jsonify(ok=True)
    return jsonify(error="Wrong PIN")


@app.get("/api/me")
def api_me():
    return jsonify(staff=bool(session.get("staff")))


@app.post("/api/order")
def api_order():
    """Step 1 of payment: save a PENDING order. It becomes 'paid' only after payment is confirmed."""
    d = request.get_json(force=True, silent=True) or {}
    kit = clean_kit(d.get("kit")); shop = d.get("shop_id"); im = items_map()
    if not kit or not db().execute("SELECT 1 FROM shops WHERE id=?", (shop,)).fetchone():
        return jsonify(error="Bad order.")
    for k, q in kit.items():
        r = db().execute("SELECT actual FROM stock WHERE shop_id=? AND item_id=?", (shop, k)).fetchone()
        if not r or r["actual"] < q: return jsonify(error="Sorry, stock just changed. Search again.")
    token = secrets.token_hex(3).upper()
    amount = round(sum(im[k]["price"] * q for k, q in kit.items()) * 100)  # paise
    rzp = None
    if RZP_ID and RZP_SECRET:
        try:
            auth = base64.b64encode(f"{RZP_ID}:{RZP_SECRET}".encode()).decode()
            r = http_json("https://api.razorpay.com/v1/orders", {"amount": amount, "currency": "INR", "receipt": token}, {"Authorization": "Basic " + auth})
            rzp = {"key_id": RZP_ID, "order_id": r["id"], "amount": amount}
        except urllib.error.HTTPError as e:
            try: why = json.loads(e.read())["error"]["description"]
            except Exception: why = f"HTTP {e.code}"
            print("Razorpay order failed:", e.code, why)
            return jsonify(error=f"Razorpay said: {why}. Check RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET (test keys start with rzp_test_).")
        except Exception as e:
            print("Razorpay order failed:", repr(e))
            return jsonify(error=f"Could not reach Razorpay ({type(e).__name__}). Check your internet connection.")
    if not rzp and MOCK:
        rzp = {"mock": True, "key_id": "mock", "order_id": "order_mock_" + token, "amount": amount}
    db().execute("INSERT INTO orders(token,shop_id,title,kit,rzp) VALUES(?,?,?,?,?)",
                 (token, shop, str(d.get("title", ""))[:80], json.dumps(kit), rzp["order_id"] if rzp else None))
    db().commit()
    upi = None
    if not rzp and UPI_ID:  # direct UPI: a payment link + QR that any UPI app understands
        rs = amount / 100
        link = f"upi://pay?pa={quote(UPI_ID, safe='@')}&pn={quote(UPI_NAME)}&am={rs:.2f}&cu=INR&tn={quote('KitKart ' + token)}"
        upi = dict(link=link, qr=qr_data(link), amount=rs, payee=UPI_ID)
    return jsonify(token=token, razorpay=rzp, upi=upi, amount=amount // 100)


@app.post("/api/reserve")
def api_reserve():
    """Pay-at-shop: hold the kit now, check that it fits, and pay at the counter."""
    d = request.get_json(force=True, silent=True) or {}
    kit = clean_kit(d.get("kit")); shop = d.get("shop_id")
    if not kit or not db().execute("SELECT 1 FROM shops WHERE id=?", (shop,)).fetchone(): return jsonify(error="Bad order.")
    for k, q in kit.items():
        r = db().execute("SELECT actual FROM stock WHERE shop_id=? AND item_id=?", (shop, k)).fetchone()
        if not r or r["actual"] < q: return jsonify(error="Sorry, stock just changed. Search again.")
    token = secrets.token_hex(3).upper()
    db().execute("INSERT INTO orders(token,shop_id,title,kit,status) VALUES(?,?,?,?,'reserved')", (token, shop, str(d.get("title", ""))[:80], json.dumps(kit)))
    db().commit(); payload, img = qr_for(token)
    return jsonify(token=token, qr=img, payload=payload)


def mark_paid(token, msg):
    db().execute("UPDATE orders SET status='paid' WHERE token=? AND status='pending'", (token,)); db().commit()
    payload, img = qr_for(token)
    return jsonify(token=token, paid=msg, qr=img, payload=payload)


@app.post("/api/pay/demo")
def pay_demo():
    d = request.get_json(force=True, silent=True) or {}
    if RZP_ID or UPI_ID: return jsonify(error="Demo payment is off. Use the real payment.")
    if not db().execute("SELECT 1 FROM orders WHERE token=?", (d.get("token"),)).fetchone(): return jsonify(error="Order not found")
    return mark_paid(d["token"], "UPI payment successful (demo)")


@app.post("/mock/pay")
def mock_pay():
    """The FAKE gateway. Like a real one, it answers with a payment id + a signature that the server then verifies.
    Test UPI IDs: success@test -> paid, failure@test -> fails, any other name@bank -> paid."""
    if not MOCK: return jsonify(error="Test gateway is off"), 404
    d = request.get_json(force=True, silent=True) or {}
    oid, vpa = str(d.get("order_id", "")), str(d.get("upi_id", "")).strip().lower()
    if not db().execute("SELECT 1 FROM orders WHERE rzp=? AND status='pending'", (oid,)).fetchone(): return jsonify(error="Order not found or already paid")
    if not re.fullmatch(r"[a-z0-9._\-]{2,}@[a-z]{2,}", vpa): return jsonify(error="Enter a UPI ID like name@bank")
    if vpa.startswith("failure@"): return jsonify(error="Payment failed (test UPI ID failure@...). Try success@test.")
    pid = "pay_mock_" + secrets.token_hex(4)
    return jsonify(payment_id=pid, signature=hmac.new(MOCK_SECRET.encode(), f"{oid}|{pid}".encode(), hashlib.sha256).hexdigest())


@app.post("/api/pay/claim")
def pay_claim():
    """Direct UPI has no automatic confirmation: the shopper says 'I paid' (optionally with the 12-digit UTR),
    and the shop checks its own UPI app before handing the items over."""
    d = request.get_json(force=True, silent=True) or {}
    o = db().execute("SELECT * FROM orders WHERE token=? AND status='pending' AND rzp IS NULL", (str(d.get("token", "")),)).fetchone()
    utr = re.sub(r"\s", "", str(d.get("utr", "")))
    if not UPI_ID or not o: return jsonify(error="Order not found")
    if utr and not re.fullmatch(r"\d{12}", utr): return jsonify(error="The UPI reference (UTR) has 12 digits. Check it, or leave it empty.")
    db().execute("UPDATE orders SET status='claimed', pay_id=? WHERE token=?", (utr or None, o["token"])); db().commit()
    payload, img = qr_for(o["token"])
    return jsonify(token=o["token"], paid="Payment noted. The shop will confirm it when you collect.", qr=img, payload=payload)


@app.post("/api/pay/verify")
def pay_verify():
    """Razorpay tells the browser 'paid'. We never trust the browser: we check the signature with our secret."""
    d = request.get_json(force=True, silent=True) or {}
    o = db().execute("SELECT * FROM orders WHERE token=?", (d.get("token"),)).fetchone()
    secret = MOCK_SECRET if MOCK else RZP_SECRET
    if not o or not o["rzp"] or not secret: return jsonify(error="Order not found")
    good = hmac.new(secret.encode(), f"{o['rzp']}|{d.get('razorpay_payment_id')}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, str(d.get("razorpay_signature", ""))): return jsonify(error="Payment could not be verified")
    db().execute("UPDATE orders SET pay_id=? WHERE token=?", (str(d.get("razorpay_payment_id", ""))[:40], o["token"]))
    return mark_paid(o["token"], "Payment successful (test gateway, no real money)" if MOCK else "Payment successful (Razorpay UPI test mode)")


# ---------- counter endpoints ----------
def take(c, shop, item, qty, price):
    """Atomic: only succeeds if enough stock. Lowers actual AND expected (a billed sale)."""
    r = c.execute("UPDATE stock SET actual=actual-?, expected=expected-? WHERE shop_id=? AND item_id=? AND actual>=?", (qty, qty, shop, item, qty))
    if r.rowcount == 0: return False
    c.execute("INSERT INTO sales(shop_id,item_id,qty,amount) VALUES(?,?,?,?)", (shop, item, qty, qty * price))
    return True


@app.post("/api/handover")
@staff
def api_handover():
    d = request.get_json(force=True, silent=True) or {}
    c = db(); tk = token_from(d.get("token"))
    o = c.execute("SELECT * FROM orders WHERE token=?", (tk,)).fetchone() if tk else None
    if not o: return jsonify(error="Token not found or QR is not genuine")
    if o["status"] == "pending": return jsonify(error="Payment not completed")
    if o["status"] in ("done", "returned"): return jsonify(error="Already handed over")
    if o["shop_id"] != d.get("shop_id"): return jsonify(error="This order is for another shop")
    im = items_map()
    if o["status"] == "claimed" and not d.get("confirm"):  # staff must check their own UPI app first
        due = round(sum(im[k]["price"] * q for k, q in json.loads(o["kit"]).items()))
        return jsonify(confirm=f"The customer says they paid Rs {due} by UPI" + (f" (UTR {o['pay_id']})" if o["pay_id"] else "") + ". Check it in your UPI app. Hand over now?")
    for k, q in json.loads(o["kit"]).items():
        if not take(c, o["shop_id"], k, q, im[k]["price"]):
            c.rollback(); return jsonify(error="Stock changed, items missing")
    c.execute("UPDATE orders SET status='done', done_at=datetime('now') WHERE token=?", (o["token"],)); c.commit()
    due = round(sum(im[k]["price"] * q for k, q in json.loads(o["kit"]).items()))
    extra = f" Collect Rs {due} from the customer now (pay at shop)." if o["status"] == "reserved" else ""
    return jsonify(ok=f"Handed over: {o['title']}. Stock and bill updated.{extra}")


@app.post("/api/return/request")
def api_return_request():
    """Shopper side: ask for a return within 7 days of collecting the order."""
    d = request.get_json(force=True, silent=True) or {}
    tk = token_from(d.get("token")); c = db()
    o = c.execute("SELECT * FROM orders WHERE token=? AND status='done' AND datetime(done_at) >= datetime('now','-7 day')", (tk,)).fetchone() if tk else None
    if not o: return jsonify(error="No collected order found for this token within the last 7 days")
    if o["ret"]: return jsonify(error="A return was already requested for this order")
    c.execute("UPDATE orders SET ret='requested', ret_reason=? WHERE token=?", (str(d.get("reason", ""))[:60], o["token"])); c.commit()
    shop = c.execute("SELECT name FROM shops WHERE id=?", (o["shop_id"],)).fetchone()["name"]
    return jsonify(ok=f"Return requested. Bring the item and your QR to {shop}.")


@app.post("/api/return")
@staff
def api_return():
    """Counter side: take the items back, put them on the shelf, refund or exchange."""
    d = request.get_json(force=True, silent=True) or {}
    c = db(); tk = token_from(d.get("token"))
    o = c.execute("SELECT * FROM orders WHERE token=?", (tk,)).fetchone() if tk else None
    if not o or o["status"] != "done": return jsonify(error="No handed-over order found for this token")
    if o["shop_id"] != d.get("shop_id"): return jsonify(error="This order is for another shop")
    if not c.execute("SELECT 1 FROM orders WHERE token=? AND datetime(done_at) >= datetime('now','-7 day')", (tk,)).fetchone():
        return jsonify(error="Return window (7 days) is over")
    im = items_map(); kit = json.loads(o["kit"]); total = sum(im[k]["price"] * q for k, q in kit.items())
    for k, q in kit.items():  # item is back on the shelf: actual AND expected go up, so no false loss alert
        c.execute("UPDATE stock SET actual=actual+?, expected=expected+? WHERE shop_id=? AND item_id=?", (q, q, o["shop_id"], k))
        c.execute("INSERT INTO sales(shop_id,item_id,qty,amount) VALUES(?,?,?,?)", (o["shop_id"], k, -q, -q * im[k]["price"]))
    mode = "exchange" if d.get("mode") == "exchange" else "refund"
    c.execute("UPDATE orders SET status='returned', ret=? WHERE token=?", (mode, tk)); c.commit()
    if mode == "exchange":
        return jsonify(ok="Exchange accepted. Stock restored. Give the new item and bill it on the counter.")
    if MOCK and o["pay_id"]:
        return jsonify(ok=f"Return accepted. Rs {round(total)} refunded to the customer's UPI (test gateway).")
    if o["pay_id"] and RZP_ID and RZP_SECRET:
        try:
            auth = base64.b64encode(f"{RZP_ID}:{RZP_SECRET}".encode()).decode()
            http_json(f"https://api.razorpay.com/v1/payments/{o['pay_id']}/refund", {"amount": round(total * 100)}, {"Authorization": "Basic " + auth})
            return jsonify(ok=f"Return accepted. Rs {round(total)} refunded to the customer's UPI (Razorpay test mode).")
        except Exception:
            return jsonify(ok=f"Return accepted and stock restored, but the online refund failed. Refund Rs {round(total)} manually.")
    return jsonify(ok=f"Return accepted. Refund Rs {round(total)} at the counter.")


@app.post("/api/bill")
@staff
def api_bill():
    d = request.get_json(force=True, silent=True) or {}
    c = db(); im = items_map(); total = 0
    for ln in d.get("lines", []):
        it = im.get(ln.get("item_id"))
        try: reading = float(ln.get("reading"))
        except (TypeError, ValueError): reading = 0
        if not it or reading <= 0: continue
        qty = round(reading / it["piece_w"]) if it["piece_w"] else reading  # weight -> pieces
        if qty <= 0 or not take(c, d.get("shop_id"), it["id"], qty, it["price"]):
            c.rollback(); return jsonify(error=f"Not enough stock for {it['name']}")
        total += qty * it["price"]
    c.commit()
    return jsonify(ok=f"Sale completed: Rs {round(total)}")


@app.post("/api/sensor")
@staff
def api_sensor():
    """The shelf sensor reports the ACTUAL stock. (ESP32 later posts here; the demo button sends 'removed'.)
    body: shop_id, item_id, and one of: removed (demo) | actual (count) | weight_g (grams on scale)"""
    d = request.get_json(force=True, silent=True) or {}
    c = db(); it = items_map().get(d.get("item_id"))
    r = c.execute("SELECT * FROM stock WHERE shop_id=? AND item_id=?", (d.get("shop_id"), d.get("item_id"))).fetchone()
    if not it or not r: return jsonify(error="Unknown shop or item")
    try:
        if "removed" in d: new = max(0, r["actual"] - float(d["removed"]))
        elif "weight_g" in d and it["piece_w"]: new = round(float(d["weight_g"]) / it["piece_w"])
        else: new = float(d["actual"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="Bad sensor reading")
    c.execute("UPDATE stock SET actual=? WHERE shop_id=? AND item_id=?", (new, d["shop_id"], d["item_id"])); c.commit()
    miss = r["expected"] - new
    alert = f"Automatic alert: {miss:g} {it['unit']} of {it['name']} missing, no bill found (Rs {round(miss * it['price'])}). The owner was notified." if miss > 0 else ""
    return jsonify(alert=alert)


# ---------- owner endpoints ----------
@app.get("/api/dashboard")
@staff
def api_dashboard():
    sid = request.args.get("shop_id", "s1"); c = db(); loss = 0; rows = []; alerts = []
    for r in c.execute("SELECT i.*, s.actual, s.expected FROM stock s JOIN items i ON i.id=s.item_id WHERE s.shop_id=?", (sid,)):
        miss = max(0, r["expected"] - r["actual"]); loss += miss * r["price"]
        rows.append(dict(name=r["name"], actual=r["actual"], expected=r["expected"], missing=miss))
        if miss > 0: alerts.append(f"{miss:g} {r['unit']} of {r['name']} missing (Rs {round(miss * r['price'])}): stock dropped with no bill")
    sold = c.execute("SELECT COALESCE(SUM(amount),0) FROM sales WHERE shop_id=?", (sid,)).fetchone()[0]
    log = [dict(name=x["name"], qty=x["qty"], amount=x["amount"]) for x in c.execute(
        "SELECT i.name, s.qty, s.amount FROM sales s JOIN items i ON i.id=s.item_id WHERE s.shop_id=? ORDER BY s.id DESC LIMIT 6", (sid,))]
    return jsonify(rows=rows, sold=round(sold), loss=round(loss), low=sum(1 for r in rows if r["actual"] <= 3), alerts=alerts, log=log)


@app.post("/api/reset")
@staff
def api_reset():
    g.pop("db", None); init_db(reset=True); return jsonify(ok=True)


init_db()  # also runs when a server like gunicorn imports this file

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
