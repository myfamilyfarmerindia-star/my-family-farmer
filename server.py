import http.server
import json
import os
import re
import socketserver
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.request

REPO_DIR = "/home/user/my-family-farmer-repo"
DEPLOY_HTML = "/home/user/deploy_ui/deploy.html"
DB_PATH = "/home/user/mff_database.db"
PORT = int(os.environ.get("PORT", "8080"))

INITIAL_POOLS = [
    ("Cereals", "Cereals & Heirloom Atta", 14200, 15100),
    ("Millets", "Millets (Shree Anna)", 4800, 3100),
    ("Pulses", "Pulses & Unpolished Dals", 6400, 5200),
    ("Oilseeds", "Oilseeds & Wood Oils", 5900, 6100),
    ("Sweeteners", "Sugarcane & Jaggery", 3500, 2900),
    ("Spices", "Spices & Condiments", 1950, 2400),
    ("Orchard", "Orchard, Makhana & Dry Fruits", 2800, 1600),
    ("Allied", "A2 Ghee, Honey & Herbs", 2200, 1900),
]


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS crop_pools (
        crop_key TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        demand_kg INTEGER NOT NULL,
        supply_kg INTEGER NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS registrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        city TEXT NOT NULL,
        crops_json TEXT NOT NULL,
        detail TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_code TEXT NOT NULL,
        items_count INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        delivery_mode TEXT NOT NULL,
        pay_mode TEXT NOT NULL,
        summary TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("SELECT COUNT(*) FROM crop_pools")
    if c.fetchone()[0] == 0:
        c.executemany("INSERT INTO crop_pools VALUES (?, ?, ?, ?)", INITIAL_POOLS)
    c.execute("SELECT COUNT(*) FROM registrations")
    if c.fetchone()[0] == 0:
        c.execute(
            "INSERT INTO registrations (kind, name, city, crops_json, detail, status) VALUES (?, ?, ?, ?, ?, ?)",
            ("Family", "Verma Family (4 Members)", "Varanasi • Mahmoorganj", json.dumps(["Cereals", "Millets", "Allied"]), "₹7,500/mo pooled demand", "Demand Pooled"),
        )
        c.execute(
            "INSERT INTO registrations (kind, name, city, crops_json, detail, status) VALUES (?, ?, ?, ?, ?, ?)",
            ("Farmer", "Mahendra Singh (6 Acres)", "Chandauli, UP", json.dumps(["Cereals", "Pulses", "Oilseeds"]), "Certified Chemical-Free • +1,500 kg capacity", "Supply Added"),
        )
    c.execute("SELECT COUNT(*) FROM orders")
    if c.fetchone()[0] == 0:
        c.execute(
            "INSERT INTO orders (order_code, items_count, amount, delivery_mode, pay_mode, summary) VALUES (?, ?, ?, ?, ?, ?)",
            ("#MFF-2026-884", 3, 1600, "society", "full", "5 kg Khapli Atta • 1 kg Sprouted Ragi • 500ml Gir A2 Ghee"),
        )
    conn.commit()
    conn.close()


def get_full_state():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    pools = {}
    for row in c.execute("SELECT crop_key, demand_kg, supply_kg FROM crop_pools"):
        pools[row["crop_key"]] = {"demandKg": row["demand_kg"], "supplyKg": row["supply_kg"]}

    regs = []
    for row in c.execute("SELECT kind, name, city, crops_json, detail, status FROM registrations ORDER BY id DESC LIMIT 15"):
        regs.append({
            "kind": row["kind"],
            "name": row["name"],
            "city": row["city"],
            "crops": json.loads(row["crops_json"]),
            "detail": row["detail"],
            "status": row["status"],
        })

    orders = []
    for row in c.execute("SELECT order_code, items_count, amount, delivery_mode, pay_mode, summary FROM orders ORDER BY id DESC LIMIT 15"):
        orders.append(dict(row))

    fam_extra = c.execute("SELECT COUNT(*) FROM registrations WHERE kind='Family'").fetchone()[0] - 1
    frm_extra = c.execute("SELECT COUNT(*) FROM registrations WHERE kind='Farmer'").fetchone()[0] - 1
    conn.close()
    return {
        "ok": True,
        "backend": "sqlite",
        "familiesCount": 1284 + max(0, fam_extra),
        "farmersCount": 74 + max(0, frm_extra),
        "pools": pools,
        "registrations": regs,
        "orders": orders,
    }


def gh_api(method, path, token, payload=None):
    url = f"https://api.github.com{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "MyFamilyFarmer-Deployer",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"message": raw}


def sanitize(text, token):
    return text.replace(token, "***") if token else text


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=REPO_DIR, **kwargs)

    def log_message(self, format, *args):
        print(f"[HTTP] {args[0] if args else ''}", flush=True)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/state":
            self.send_json(200, get_full_state())
            return
        if path in ("/deploy", "/deploy/") and os.path.exists(DEPLOY_HTML):
            with open(DEPLOY_HTML, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path in ("/", "/index.html"):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(min(length, 131072))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self.send_json(400, {"ok": False, "error": "Invalid JSON"})
            return

        if path == "/api/register/family":
            name = (payload.get("name") or "").strip()
            city = (payload.get("city") or "").strip()
            size = int(payload.get("size") or 4)
            budget = int(payload.get("budget") or 6500)
            crops = payload.get("crops") or []
            if not name or not city or not crops:
                self.send_json(400, {"ok": False, "error": "Missing required fields"})
                return
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            for k in crops:
                c.execute("UPDATE crop_pools SET demand_kg = demand_kg + ? WHERE crop_key = ?", (size * 8, k))
            c.execute(
                "INSERT INTO registrations (kind, name, city, crops_json, detail, status) VALUES (?, ?, ?, ?, ?, ?)",
                ("Family", f"{name} ({size} Members)", city, json.dumps(crops), f"₹{budget:,}/mo pooled demand", "Demand Pooled"),
            )
            conn.commit()
            conn.close()
            self.send_json(200, get_full_state())
            return

        if path == "/api/register/farmer":
            name = (payload.get("name") or "").strip()
            city = (payload.get("city") or "").strip()
            acres = int(payload.get("acres") or 2)
            stage = (payload.get("stage") or "Chemical-Free").strip()
            crops = payload.get("crops") or []
            if not name or not city or not crops:
                self.send_json(400, {"ok": False, "error": "Missing required fields"})
                return
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            for k in crops:
                c.execute("UPDATE crop_pools SET supply_kg = supply_kg + ? WHERE crop_key = ?", (acres * 250, k))
            c.execute(
                "INSERT INTO registrations (kind, name, city, crops_json, detail, status) VALUES (?, ?, ?, ?, ?, ?)",
                ("Farmer", f"{name} ({acres} Acres)", city, json.dumps(crops), f"{stage} • +{acres*250} kg capacity", "Supply Added"),
            )
            conn.commit()
            conn.close()
            self.send_json(200, get_full_state())
            return

        if path == "/api/orders":
            order_code = payload.get("order_code") or "#MFF-NEW"
            items_count = int(payload.get("items_count") or 1)
            amount = int(payload.get("amount") or 0)
            delivery_mode = payload.get("delivery_mode") or "society"
            pay_mode = payload.get("pay_mode") or "full"
            summary = payload.get("summary") or "Custom Harvest Basket"
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute(
                "INSERT INTO orders (order_code, items_count, amount, delivery_mode, pay_mode, summary) VALUES (?, ?, ?, ?, ?, ?)",
                (order_code, items_count, amount, delivery_mode, pay_mode, summary),
            )
            conn.commit()
            conn.close()
            self.send_json(200, get_full_state())
            return

        if path == "/api/deploy":
            token = (payload.get("token") or "").strip()
            repo_name = (payload.get("repo_name") or "").strip()
            is_private = bool(payload.get("is_private", False))
            enable_pages = bool(payload.get("enable_pages", True))
            if not token or not re.match(r"^[A-Za-z0-9._-]{1,100}$", repo_name):
                self.send_json(400, {"ok": False, "error": "Invalid token or repository name."})
                return
            status, user_info = gh_api("GET", "/user", token)
            if status != 200 or not user_info.get("login"):
                self.send_json(401, {"ok": False, "error": f"GitHub Auth Error ({status})"})
                return
            owner = user_info["login"]
            askpass_fd, askpass_path = tempfile.mkstemp(prefix="mff_ap_", suffix=".sh")
            try:
                with os.fdopen(askpass_fd, "w") as af:
                    af.write('#!/bin/sh\ncase "$1" in\n*Username*) echo "x-access-token" ;;\n*) echo "$MFF_GH_TOKEN" ;;\nesac\n')
                os.chmod(askpass_path, 0o700)
                env = os.environ.copy()
                env["GIT_ASKPASS"] = askpass_path
                env["GIT_TERMINAL_PROMPT"] = "0"
                env["MFF_GH_TOKEN"] = token
                proc = subprocess.run(["git", "-C", REPO_DIR, "push", "--force", f"https://github.com/{owner}/{repo_name}.git", "main:main"], env=env, capture_output=True, text=True, timeout=35)
                if proc.returncode != 0:
                    self.send_json(400, {"ok": False, "error": sanitize(proc.stderr or proc.stdout, token)})
                    return
            finally:
                if os.path.exists(askpass_path):
                    os.remove(askpass_path)
            pages_url = f"https://{owner}.github.io/{repo_name}/" if enable_pages and not is_private else None
            self.send_json(200, {"ok": True, "repo_url": f"https://github.com/{owner}/{repo_name}", "pages_url": pages_url})
            return

        self.send_json(404, {"ok": False, "error": "Endpoint not found"})


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    init_db()
    with ThreadingHTTPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"My Family Farmer Full-Stack Server (SQLite + REST API) on 0.0.0.0:{PORT}", flush=True)
        httpd.serve_forever()
