"""Fresh Stock: local inventory workbench. Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
import csv
import io
import json
import secrets
import sqlite3
import tempfile
import threading
import webbrowser
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).resolve().parent
CHINA = timezone(timedelta(hours=8))


class BusinessError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def today():
    return datetime.now(CHINA).date()


def now():
    return datetime.now(CHINA).isoformat(timespec="seconds")


def text(data, key, required=True, limit=120):
    value = data.get(key, "")
    if not isinstance(value, str):
        raise BusinessError(f"{key} 必须是文本")
    value = value.strip()
    if (required and not value) or len(value) > limit:
        raise BusinessError(f"{key} 不能为空且不能超过 {limit} 个字符" if required else f"{key} 最多 {limit} 个字符")
    return value


def number(value, scale=1000, allow_zero=False):
    try:
        amount = Decimal(str(value))
        scaled = amount * scale
        if not amount.is_finite() or scaled != scaled.to_integral_value():
            raise ValueError()
        result = int(scaled)
        if result < 0 or (result == 0 and not allow_zero) or result > 10**10:
            raise ValueError()
        return result
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        precision = 3 if scale == 1000 else 2
        raise BusinessError(f"数值无效：请输入{'非负' if allow_zero else '正'}数，最多 {precision} 位小数") from None


def integer(value, label, minimum=1, maximum=10**9):
    try:
        n = int(str(value))
        if not minimum <= n <= maximum:
            raise ValueError()
        return n
    except (ValueError, TypeError):
        raise BusinessError(f"{label}必须为 {minimum} 至 {maximum} 的整数") from None


def iso_date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed
    except (ValueError, TypeError):
        raise BusinessError("日期无效，请使用 YYYY-MM-DD 格式") from None


class Inventory:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT OR IGNORE INTO meta VALUES ('mode','empty');
                CREATE TABLE IF NOT EXISTS products(
                    id INTEGER PRIMARY KEY, sku TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    name TEXT NOT NULL, category TEXT NOT NULL, unit TEXT NOT NULL,
                    min_stock INTEGER NOT NULL CHECK(min_stock>=0),
                    warning_days INTEGER NOT NULL CHECK(warning_days BETWEEN 0 AND 365),
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS batches(
                    id INTEGER PRIMARY KEY,product_id INTEGER NOT NULL REFERENCES products(id),
                    code TEXT NOT NULL, location TEXT NOT NULL, supplier TEXT NOT NULL,
                    produced_on TEXT NOT NULL,expires_on TEXT NOT NULL,
                    cost INTEGER NOT NULL CHECK(cost>=0), qty INTEGER NOT NULL CHECK(qty>=0),
                    version INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,
                    UNIQUE(product_id,code));
                CREATE TABLE IF NOT EXISTS movements(
                    id INTEGER PRIMARY KEY,batch_id INTEGER NOT NULL REFERENCES batches(id),
                    kind TEXT NOT NULL CHECK(kind IN ('inbound','outbound','damage','count')),
                    delta INTEGER NOT NULL,balance INTEGER NOT NULL CHECK(balance>=0),
                    reference TEXT NOT NULL,note TEXT NOT NULL,operator TEXT NOT NULL,
                    request_id TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS counts(
                    id INTEGER PRIMARY KEY,batch_id INTEGER NOT NULL REFERENCES batches(id),
                    expected INTEGER NOT NULL,actual INTEGER NOT NULL,difference INTEGER NOT NULL,
                    reason TEXT NOT NULL,operator TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests(
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL,response TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS batch_fefo ON batches(product_id,expires_on);
                CREATE INDEX IF NOT EXISTS movement_date ON movements(created_at);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def change(self, action, data):
        request_id = text(data, "request_id", limit=80)
        payload = json.dumps([action, data], ensure_ascii=False, sort_keys=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if previous:
                if previous["payload"] != payload:
                    raise BusinessError("请求编号重复但内容不同，请重新打开表单", 409)
                return json.loads(previous["response"])
            handlers = {"products": self.product, "inbound": self.inbound,
                        "outbound": self.outbound, "damage": self.damage, "count": self.count}
            if action not in handlers:
                raise BusinessError("找不到此操作", 404)
            result = handlers[action](db, data)
            db.execute("INSERT INTO requests VALUES (?,?,?)", (request_id, payload, json.dumps(result, ensure_ascii=False)))
            return result

    def product(self, db, data):
        sku, name = text(data, "sku", limit=40), text(data, "name", limit=80)
        category, unit = text(data, "category", limit=30), text(data, "unit", limit=10)
        minimum = number(data.get("min_stock", 0), allow_zero=True)
        warning = integer(data.get("warning_days", 3), "预警天数", 0, 365)
        product_id = data.get("id")
        try:
            if product_id:
                product_id = integer(product_id, "商品编号")
                previous = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
                if not previous:
                    raise BusinessError("商品不存在", 404)
                if previous["sku"] != sku or previous["unit"] != unit:
                    raise BusinessError("商品编码和计量单位创建后不可变更，请新建商品")
                db.execute("UPDATE products SET name=?,category=?,min_stock=?,warning_days=? WHERE id=?", (name, category, minimum, warning, product_id))
            else:
                cur = db.execute("INSERT INTO products(sku,name,category,unit,min_stock,warning_days,created_at) VALUES(?,?,?,?,?,?,?)", (sku, name, category, unit, minimum, warning, now()))
                product_id = cur.lastrowid
        except sqlite3.IntegrityError:
            raise BusinessError("商品编码已存在，请使用其他编码", 409) from None
        return {"message": "商品资料已保存", "id": product_id}

    def record(self, db, batch_id, kind, delta, data):
        row = db.execute("SELECT qty FROM batches WHERE id=?", (batch_id,)).fetchone()
        balance = row["qty"] + delta
        if balance < 0:
            raise BusinessError("库存不足，操作未提交", 409)
        db.execute("UPDATE batches SET qty=?,version=version+1 WHERE id=?", (balance, batch_id))
        db.execute("""INSERT INTO movements(batch_id,kind,delta,balance,reference,note,operator,request_id,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?)""", (batch_id, kind, delta, balance,
                      text(data, "reference", required=False, limit=80), text(data, "note", required=False, limit=300),
                      text(data, "operator", limit=30), data["request_id"], now()))

    def inbound(self, db, data):
        product_id = integer(data.get("product_id"), "商品编号")
        if not db.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
            raise BusinessError("请先创建商品", 404)
        code = text(data, "code", limit=60)
        location, supplier = text(data, "location", limit=40), text(data, "supplier", limit=80)
        produced, expires = iso_date(data.get("produced_on")), iso_date(data.get("expires_on"))
        if produced > today() or expires < produced or expires < today():
            raise BusinessError("生产日期不能晚于今天，有效期不能早于生产日期或今天")
        qty, cost = number(data.get("qty")), number(data.get("cost"), scale=100, allow_zero=True)
        existing = db.execute("SELECT * FROM batches WHERE product_id=? AND code=?", (product_id, code)).fetchone()
        if existing:
            checks = {"location": location, "supplier": supplier, "produced_on": str(produced), "expires_on": str(expires), "cost": cost}
            if any(existing[key] != value for key, value in checks.items()):
                raise BusinessError("同一批次的日期、库位、供应商和成本必须一致；不同到货请使用新批次号", 409)
            batch_id = existing["id"]
        else:
            cur = db.execute("""INSERT INTO batches(product_id,code,location,supplier,produced_on,expires_on,cost,qty,created_at)
                                VALUES(?,?,?,?,?,?,?,0,?)""", (product_id, code, location, supplier, str(produced), str(expires), cost, now()))
            batch_id = cur.lastrowid
        self.record(db, batch_id, "inbound", qty, data)
        return {"message": "入库完成，库存和流水已同步更新", "batch_id": batch_id}

    def outbound(self, db, data):
        product_id, quantity = integer(data.get("product_id"), "商品编号"), number(data.get("qty"))
        text(data, "reference", limit=80)
        rows = db.execute("""SELECT * FROM batches WHERE product_id=? AND qty>0 AND expires_on>=?
                             ORDER BY expires_on,produced_on,id""", (product_id, str(today()))).fetchall()
        available = sum(r["qty"] for r in rows)
        if available < quantity:
            raise BusinessError(f"可用库存不足：当前 {available / 1000:g}，申请 {quantity / 1000:g}；过期批次不参与出库", 409)
        remaining, allocations = quantity, []
        for row in rows:
            if remaining == 0:
                break
            take = min(remaining, row["qty"])
            self.record(db, row["id"], "outbound", -take, data)
            allocations.append({"batch": row["code"], "qty": take / 1000})
            remaining -= take
        return {"message": "出库完成，已按先到期先出分配批次", "allocations": allocations}

    def get_batch(self, db, data):
        batch_id = integer(data.get("batch_id"), "批次编号")
        batch = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise BusinessError("批次不存在", 404)
        return batch

    def damage(self, db, data):
        batch = self.get_batch(db, data)
        qty = number(data.get("qty"))
        text(data, "note", limit=300)
        self.record(db, batch["id"], "damage", -qty, data)
        return {"message": "报损完成，已记录损耗原因"}

    def count(self, db, data):
        batch = self.get_batch(db, data)
        version = integer(data.get("version"), "库存版本")
        if batch["version"] != version:
            raise BusinessError("此批次库存已变动，请关闭表单并刷新后重新盘点", 409)
        actual = number(data.get("actual"), allow_zero=True)
        difference = actual - batch["qty"]
        reason = text(data, "note", limit=300)
        operator = text(data, "operator", limit=30)
        db.execute("INSERT INTO counts(batch_id,expected,actual,difference,reason,operator,created_at) VALUES(?,?,?,?,?,?,?)",
                   (batch["id"], batch["qty"], actual, difference, reason, operator, now()))
        self.record(db, batch["id"], "count", difference, data)
        return {"message": "盘点已完成，差异已计入库存", "difference": difference / 1000}

    def state(self):
        with self.connect() as db:
            db.execute("BEGIN")
            products = [dict(r) for r in db.execute("SELECT * FROM products ORDER BY id")]
            batches = [dict(r) for r in db.execute("""SELECT b.*,p.name,p.sku,p.category,p.unit,p.warning_days
                           FROM batches b JOIN products p ON p.id=b.product_id ORDER BY b.expires_on,b.id""")]
            movements = [dict(r) for r in db.execute("""SELECT m.*,b.code,b.cost,p.name,p.sku,p.unit
                           FROM movements m JOIN batches b ON b.id=m.batch_id JOIN products p ON p.id=b.product_id ORDER BY m.id DESC""")]
            counts = [dict(r) for r in db.execute("""SELECT c.*,b.code,p.name,p.sku,p.unit FROM counts c
                           JOIN batches b ON b.id=c.batch_id JOIN products p ON p.id=b.product_id ORDER BY c.id DESC""")]
            mode = db.execute("SELECT value FROM meta WHERE key='mode'").fetchone()[0]
        current = today()
        for b in batches:
            b["days_left"] = (date.fromisoformat(b["expires_on"]) - current).days
            b["status"] = "empty" if b["qty"] == 0 else "expired" if b["days_left"] < 0 else "expiring" if b["days_left"] <= b["warning_days"] else "normal"
            b["value"] = round(b["qty"] * b["cost"] / 100000, 2)
            b["qty"] /= 1000
            b["cost"] /= 100
        for p in products:
            own = [b for b in batches if b["product_id"] == p["id"]]
            p["min_stock"] /= 1000
            p["stock"] = round(sum(b["qty"] for b in own), 3)
            p["available"] = round(sum(b["qty"] for b in own if b["days_left"] >= 0), 3)
            p["low_stock"] = p["available"] < p["min_stock"]
        for m in movements:
            m["delta"] /= 1000
            m["balance"] /= 1000
            m["value"] = round(abs(m["delta"]) * m.pop("cost") / 100, 2)
        for c in counts:
            for key in ("expected", "actual", "difference"):
                c[key] /= 1000
        active = [b for b in batches if b["qty"] > 0]
        summary = {"products": len(products), "batches": len(active),
                   "stock_value": round(sum(b["value"] for b in batches), 2),
                   "expiring": sum(b["status"] == "expiring" for b in active),
                   "expired": sum(b["status"] == "expired" for b in active),
                   "low_stock": sum(p["low_stock"] for p in products),
                   "today_outbound": len({m["request_id"] for m in movements if m["kind"] == "outbound" and m["created_at"][:10] == str(current)}),
                   "today_damage_value": round(sum(m["value"] for m in movements if m["kind"] == "damage" and m["created_at"][:10] == str(current)), 2)}
        trend = []
        for back in range(6, -1, -1):
            day = str(current - timedelta(days=back))
            day_movements = [m for m in movements if m["created_at"][:10] == day]
            trend.append({"date": day, **{kind: len({m["request_id"] for m in day_movements if m["kind"] == kind}) for kind in ("inbound", "outbound", "damage")}})
        return {"today": str(current), "mode": mode, "products": products, "batches": batches,
                "movements": movements, "counts": counts, "summary": summary, "trend": trend}

    def seed_demo(self):
        """Initialize once. Never overwrite an existing catalogue."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM products LIMIT 1").fetchone():
                return False
            db.execute("UPDATE meta SET value='demo' WHERE key='mode'")
            catalog = [
                ("VEG-001", "云南小白菜", "蔬菜", "kg", 20, 2, 4.8),
                ("VEG-002", "山东西红柿", "蔬菜", "kg", 15, 3, 5.2),
                ("FRU-001", "陕西红富士苹果", "水果", "kg", 25, 5, 7.6),
                ("DAI-001", "巴氏鲜牛奶 950mL", "乳品", "瓶", 20, 3, 12.5),
                ("MEA-001", "冷鲜鸡胸肉", "肉禽", "kg", 15, 2, 19.8),
                ("EGG-001", "鲜鸡蛋 10枚装", "蛋品", "盒", 10, 5, 9.9),
                ("DRY-001", "东北大米 5kg", "粮油", "袋", 10, 30, 26),
                ("FRU-002", "海南香蕉", "水果", "kg", 15, 2, 5.5),
            ]
            for i, (sku, name, category, unit, minimum, warning, cost) in enumerate(catalog, 1):
                db.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?,?)", (i, sku, name, category, unit, minimum * 1000, warning, now()))
                for j in range(2 if i in (1, 4, 5) else 1):
                    days = [-1, 6, 18, 1, 2, 12, 100, 1][i - 1] + j * 6
                    qty = [8, 42, 76, 18, 12, 36, 24, 10][i - 1] + j * 15
                    produced = today() - timedelta(days=7 if i != 7 else 80)
                    expiry = today() + timedelta(days=days)
                    created = (datetime.now(CHINA) - timedelta(days=6)).isoformat(timespec="seconds")
                    cur = db.execute("INSERT INTO batches(product_id,code,location,supplier,produced_on,expires_on,cost,qty,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (i, f"DEMO-{i:02}-{j+1:02}", f"{'冷藏' if i in (1,4,5) else '常温'}-{i:02}-{j+1:02}", "演示供应商", str(produced), str(expiry), round(cost * 100), qty * 1000, created))
                    db.execute("INSERT INTO movements(batch_id,kind,delta,balance,reference,note,operator,request_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (cur.lastrowid, "inbound", qty * 1000, qty * 1000, "DEMO-入库", "系统演示数据", "演示操作员", f"seed-{i}-{j}", created))
            # Outbound examples reduce the actual batch balances: the ledger remains reconciled.
            for day in range(6):
                batch = db.execute("SELECT * FROM batches WHERE product_id=3").fetchone()
                delta = -(day + 1) * 1000
                balance = batch["qty"] + delta
                db.execute("UPDATE batches SET qty=?,version=version+1 WHERE id=?", (balance, batch["id"]))
                stamp = (datetime.now(CHINA) - timedelta(days=5 - day)).isoformat(timespec="seconds")
                db.execute("INSERT INTO movements(batch_id,kind,delta,balance,reference,note,operator,request_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (batch["id"], "outbound", delta, balance, f"DEMO-订单-{day+1}", "系统演示数据", "演示操作员", f"seed-out-{day}", stamp))
        return True

    def export(self, kind):
        data = self.state()
        columns = {
            "stock": [("sku", "商品编码"), ("name", "商品名称"), ("code", "批次号"), ("location", "库位"), ("supplier", "供应商"), ("qty", "库存数量"), ("unit", "单位"), ("cost", "单位成本"), ("value", "库存成本金额"), ("produced_on", "生产日期"), ("expires_on", "到期日期"), ("days_left", "剩余天数"), ("status", "状态")],
            "movements": [("id", "流水号"), ("created_at", "时间"), ("kind", "类型"), ("sku", "商品编码"), ("name", "商品名称"), ("code", "批次号"), ("delta", "变动数量"), ("balance", "变动后库存"), ("unit", "单位"), ("reference", "关联单号"), ("operator", "操作人"), ("note", "备注")],
            "counts": [("id", "盘点编号"), ("created_at", "时间"), ("name", "商品名称"), ("code", "批次号"), ("expected", "账面数量"), ("actual", "实盘数量"), ("difference", "差异"), ("unit", "单位"), ("reason", "原因"), ("operator", "操作人")],
        }
        if kind not in columns:
            raise BusinessError("不支持此导出类型", 404)
        rows = data["batches" if kind == "stock" else kind]
        labels = {"normal": "正常", "expiring": "临期", "expired": "过期", "empty": "已清零", "inbound": "入库", "outbound": "出库", "damage": "报损", "count": "盘点"}
        def safe(value):
            if isinstance(value, str):
                if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
                    return "'" + value
            return value
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow([label for _, label in columns[kind]])
        for row in rows:
            writer.writerow([safe(labels.get(row[key], row[key]) if key in ("kind", "status") else row[key]) for key, _ in columns[kind]])
        return output.getvalue().encode("utf-8-sig")


class Handler(BaseHTTPRequestHandler):
    server_version = "FreshStock/1.0"

    def send(self, body, content_type="application/json; charset=utf-8", status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def json(self, value, status=200):
        self.send(json.dumps(value, ensure_ascii=False).encode("utf-8"), status=status)

    def valid_host(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

    def do_GET(self):
        if not self.valid_host():
            return self.json({"error": "仅支持本机访问"}, 403)
        path = urlsplit(self.path)
        try:
            if path.path == "/api/state":
                return self.json({**self.server.inventory.state(), "token": self.server.token})
            if path.path == "/api/export":
                kind = parse_qs(path.query).get("kind", ["stock"])[0]
                return self.send(self.server.inventory.export(kind), "text/csv; charset=utf-8", filename=f"fresh-stock-{kind}-{today()}.csv")
            if path.path == "/api/backup":
                with tempfile.TemporaryDirectory() as directory:
                    backup = Path(directory) / "backup.db"
                    with self.server.inventory.connect() as source:
                        target = sqlite3.connect(backup)
                        try:
                            source.backup(target)
                        finally:
                            target.close()
                    return self.send(backup.read_bytes(), "application/octet-stream", filename=f"fresh-stock-{today()}.db")
            static = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8"), "/tokens.css": ("tokens.css", "text/css; charset=utf-8"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
            if path.path in static:
                filename, mime = static[path.path]
                return self.send((ROOT / "static" / filename).read_bytes(), mime)
            self.json({"error": "页面不存在"}, 404)
        except BusinessError as exc:
            self.json({"error": str(exc)}, exc.status)
        except (sqlite3.Error, OSError):
            self.json({"error": "读取失败，请检查数据库或重启服务"}, 500)

    def do_POST(self):
        if not self.valid_host() or not secrets.compare_digest(self.headers.get("X-App-Token", ""), self.server.token):
            return self.json({"error": "会话已过期，请刷新页面"}, 403)
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers['Host']}":
            return self.json({"error": "不支持跨站操作"}, 403)
        try:
            length = integer(self.headers.get("Content-Length", "0"), "请求长度", 1, 32768)
            if self.headers.get_content_type() != "application/json":
                raise BusinessError("请使用 JSON 请求", 415)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise BusinessError("请求格式无效")
            path = urlsplit(self.path).path
            if not path.startswith("/api/"):
                raise BusinessError("接口不存在", 404)
            self.json(self.server.inventory.change(path[5:], data))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.json({"error": "JSON 格式无效"}, 400)
        except BusinessError as exc:
            self.json({"error": str(exc)}, exc.status)
        except sqlite3.Error:
            self.json({"error": "数据库暂时不可用，操作未提交，请稍后重试"}, 500)


def make_server(inventory, port=8765):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.inventory = inventory
    server.token = secrets.token_hex(32)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description="鲜仓：生鲜库存与效期管理")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "inventory.db")
    parser.add_argument("--demo", action="store_true", help="首次启动装入演示数据，不覆盖已有商品")
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = parser.parse_args()
    inventory = Inventory(args.db)
    if args.demo:
        inventory.seed_demo()
    try:
        server = make_server(inventory, args.port)
    except OSError as exc:
        parser.exit(1, f"启动失败：{exc}。可使用 --port 8766 更换端口。\n")
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Fresh Stock running: {url}\nDatabase: {inventory.path}\nPress Ctrl+C to stop.", flush=True)
    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
