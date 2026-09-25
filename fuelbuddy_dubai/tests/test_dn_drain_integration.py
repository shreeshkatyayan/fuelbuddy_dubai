"""
End-to-end drain tests on a live ERPNext site (IDEV-3156 anchor, IDEV-3266 run key).

The pure anchor rule is covered by test_dn_drain_anchor.py; these tests prove
the DB shell around it: a drain over a LATER window adopts an EARLIER pending
repost's anchor, sees based_on=Transaction reposts, defers instead of walking
past anchor_floor, and stays off the run key while an amend holds it.

Needs an ERPNext company (setup wizard done). Each test uses its own item and
warehouse so the reposts of one test never become another test's anchors.

    bench --site <site> run-tests --module fuelbuddy_dubai.tests.test_dn_drain_integration
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import getdate

from fuelbuddy_dubai.api import dn_drain, shadow_bin

RATE = 3.5


class TestDrainRepostAnchor(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = frappe.db.get_single_value("Global Defaults", "default_company")
        if not cls.company:
            raise __import__("unittest").SkipTest("needs an ERPNext company (run the setup wizard)")
        cls.abbr = frappe.get_cached_value("Company", cls.company, "abbr")
        # Production behaviour: a submitted repost is only Queued, never run on submit.
        frappe.flags.dont_execute_stock_reposts = True
        cls.customer = _ensure_customer("QC Drain Test Customer")

    def setUp(self):
        self.r = shadow_bin._raw_redis()
        if self.r.get(shadow_bin.RUN_KEY):
            self.skipTest(f"run key held by a real run: {self.r.get(shadow_bin.RUN_KEY)}")
        tag = frappe.generate_hash(length=6).upper()
        self.item = _ensure_item(f"QC-DRAIN-{tag}")
        self.wh = _ensure_warehouse(f"QC Drain {tag}", self.company)
        _receive(self.item, self.wh, self.company, 100_000, "2026-01-02")
        frappe.db.commit()

    def tearDown(self):
        self.r.delete(shadow_bin.RUN_KEY)

    # ---- helpers --------------------------------------------------------------
    def _draft(self, posting_date, qty=100):
        dn = frappe.get_doc(
            {
                "doctype": "Delivery Note",
                "company": self.company,
                "customer": self.customer,
                "posting_date": posting_date,
                "posting_time": "10:00:00",
                "set_posting_time": 1,
                "items": [
                    {"item_code": self.item, "qty": qty, "rate": RATE, "warehouse": self.wh, "uom": "Litre"}
                ],
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()
        return dn

    def _queued_riv(self, posting_date, **extra):
        riv = frappe.get_doc(
            {
                "doctype": "Repost Item Valuation",
                "company": self.company,
                "posting_date": posting_date,
                "posting_time": "00:00:00",
                **(extra or {"based_on": "Item and Warehouse", "item_code": self.item, "warehouse": self.wh}),
            }
        ).insert(ignore_permissions=True)
        riv.submit()
        frappe.db.commit()
        self.assertEqual(riv.status, "Queued")
        return riv

    def _drain(self, from_date, to_date, **kw):
        return dn_drain.drain(
            customer=self.customer, from_date=from_date, to_date=to_date,
            item_code=self.item, warehouse=self.wh, **kw,
        )

    # ---- tests ----------------------------------------------------------------
    def test_drain_adopts_an_earlier_pending_anchor(self):
        d1, d2 = self._draft("2026-08-10"), self._draft("2026-08-11")
        pending = self._queued_riv("2026-06-09")

        result = self._drain("2026-08-10", "2026-08-11")

        self.assertEqual(result["stage"], "complete", result.get("error"))
        self.assertEqual(result["submitted_count"], 2)
        self.assertEqual(result["repost_anchor"], "2026-06-09")
        self.assertEqual(result["repost_anchor_batch_earliest"], "2026-08-10")
        self.assertTrue(result["repost_anchor_inherited"])
        self.assertIn(pending.name, result["riv_parked_pre"])
        self.assertEqual(frappe.db.get_value("Repost Item Valuation", pending.name, "status"), "Skipped")
        consolidated = frappe.get_doc("Repost Item Valuation", result["consolidated_repost"])
        self.assertEqual(getdate(consolidated.posting_date), getdate("2026-06-09"))
        self.assertEqual(consolidated.status, "Completed")
        self.assertTrue(result["repost_ran_inline"])
        for dn in (d1, d2):
            self.assertEqual(frappe.db.get_value("Delivery Note", dn.name, "docstatus"), 1)
        self.assertIsNone(self.r.get(shadow_bin.RUN_KEY), "drain must release the run key")

    def test_drain_without_pending_reposts_keeps_its_own_anchor(self):
        self._draft("2026-08-12")
        result = self._drain("2026-08-12", "2026-08-12")
        self.assertEqual(result["stage"], "complete", result.get("error"))
        self.assertEqual(result["repost_anchor"], "2026-08-12")
        self.assertFalse(result["repost_anchor_inherited"])

    def test_transaction_based_repost_is_seen_and_adopted(self):
        """based_on=Transaction rows leave item_code/warehouse NULL; the quiesce
        query resolves them through the stock ledger."""
        early = self._draft("2026-06-15")
        early.submit()
        frappe.db.commit()
        pending = self._queued_riv(
            "2026-06-15", based_on="Transaction", voucher_type="Delivery Note", voucher_no=early.name
        )
        self._draft("2026-08-20")

        result = self._drain("2026-08-20", "2026-08-20")

        self.assertEqual(result["stage"], "complete", result.get("error"))
        self.assertIn(pending.name, result["riv_parked_pre"])
        self.assertEqual(result["repost_anchor"], "2026-06-15")

    def test_anchor_floor_defers_instead_of_walking(self):
        self._draft("2026-08-21")
        self._queued_riv("2026-02-01")

        result = self._drain("2026-08-21", "2026-08-21", anchor_floor="2026-06-01")

        self.assertEqual(result["stage"], "repost_deferred_anchor_too_early")
        self.assertEqual(result["repost_anchor"], "2026-02-01")
        deferred = frappe.get_doc("Repost Item Valuation", result["consolidated_repost"])
        self.assertEqual(deferred.status, "Queued")
        self.assertEqual(getdate(deferred.posting_date), getdate("2026-02-01"))
        self.assertIsNone(self.r.get(shadow_bin.RUN_KEY))

    def test_drain_stays_off_while_an_amend_holds_the_run_key(self):
        draft = self._draft("2026-08-22")
        self.assertTrue(shadow_bin.acquire_run_key("qc-amend:episode-x:1234", 60))

        result = self._drain("2026-08-22", "2026-08-22")

        self.assertEqual(result["stage"], "exception")
        self.assertIn("qc-amend:episode-x:1234", result["error"])
        self.assertEqual(frappe.db.get_value("Delivery Note", draft.name, "docstatus"), 0)
        # the failed drain's cleanup must not release the amend's key
        self.assertEqual(self.r.get(shadow_bin.RUN_KEY), "qc-amend:episode-x:1234")

    def test_drain_async_passes_anchor_floor_to_the_job(self):
        with patch.object(frappe, "enqueue") as enqueue:
            dn_drain.drain_async(from_date="2026-08-01", to_date="2026-08-31", min_age_minutes=15,
                                 anchor_floor="2026-06-01")
        kwargs = enqueue.call_args.kwargs["drain_kwargs"]
        self.assertEqual(kwargs["anchor_floor"], "2026-06-01")
        self.assertEqual(kwargs["min_age_minutes"], 15)
        with patch.object(dn_drain, "drain", return_value={"stage": "empty"}) as drain:
            dn_drain._drain_job("fb_dn_drain_result:test", kwargs)
        self.assertEqual(drain.call_args.kwargs["anchor_floor"], "2026-06-01")


# ---- fixtures -------------------------------------------------------------------
def _ensure_customer(name):
    if not frappe.db.exists("Customer", name):
        customer = frappe.get_doc(
            {"doctype": "Customer", "customer_name": name, "customer_group": "All Customer Groups",
             "territory": "All Territories"}
        )
        # Only a party for draft DNs: mandatory fields other apps' fixtures add don't matter.
        customer.flags.ignore_mandatory = True
        customer.insert(ignore_permissions=True)
    return name


def _ensure_item(code):
    if not frappe.db.exists("Item", code):
        frappe.get_doc(
            {"doctype": "Item", "item_code": code, "item_name": code, "item_group": "All Item Groups",
             "stock_uom": "Litre", "is_stock_item": 1, "valuation_rate": 3.0}
        ).insert(ignore_permissions=True)
    return code


def _ensure_warehouse(name, company):
    abbr = frappe.get_cached_value("Company", company, "abbr")
    full = f"{name} - {abbr}"
    if not frappe.db.exists("Warehouse", full):
        frappe.get_doc({"doctype": "Warehouse", "warehouse_name": name, "company": company}).insert(
            ignore_permissions=True
        )
    return full


def _receive(item, wh, company, qty, posting_date):
    se = frappe.get_doc(
        {
            "doctype": "Stock Entry",
            "stock_entry_type": "Material Receipt",
            "company": company,
            "posting_date": posting_date,
            "posting_time": "00:30:00",
            "set_posting_time": 1,
            "items": [{"item_code": item, "qty": qty, "t_warehouse": wh, "basic_rate": 3.0}],
        }
    ).insert(ignore_permissions=True)
    se.submit()
    return se
