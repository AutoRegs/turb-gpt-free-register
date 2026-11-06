# -*- coding: utf-8 -*-
import copy
import json
import unittest
from unittest.mock import MagicMock, patch

from core import db, payment_service
from core.extract_link_service import _run_extract
from webui.app import create_app


class FoargeClientTests(unittest.TestCase):
    def test_pbk_headers(self):
        client = payment_service.FoargeClient(
            base_url="https://foarge.com/api/publisher/v1",
            auth_mode="pbk",
            pbk_key="PBK-TEST-1234-5678",
        )
        headers = client._headers()
        self.assertEqual(headers["X-Publisher-Cdk"], "PBK-TEST-1234-5678")
        self.assertEqual(headers["Authorization"], "Bearer PBK-TEST-1234-5678")

    def test_api_key_headers(self):
        client = payment_service.FoargeClient(
            base_url="https://foarge.com/api/publisher/v1",
            auth_mode="api_key",
            api_key="pk_live_test_key",
        )
        headers = client._headers()
        self.assertEqual(headers["X-Api-Key"], "pk_live_test_key")
        self.assertEqual(headers["Authorization"], "Bearer pk_live_test_key")

    def test_missing_credentials_raises(self):
        client = payment_service.FoargeClient(
            base_url="https://foarge.com/api/publisher/v1",
            auth_mode="pbk",
            pbk_key="",
            api_key="",
        )
        with self.assertRaises(ValueError):
            client._headers()

    @patch.object(payment_service.FoargeClient, "_request")
    def test_create_task_payload(self, mock_req):
        mock_req.return_value = {"ok": True, "task_id": "tsk_123"}
        client = payment_service.FoargeClient(pbk_key="PBK-TEST")
        res = client.create_task(
            email="buyer@test.local",
            payment_method="upi",
            markup_cny=2.5,
            preferred_worker_ids="w_1, w_2",
            fallback_sec=30,
            external_ref="acc_99",
        )
        self.assertEqual(res["task_id"], "tsk_123")
        mock_req.assert_called_once()
        method, path, body = mock_req.call_args[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/tasks")
        self.assertEqual(body["account_email"], "buyer@test.local")
        self.assertEqual(body["payment_method"], "upi")
        self.assertEqual(body["markup_cny"], 2.5)
        self.assertEqual(body["preferred_worker_ids"], ["w_1", "w_2"])
        self.assertEqual(body["fallback_sec"], 30)
        self.assertEqual(body["external_ref"], "acc_99")

    @patch.object(payment_service.FoargeClient, "_request")
    def test_submit_checkout_payload(self, mock_req):
        mock_req.return_value = {"ok": True, "task": {"id": "tsk_123", "status": "pending"}}
        client = payment_service.FoargeClient(pbk_key="PBK-TEST")
        res = client.submit_checkout(
            task_id="tsk_123",
            access_token="at_xyz",
            pay_link="https://checkout.stripe.com/c/pay/cs_live_abc",
        )
        self.assertTrue(res["ok"])
        method, path, body = mock_req.call_args[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/tasks/tsk_123/submit-checkout")
        self.assertEqual(body["checkout_data"]["access_token"], "at_xyz")
        self.assertEqual(body["checkout_data"]["pay_link"], "https://checkout.stripe.com/c/pay/cs_live_abc")
        self.assertEqual(body["checkout_data"]["upi_redirect_url"], "https://checkout.stripe.com/c/pay/cs_live_abc")
        self.assertEqual(body["checkout_data"]["brcode"], "https://checkout.stripe.com/c/pay/cs_live_abc")


class PaymentServiceExecutionTests(unittest.TestCase):
    def test_payment_success_and_plan_upgrade(self):
        row = {
            "id": 101,
            "email": "auto_pay_user@test.local",
            "access_token": "token_101",
            "plan_type": "free",
            "current_plan_type": "free",
            "plus_trial_eligible": True,
            "extract_link_status": "success",
            "extract_link_long_url": "https://checkout.stripe.com/c/pay/test_link",
            "extract_link_payment_method": "upi",
            "payment_status": "queued",
        }
        rows = [row]

        with patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "get_account", side_effect=lambda aid: next((r for r in rows if int(r.get("id") or 0) == int(aid)), None)), \
             patch.object(db, "_save_accounts"), \
             patch("core.payment_service.FoargeClient") as mock_client_cls, \
             patch("core.plan_check_service.enqueue_account_plan_check") as mock_enqueue_plan, \
             patch("core.payment_service.time.sleep", return_value=None):

            mock_client = mock_client_cls.return_value
            mock_client.create_task.return_value = {"ok": True, "task_id": "tsk_999"}
            mock_client.submit_checkout.return_value = {"ok": True, "status": "pending"}
            # 轮询第一次为 assigned，第二次 completed
            mock_client.get_task.side_effect = [
                {"task": {"id": "tsk_999", "status": "assigned", "worker_name": "专员01"}},
                {"task": {"id": "tsk_999", "status": "completed", "paid_amount": 19.99}},
            ]

            result = payment_service._run_account_payment(
                account_id=101,
                email="auto_pay_user@test.local",
                access_token="token_101",
                channel="foarge",
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["task_id"], "tsk_999")
            # 验证账号已自动升级为 plus
            self.assertEqual(row["payment_status"], "success")
            self.assertTrue(row["payment_ok"])
            self.assertEqual(row["plan_type"], "plus")
            self.assertEqual(row["current_plan_type"], "plus")
            self.assertFalse(row["plus_trial_eligible"])
            mock_enqueue_plan.assert_called_once_with(
                account_id=101,
                email="auto_pay_user@test.local",
                access_token="token_101",
                trigger="payment_success_auto",
            )

    def test_payment_fails_when_no_extract_link(self):
        row = {
            "id": 102,
            "email": "no_link@test.local",
            "access_token": "token_102",
            "plan_type": "free",
            "payment_status": "queued",
        }
        rows = [row]

        with patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "get_account", side_effect=lambda aid: next((r for r in rows if int(r.get("id") or 0) == int(aid)), None)), \
             patch.object(db, "_save_accounts"):
            result = payment_service._run_account_payment(
                account_id=102,
                email="no_link@test.local",
                access_token="token_102",
                channel="foarge",
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "failed")
            self.assertIn("尚未成功提链", result["error"])
            self.assertEqual(row["payment_status"], "failed")
    def test_payment_fails_when_extract_link_expired(self):
        row = {
            "id": 108,
            "email": "expired_link@test.local",
            "access_token": "token_108",
            "plan_type": "free",
            "extract_link_long_url": "https://checkout.stripe.com/c/pay/test_link",
            "extract_link_expires_at": "2020-01-01T00:00:00Z",
            "payment_status": "queued",
        }
        rows = [row]
        with patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "get_account", return_value=row), \
             patch.object(db, "_save_accounts"):
            result = payment_service._run_account_payment(
                account_id=108,
                email="expired_link@test.local",
                access_token="token_108",
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "failed")
            self.assertIn("已过期", result["error"])
            self.assertFalse(row["payment_ok"])

    def test_payment_fails_when_worker_fails(self):
        row = {
            "id": 103,
            "email": "worker_fail@test.local",
            "access_token": "token_103",
            "extract_link_long_url": "https://checkout.stripe.com/c/pay/test_link",
            "payment_status": "queued",
        }
        rows = [row]

        with patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "get_account", side_effect=lambda aid: next((r for r in rows if int(r.get("id") or 0) == int(aid)), None)), \
             patch.object(db, "_save_accounts"), \
             patch("core.payment_service.FoargeClient") as mock_client_cls, \
             patch("core.payment_service.time.sleep", return_value=None):

            mock_client = mock_client_cls.return_value
            mock_client.create_task.return_value = {"task_id": "tsk_fail_1"}
            mock_client.submit_checkout.return_value = {"ok": True}
            mock_client.get_task.return_value = {
                "task": {"id": "tsk_fail_1", "status": "failed", "error": "银行卡余额不足或拒付"}
            }

            result = payment_service._run_account_payment(
                account_id=103,
                email="worker_fail@test.local",
                access_token="token_103",
                channel="foarge",
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "failed")
            self.assertIn("银行卡余额不足或拒付", result["error"])
            self.assertEqual(row["payment_status"], "failed")

    def test_enqueue_payment_busy_protection(self):
        with patch.object(db, "claim_account_payment", return_value=False):
            res = payment_service.enqueue_account_payment(
                account_id=104,
                email="busy@test.local",
            )
            self.assertFalse(res["accepted"])
            self.assertTrue(res["busy"])

    def test_payment_creates_unique_external_ref_per_attempt(self):
        row = {
            "id": 105,
            "email": "unique_ref@test.local",
            "access_token": "token_105",
            "extract_link_long_url": "https://checkout.stripe.com/c/pay/test_link",
            "payment_status": "queued",
        }
        rows = [row]
        with patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "get_account", return_value=row), \
             patch.object(db, "_save_accounts"), \
             patch("core.payment_service.FoargeClient") as mock_client_cls, \
             patch("core.plan_check_service.enqueue_account_plan_check"), \
             patch("core.payment_service.time.sleep", return_value=None):
            mock_client = mock_client_cls.return_value
            mock_client.create_task.return_value = {"ok": True, "task_id": "tsk_1"}
            mock_client.submit_checkout.return_value = {"ok": True}
            mock_client.get_task.return_value = {"task": {"id": "tsk_1", "status": "completed"}}

            payment_service._run_account_payment(account_id=105, email="unique_ref@test.local", access_token="token_105")
            ref1 = mock_client.create_task.call_args_list[0].kwargs["external_ref"]

            row["payment_status"] = "queued"
            mock_client.create_task.return_value = {"ok": True, "task_id": "tsk_2"}
            payment_service._run_account_payment(account_id=105, email="unique_ref@test.local", access_token="token_105")
            ref2 = mock_client.create_task.call_args_list[1].kwargs["external_ref"]

            self.assertTrue(ref1.startswith("acc_105_"))
            self.assertTrue(ref2.startswith("acc_105_"))
            self.assertNotEqual(ref1, ref2)

    def test_claim_account_payment_resets_task_id(self):
        row = {
            "id": 106,
            "email": "reset_task@test.local",
            "payment_status": "failed",
            "payment_task_id": "t_old_12345",
        }
        rows = [row]
        with patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "_save_accounts"):
            claimed = db.claim_account_payment(106)
            self.assertTrue(claimed)
            self.assertEqual(row["payment_status"], "queued")
            self.assertIsNone(row["payment_task_id"])

    def test_list_account_plan_check_statuses_revision_includes_payment(self):
        row1 = {
            "id": 107,
            "email": "rev@test.local",
            "updated_at": "2026-10-28T10:00:00",
            "payment_status": "queued",
            "payment_message": "排队中",
        }
        row2 = copy.deepcopy(row1)
        row2["payment_status"] = "failed"
        row2["payment_message"] = "Foarge API 错误 (400): invalid_status"

        with patch.object(db, "_query_collection_page", return_value=([row1], 1, 107)):
            res1 = db.list_account_plan_check_statuses()
            rev1 = res1.get("revision")

        with patch.object(db, "_query_collection_page", return_value=([row2], 1, 107)):
            res2 = db.list_account_plan_check_statuses()
            rev2 = res2.get("revision")

        self.assertNotEqual(rev1, rev2)

