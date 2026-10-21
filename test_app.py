import concurrent.futures
import csv
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import uuid
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from app import Inventory, BusinessError, today, make_server


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inventory = Inventory(Path(self.tmp.name) / "test.db")
        self.product = self.change("products", sku="T-01", name="测试番茄", category="蔬菜", unit="kg", min_stock=5, warning_days=3)["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def change(self, action, **data):
        data.setdefault("request_id", str(uuid.uuid4()))
        data.setdefault("operator", "测试员")
        return self.inventory.change(action, data)

    def inbound(self, code="B1", qty=10, days=5, **extra):
        return self.change("inbound", **{"product_id": self.product, "code": code, "qty": qty, "cost": "4.25", "location": "A-01", "supplier": "测试供应商", "produced_on": str(today()), "expires_on": str(today()+timedelta(days=days)), **extra})

    def outbound(self, qty, **extra):
        return self.change("outbound", product_id=self.product, qty=qty, reference="ORDER-1", **extra)

    def test_fefo_splits_batches(self):
        self.inbound("LATER", qty=10, days=8)
        self.inbound("FIRST", qty=3, days=1)
        result = self.outbound(5)
        self.assertEqual(result["allocations"], [{"batch":"FIRST","qty":3},{"batch":"LATER","qty":2}])
        self.assertEqual(self.inventory.state()["products"][0]["available"],8)

    def test_insufficient_stock_is_atomic(self):
        self.inbound(qty=2)
        before = self.inventory.state()
        with self.assertRaises(BusinessError):
            self.outbound(3)
        after = self.inventory.state()
        self.assertEqual(before["batches"],after["batches"])
        self.assertEqual(before["movements"],after["movements"])

    def test_expired_stock_excluded_and_can_be_damaged(self):
        historical = today()-timedelta(days=10)
        with patch("app.today", return_value=historical):
            self.inbound(days=1, produced_on=str(historical), expires_on=str(historical+timedelta(days=1)))
        state = self.inventory.state()
        self.assertEqual(state["summary"]["expired"],1)
        self.assertEqual(state["products"][0]["available"],0)
        with self.assertRaises(BusinessError):
            self.outbound(1)
        self.change("damage", batch_id=state["batches"][0]["id"], qty=10, note="过期处置")
        self.assertEqual(self.inventory.state()["summary"]["expired"],0)

    def test_today_expiry_is_usable(self):
        self.inbound(days=0)
        self.assertEqual(self.inventory.state()["batches"][0]["status"], "expiring")
        self.outbound(1)

    def test_count_adjusts_ledger_and_conflict(self):
        self.inbound(qty=10)
        batch = self.inventory.state()["batches"][0]
        result = self.change("count", batch_id=batch["id"],version=batch["version"],actual="9.125",note="称重复核")
        self.assertEqual(result["difference"],-.875)
        self.assertEqual(self.inventory.state()["counts"][0]["actual"],9.125)
        with self.assertRaises(BusinessError) as exc:
            self.change("count", batch_id=batch["id"],version=batch["version"],actual=8,note="旧盘点表")
        self.assertEqual(exc.exception.status,409)

    def test_same_quantity_count_still_recorded(self):
        self.inbound(qty=2)
        batch=self.inventory.state()["batches"][0]
        self.change("count",batch_id=batch["id"],version=batch["version"],actual=2,note="账实一致")
        self.assertEqual(self.inventory.state()["counts"][0]["difference"],0)

    def test_stale_count_after_inbound_rejected(self):
        self.inbound()
        batch=self.inventory.state()["batches"][0]
        self.inbound(qty=2)
        with self.assertRaises(BusinessError):
            self.change("count",batch_id=batch["id"],version=batch["version"],actual=9,note="过时数据")

    def test_exact_decimal_precision(self):
        self.inbound(qty="0.300")
        self.outbound("0.100")
        self.outbound("0.200")
        self.assertEqual(self.inventory.state()["batches"][0]["qty"],0)

    def test_invalid_numbers_rejected(self):
        for value in ["NaN","Infinity","-1","0","1.0001",True,"abc",None]:
            with self.subTest(value=value), self.assertRaises(BusinessError):
                self.inbound(qty=value)
        self.assertEqual(len(self.inventory.state()["batches"]),0)

    def test_request_retry_idempotent(self):
        self.inbound()
        request_id=str(uuid.uuid4())
        first=self.outbound(2,request_id=request_id)
        second=self.outbound(2,request_id=request_id)
        self.assertEqual(first,second)
        self.assertEqual(self.inventory.state()["products"][0]["available"],8)
        with self.assertRaises(BusinessError):
            self.outbound(3,request_id=request_id)

    def test_concurrent_outbounds_no_oversell(self):
        self.inbound(qty=5)
        def attempt(_):
            try:
                self.outbound(4)
                return True
            except BusinessError:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(attempt,range(2)))
        self.assertEqual(sum(results),1)
        self.assertEqual(self.inventory.state()["products"][0]["available"],1)

    def test_batch_metadata_conflict_rolls_back(self):
        self.inbound()
        with self.assertRaises(BusinessError):
            self.inbound(cost=8)
        self.assertEqual(self.inventory.state()["batches"][0]["qty"],10)

    def test_missing_operator_does_not_leave_new_batch(self):
        with self.assertRaises(BusinessError):
            self.inbound(operator="")
        self.assertFalse(self.inventory.state()["batches"])

    def test_required_reason_and_damage_limit(self):
        self.inbound(qty=2)
        b=self.inventory.state()["batches"][0]
        for qty,note in [(1,""),(3,"多报损")]:
            with self.assertRaises(BusinessError):
                self.change("damage",batch_id=b["id"],qty=qty,note=note)
        self.assertEqual(self.inventory.state()["batches"][0]["qty"],2)

    def test_invalid_dates(self):
        for values in [{"produced_on":"2026-02-30"},{"produced_on":str(today()+timedelta(days=1))},{"expires_on":str(today()-timedelta(days=1))}]:
            with self.assertRaises(BusinessError):
                self.inbound(**values)

    def test_product_uniqueness_and_fixed_unit(self):
        with self.assertRaises(BusinessError):
            self.change("products",sku="t-01",name="重复",category="蔬菜",unit="kg")
        with self.assertRaises(BusinessError):
            self.change("products",id=self.product,sku="T-01",name="番茄",category="蔬菜",unit="箱")

    def test_export_csv_prevents_formulas_and_preserves_numbers(self):
        self.inbound(supplier="=HYPERLINK(1)")
        self.outbound(1)
        raw=self.inventory.export("stock")
        rows=list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
        self.assertIn("'=HYPERLINK(1)",rows[1])
        raw=self.inventory.export("movements").decode("utf-8-sig")
        self.assertIn(",-1.0,",raw)

    def test_persistence_and_balanced_ledger(self):
        self.inbound(qty=10)
        self.outbound(2)
        other=Inventory(self.inventory.path)
        self.assertEqual(other.state()["products"][0]["available"],8)
        with other.connect() as db:
            result=db.execute("SELECT b.qty,SUM(m.delta) AS net FROM batches b JOIN movements m ON b.id=m.batch_id GROUP BY b.id").fetchone()
            self.assertEqual(result["qty"],result["net"])

    def test_demo_never_overwrites_existing_data(self):
        self.assertFalse(self.inventory.seed_demo())
        self.assertEqual(len(self.inventory.state()["products"]),1)

    def test_fresh_demo_reconciles(self):
        fresh=Inventory(Path(self.tmp.name)/"demo.db")
        self.assertTrue(fresh.seed_demo())
        self.assertFalse(fresh.seed_demo())
        self.assertEqual(fresh.state()["mode"],"demo")
        with fresh.connect() as db:
            for row in db.execute("SELECT b.qty,SUM(m.delta) AS net FROM batches b JOIN movements m ON b.id=m.batch_id GROUP BY b.id"):
                self.assertEqual(row["qty"],row["net"])


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.inventory=Inventory(Path(cls.tmp.name)/"http.db")
        cls.server=make_server(cls.inventory,0)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.url=f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()
        cls.tmp.cleanup()

    def call(self,path,body=None,headers=None):
        req=urllib.request.Request(self.url+path,data=body,headers=headers or {})
        try:
            with urllib.request.urlopen(req,timeout=5) as response:
                return response.status,response.headers,response.read()
        except urllib.error.HTTPError as exc:
            return exc.code,exc.headers,exc.read()

    def test_pages_and_state(self):
        self.assertEqual(self.call('/')[0],200)
        self.assertEqual(self.call('/style.css')[0],200)
        code,headers,body=self.call('/api/state')
        self.assertEqual(code,200)
        self.assertEqual(json.loads(body)["products"],[])
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])

    def test_token_and_origin_protection(self):
        self.assertEqual(self.call('/api/products',b'{}',{'Content-Type':'application/json'})[0],403)
        self.assertEqual(self.call('/api/products',b'{}',{'Content-Type':'application/json','X-App-Token':self.server.token,'Origin':'https://example.org'})[0],403)
        self.assertEqual(self.call('/api/state',headers={'Host':'evil.example'})[0],403)

    def test_malformed_body_and_unknown_path(self):
        headers={'Content-Type':'application/json','X-App-Token':self.server.token}
        self.assertEqual(self.call('/api/products',b'{',headers)[0],400)
        self.assertEqual(self.call('/api/products',b'[]',headers)[0],400)
        self.assertEqual(self.call('/../app.py')[0],404)

    def test_backup_is_valid_sqlite(self):
        code,headers,body=self.call('/api/backup')
        self.assertEqual(code,200)
        path=Path(self.tmp.name)/'backup.db'
        path.write_bytes(body)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')


if __name__=='__main__':
    unittest.main(verbosity=2)
