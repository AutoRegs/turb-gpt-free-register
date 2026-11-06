# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import MagicMock, patch

from core import db
import core.plan_check_service as plan_srv
import core.live_check_service as live_srv
import core.extract_link_service as extract_srv
import core.payment_service as payment_srv
import core.twofa_service as twofa_srv
import core.email_change_service as email_change_srv
from webui.app import _compact_account_for_list, create_app


class AccountSoldStatusTests(unittest.TestCase):
    def test_compact_account_includes_status(self):
        row = {"id": 1, "email": "test@example.com", "status": "sold"}
        compact = _compact_account_for_list(row)
        self.assertEqual(compact["status"], "sold")

    def test_matches_status_filter_sold_and_unsold(self):
        sold_row = {"status": "sold"}
        normal_row = {"status": ""}
        none_row = {"status": None}

        # sold filter
        self.assertTrue(db._matches_codex_status_filter(sold_row, "sold"))
        self.assertFalse(db._matches_codex_status_filter(normal_row, "sold"))
        self.assertFalse(db._matches_codex_status_filter(none_row, "sold"))

        # unsold filter
        self.assertFalse(db._matches_codex_status_filter(sold_row, "unsold"))
        self.assertTrue(db._matches_codex_status_filter(normal_row, "unsold"))
        self.assertTrue(db._matches_codex_status_filter(none_row, "unsold"))

    def test_account_filter_sql_sold_and_unsold(self):
        where, params = db._account_filter_sql(codex_filter="sold")
        self.assertTrue(any("status" in w for w in where))
        self.assertIn("sold", params)

        where_unsold, params_unsold = db._account_filter_sql(codex_filter="unsold")
        self.assertTrue(any("status" in w for w in where_unsold))

    def test_claim_account_rejects_sold(self):
        sold_row = {"id": 10, "email": "sold@test.com", "status": "sold"}
        with patch.object(db, "_account_for_update") as mock_update, patch.object(
            db, "_load_accounts", return_value=[sold_row]
        ):
            mock_update.return_value.__enter__.return_value = sold_row
            # All claims must reject sold account
            self.assertFalse(db.claim_account_plan_check(10))
            self.assertFalse(db.claim_account_live_check(10))
            self.assertFalse(db.claim_account_extract(10))
            self.assertFalse(db.claim_account_payment(10))
            self.assertFalse(db.claim_account_email_change(10, source="remail"))
            self.assertFalse(db.claim_account_totp_setup(10))
            self.assertFalse(db.claim_account_codex_agent(10))

    def test_services_enqueue_reject_sold_account(self):
        sold_account = {"id": 10, "email": "sold@test.com", "status": "sold", "access_token": "token123"}
        with patch.object(db, "get_account", return_value=sold_account):
            # Plan check service
            res_plan = plan_srv.enqueue_account_plan_check(
                account_id=10, email="sold@test.com", access_token="token123", trigger="manual"
            )
            self.assertFalse(res_plan.get("accepted"))
            self.assertIn("已出售", res_plan.get("error", ""))

            # Live check service
            res_live = live_srv.enqueue_account_live_check(account_id=10, email="sold@test.com")
            self.assertFalse(res_live.get("accepted"))
            self.assertIn("已出售", res_live.get("error", ""))

            # Extract link service
            res_extract = extract_srv.enqueue_account_extract(
                account_id=10, email="sold@test.com", access_token="token123"
            )
            self.assertFalse(res_extract.get("accepted"))
            self.assertIn("已出售", res_extract.get("error", ""))

            # Payment service
            res_pay = payment_srv.enqueue_account_payment(
                account_id=10, email="sold@test.com", access_token="token123"
            )
            self.assertFalse(res_pay.get("accepted"))
            self.assertIn("已出售", res_pay.get("error", ""))

            # 2FA service
            res_twofa = twofa_srv.enqueue_account_totp_setup(
                account_id=10, email="sold@test.com", access_token="token123"
            )
            self.assertFalse(res_twofa.get("accepted"))
            self.assertIn("已出售", res_twofa.get("error", ""))

            # Email change service
            res_email = email_change_srv.enqueue(account_id=10, source="remail")
            self.assertFalse(res_email.get("accepted"))
            self.assertIn("已出售", res_email.get("error", ""))

    def test_api_mark_sold_endpoints(self):
        app = create_app(auth_code="test-auth")
        app.config["TESTING"] = True
        client = app.test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

        with patch.object(db, "mark_accounts_sold", return_value=1) as mock_sold, patch.object(
            db, "get_account", return_value={"id": 1, "status": "sold"}
        ):
            # 1. Single mark sold
            resp = client.post("/api/accounts/1/sold", json={"sold": True})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["ok"])
            self.assertTrue(data["sold"])
            mock_sold.assert_called_with(account_ids=[1], sold=True)

            # 2. Bulk mark sold
            resp_bulk = client.post("/api/accounts/sold-bulk", json={"account_ids": [1, 2], "sold": False})
            self.assertEqual(resp_bulk.status_code, 200)
            data_bulk = resp_bulk.get_json()
            self.assertTrue(data_bulk["ok"])
            self.assertFalse(data_bulk["sold"])
            mock_sold.assert_called_with(account_ids=[1, 2], sold=False)

    def test_api_check_plan_bulk_processes_all_selected_accounts(self):
        app = create_app(auth_code="test-auth")
        app.config["TESTING"] = True
        client = app.test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

        acc1 = {"id": 1, "email": "a1@test.com", "access_token": "token1", "status": "active"}
        acc2 = {"id": 2, "email": "a2@test.com", "access_token": "token2", "status": "active"}
        acc3 = {"id": 3, "email": "a3@test.com", "access_token": "token3", "status": "active"}
        acc_sold = {"id": 4, "email": "sold@test.com", "access_token": "token4", "status": "sold"}

        def get_account_mock(aid):
            return {1: acc1, 2: acc2, 3: acc3, 4: acc_sold}.get(aid)

        with patch.object(db, "get_account", side_effect=get_account_mock), \
             patch.object(plan_srv, "enqueue_account_plan_check", return_value={"accepted": True, "busy": False}) as mock_enqueue:
            resp = client.post("/api/accounts/check-plan-bulk", json={"account_ids": [1, 2, 3, 4]})
            self.assertEqual(resp.status_code, 202)
            data = resp.get_json()
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("started_count"), 3)
            self.assertEqual(len(data.get("started", [])), 3)
            self.assertEqual(mock_enqueue.call_count, 3)
            # Verify sold account was skipped
            skipped = data.get("skipped", [])
            self.assertTrue(any(s.get("id") == 4 and "已出售" in s.get("reason", "") for s in skipped))


if __name__ == "__main__":
    unittest.main()