class AutoPayAfterExtractTests(unittest.TestCase):
    def test_auto_pay_triggered_when_enabled(self):
        events = [
            ("info", {"message": "正在提链..."}),
            ("result", {
                "result": {
                    "long_url": "https://checkout.stripe.com/c/pay/extracted_url",
                    "payment_method": "upi",
                }
            }),
            ("done", {}),
        ]

        with patch.object(db, "mark_account_extract_running", return_value=True), \
             patch("core.extract_link_service._create_extract_job", return_value={"job_id": "job_1"}), \
             patch("core.extract_link_service._iter_sse_events", return_value=events), \
             patch.object(db, "update_account_extract"), \
             patch("core.payment_service.is_auto_pay_enabled", return_value=True), \
             patch("core.payment_service.enqueue_account_payment") as mock_enqueue:
            mock_enqueue.return_value = {"accepted": True}
            result = _run_extract(
                account_id=201,
                email="auto_user@test.local",
                access_token="at_201",
                link_type="upi",
                cdk="cdk_test",
                trigger="manual",
            )

            self.assertTrue(result["ok"])
            mock_enqueue.assert_called_once_with(
                account_id=201,
                email="auto_user@test.local",
                access_token="at_201",
                pay_link="https://checkout.stripe.com/c/pay/extracted_url",
                payment_method="upi",
                trigger="auto_after_extract",
            )

    def test_auto_pay_not_triggered_when_disabled(self):
        events = [
            ("result", {
                "result": {
                    "long_url": "https://checkout.stripe.com/c/pay/extracted_url",
                }
            }),
            ("done", {}),
        ]

        with patch.object(db, "mark_account_extract_running", return_value=True), \
             patch("core.extract_link_service._create_extract_job", return_value={"job_id": "job_2"}), \
             patch("core.extract_link_service._iter_sse_events", return_value=events), \
             patch.object(db, "update_account_extract"), \
             patch("core.payment_service.is_auto_pay_enabled", return_value=False), \
             patch("core.payment_service.enqueue_account_payment") as mock_enqueue:

            result = _run_extract(
                account_id=202,
                email="manual_user@test.local",
                access_token="at_202",
                link_type="upi",
                cdk="cdk_test",
                trigger="manual",
            )

            self.assertTrue(result["ok"])
            mock_enqueue.assert_not_called()


class WebuiPaymentEndpointsTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch.object(payment_service.FoargeClient, "query_cdk")
    def test_query_cdk_endpoint(self, mock_query):
        mock_query.return_value = {"cdk": "PBK-1234", "remaining_count": 5}
        resp = self.client.get("/api/payment/foarge/cdk?code=PBK-1234")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["remaining_count"], 5)

    def test_single_account_payment_without_link(self):
        with patch.object(db, "get_account", return_value={"id": 301, "email": "a@b.c", "access_token": "tk"}):
            resp = self.client.post("/api/accounts/payment", json={"account_id": 301})
            self.assertEqual(resp.status_code, 400)
            data = resp.get_json()
            self.assertIn("缺少支付链接", data["error"])

    @patch("core.payment_service.enqueue_account_payment")
    def test_single_account_payment_success(self, mock_enqueue):
        mock_enqueue.return_value = {"accepted": True, "busy": False}
        with patch.object(db, "get_account", return_value={
            "id": 302,
            "email": "pay@test.local",
            "access_token": "valid_token",
            "extract_link_long_url": "https://stripe.com/pay_link",
        }):
            resp = self.client.post("/api/accounts/payment", json={"account_id": 302})
            self.assertEqual(resp.status_code, 202)
            data = resp.get_json()
            self.assertTrue(data["ok"])
            self.assertTrue(data["started"])

    @patch("core.payment_service.enqueue_account_payment")
    def test_bulk_payment_endpoint(self, mock_enqueue):
        mock_enqueue.return_value = {"accepted": True, "busy": False}
        accounts_map = {
            401: {"id": 401, "email": "p1@t.l", "access_token": "tk1", "extract_link_long_url": "https://link1"},
            402: {"id": 402, "email": "p2@t.l", "access_token": "tk2", "extract_link_copy_paste": "pix_code"},
            403: {"id": 403, "email": "p3@t.l", "access_token": "tk3"}, # 缺少链接
        }
        with patch.object(db, "get_account", side_effect=lambda aid: accounts_map.get(aid)):
            resp = self.client.post("/api/accounts/payment-bulk", json={"account_ids": [401, 402, 403]})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["started_count"], 2)
            self.assertEqual(data["failed_count"], 1)


if __name__ == "__main__":
    unittest.main()
