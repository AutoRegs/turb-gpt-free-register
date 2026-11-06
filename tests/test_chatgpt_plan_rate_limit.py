# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import MagicMock, patch

from core import db
import core.chatgpt_plan as plan
from webui.app import _compact_account_for_list


class ChatgptPlanRateLimitTests(unittest.TestCase):
    def test_format_rate_limit_label(self):
        self.assertEqual(plan.format_rate_limit_label(18000), "5小时限")
        self.assertEqual(plan.format_rate_limit_label(604800), "周限")
        self.assertEqual(plan.format_rate_limit_label(10800), "3小时限")
        self.assertEqual(plan.format_rate_limit_label(86400), "24小时限")
        self.assertEqual(plan.format_rate_limit_label(172800), "2天限")
        self.assertEqual(plan.format_rate_limit_label(3600), "1小时限")
        self.assertIsNone(plan.format_rate_limit_label(None))
        self.assertIsNone(plan.format_rate_limit_label(0))
        self.assertIsNone(plan.format_rate_limit_label(-100))

    def test_parse_rate_limit_payload_wham_usage_5hour(self):
        payload = {
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 0,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": 11135,
                    "reset_at": 1793176659,
                },
                "secondary_window": {
                    "used_percent": 0,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 597935,
                    "reset_at": 1793763459,
                },
            }
        }
        res = plan.parse_rate_limit_payload(payload)
        self.assertEqual(res["rate_limit_label"], "5小时限")
        self.assertEqual(res["rate_limit_window_seconds"], 18000)
        self.assertEqual(res["rate_limit_used_percent"], 0)
        self.assertEqual(res["rate_limit_reset_at"], 1793176659)
        self.assertEqual(res["rate_limit_reset_after_seconds"], 11135)
        self.assertEqual(res["rate_limit_raw"], payload["rate_limit"])

    def test_parse_rate_limit_payload_wham_usage_weekly(self):
        payload = {
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 13,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 436856,
                    "reset_at": 1793602399,
                },
                "secondary_window": None,
            }
        }
        res = plan.parse_rate_limit_payload(payload)
        self.assertEqual(res["rate_limit_label"], "周限")
        self.assertEqual(res["rate_limit_window_seconds"], 604800)
        self.assertEqual(res["rate_limit_used_percent"], 13)
        self.assertEqual(res["rate_limit_reset_at"], 1793602399)
        self.assertEqual(res["rate_limit_reset_after_seconds"], 436856)

    def test_parse_rate_limit_payload_models_fallback(self):
        payload = {
            "models": [
                {
                    "slug": "gpt-4o",
                    "rate_limit": {
                        "primary_window": {
                            "limit": 80,
                            "window_seconds": 18000,
                        }
                    },
                }
            ]
        }
        res = plan.parse_rate_limit_payload(payload)
        self.assertEqual(res["rate_limit_label"], "5小时限")
        self.assertEqual(res["rate_limit_window_seconds"], 18000)

    def test_fetch_plus_rate_limit_calls_wham_usage(self):
        env = MagicMock()
        env.get_chatgpt_headers.return_value = {"oai-device-id": "dev1"}
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "rate_limit": {
                "primary_window": {"limit_window_seconds": 18000, "used_percent": 5}
            }
        }
        env.get.return_value = resp

        res = plan.fetch_plus_rate_limit(env, "token123", {"account_id": "acc-1"})
        self.assertEqual(res["rate_limit_label"], "5小时限")
        self.assertEqual(res["rate_limit_used_percent"], 5)
        # 验证调用了 wham/usage
        call_url = env.get.call_args[0][0]
        self.assertIn("/backend-api/wham/usage", call_url)

    def test_fetch_plus_rate_limit_fallback_to_models_on_404(self):
        env = MagicMock()
        env.get_chatgpt_headers.return_value = {"oai-device-id": "dev1"}
        resp_404 = MagicMock()
        resp_404.status_code = 404
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            "models": [
                {"slug": "gpt-4o", "max_rate_limit": {"window_seconds": 604800}}
            ]
        }
        env.get.side_effect = [resp_404, resp_200]

        res = plan.fetch_plus_rate_limit(env, "token123", {"account_id": "acc-1"})
        self.assertEqual(res["rate_limit_label"], "周限")
        self.assertEqual(env.get.call_count, 2)

    def test_check_account_plan_skips_rate_limit_when_free(self):
        claims = {"payload": {}, "email": "free@test.com", "token_expired": False}
        free_data = {
            "accounts": {
                "default": {
                    "account": {"account_id": "acc-free", "plan_type": "free"},
                    "entitlement": {"subscription_plan": "chatgptfreeplan"},
                }
            }
        }
        session = MagicMock()
        session.get_chatgpt_headers.return_value = {}
        session.get.return_value = MagicMock(status_code=200, json=lambda: free_data, text="")

        with patch.object(plan, "token_claims", return_value=claims), patch.object(
            plan, "fetch_plus_rate_limit"
        ) as mock_fetch:
            res = plan.check_account_plan("token", session=session)

        self.assertTrue(res["ok"])
        self.assertEqual(res["current_plan_type"], "free")
        # 严格验证：Free 账号绝不查询速率限制接口
        mock_fetch.assert_not_called()
        self.assertNotIn("rate_limit_label", res)

    def test_check_account_plan_queries_rate_limit_when_plus(self):
        claims = {"payload": {}, "email": "plus@test.com", "token_expired": False}
        plus_data = {
            "accounts": {
                "default": {
                    "account": {"account_id": "acc-plus", "plan_type": "plus"},
                    "entitlement": {
                        "subscription_plan": "chatgptplusplan",
                        "billing_period": "monthly",
                        "billing_currency": "INR",
                    },
                }
            }
        }
        session = MagicMock()
        session.get_chatgpt_headers.return_value = {}
        session.get.return_value = MagicMock(status_code=200, json=lambda: plus_data, text="")

        with patch.object(plan, "token_claims", return_value=claims), patch.object(
            plan, "fetch_plus_rate_limit", return_value={"rate_limit_label": "5小时限", "rate_limit_window_seconds": 18000}
        ) as mock_fetch:
            res = plan.check_account_plan("token", session=session)

        self.assertTrue(res["ok"])
        self.assertEqual(res["current_plan_type"], "plus")
        # 验证：Plus 账号准确触发查询并合并字段
        mock_fetch.assert_called_once()
        self.assertEqual(res["rate_limit_label"], "5小时限")
        self.assertEqual(res["rate_limit_window_seconds"], 18000)

    def test_db_persistence_and_webui_compact(self):
        row = {"id": 99, "email": "plus@test.com"}
        with patch.object(db, "_account_for_update") as mock_ctx:
            mock_ctx.return_value.__enter__.return_value = row
            # 1. 保存 Plus 账号结果
            db.update_account_plan_check(
                acc_id=99,
                result={
                    "ok": True,
                    "current_plan_type": "plus",
                    "rate_limit_label": "周限",
                    "rate_limit_window_seconds": 604800,
                    "rate_limit_used_percent": 13,
                },
            )
            self.assertEqual(row["rate_limit_label"], "周限")
            self.assertEqual(row["rate_limit_window_seconds"], 604800)
            self.assertEqual(row["rate_limit_used_percent"], 13)

            # 2. webui compact 包含该字段
            compact = _compact_account_for_list(row)
            self.assertEqual(compact["rate_limit_label"], "周限")
            self.assertEqual(compact["rate_limit_window_seconds"], 604800)
            self.assertEqual(compact["rate_limit_used_percent"], 13)

            # 3. 账号变为 Free 时自动清除速率限制字段
            db.update_account_plan_check(
                acc_id=99,
                result={
                    "ok": True,
                    "current_plan_type": "free",
                },
            )
            self.assertNotIn("rate_limit_label", row)
            self.assertNotIn("rate_limit_window_seconds", row)


if __name__ == "__main__":
    unittest.main()
