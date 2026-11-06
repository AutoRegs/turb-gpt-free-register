# -*- coding: utf-8 -*-
"""查活协议优先、连续失败阈值与持久化重置的离线回归测试。"""
import hashlib
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from config import proxy as proxy_cfg, roxybrowser
from core import account_browser_login as browser_login
from core import account_liveness as liveness, db, live_check_service as service
from core import roxy_registration as pages
from core.roxybrowser_client import RoxyBrowserClient, RoxyProfileMissingError


class LiveCheckFallbackTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        missing = root / "missing"
        stack.enter_context(patch.multiple(db, **{
            "_ACCOUNTS_JSON": root / "accounts.json", "_OUTLOOK_JSON": root / "outlook.json",
            "_GENERIC_API_EMAIL_JSON": root / "generic.json", "_DOMAIN_EMAIL_JSON": root / "domain.json",
            "_JOBS_JSON": root / "jobs.json", "_LEGACY_SQLITE": missing,
            "_LEGACY_ACCOUNTS_JSON": missing, "_LEGACY_OUTLOOK_JSON": missing, "_LEGACY_JOBS_JSON": missing,
            "_SQLITE_READY": False, "_SQLITE_READY_PATH": None,
        }))
        stack.enter_context(patch.object(liveness, "_LOG_DIR", root / "logs"))
        stack.enter_context(patch.object(roxybrowser, "REGISTRATION_DRIVER", "roxy"))
        stack.enter_context(patch.object(roxybrowser, "LIVE_CHECK_PROTOCOL_FAILURE_LIMIT", 4))
        stack.enter_context(patch.object(service, "_QUEUE_SLOTS", threading.BoundedSemaphore(1)))
        self.proxy = "socks5://dedicated.example:1080"
        stack.enter_context(patch.object(proxy_cfg, "PLAN_CHECK_PROXY", [self.proxy]))
        stack.enter_context(patch.object(proxy_cfg, "PLAN_CHECK_MAX_ATTEMPTS", 3))
        stack.enter_context(patch.object(service, "resolve_plan_check_route", side_effect=lambda explicit_proxy=None: {
            "proxy": explicit_proxy or self.proxy, "network_route": "proxy", "proxy_mode": "proxy_pool",
        }))
        self.open_proxy = stack.enter_context(patch.object(
            service, "open_plan_check_proxy", side_effect=lambda route, selected, **kw: (selected, None),
        ))
        stack.enter_context(patch.object(liveness, "_stored_access_token", return_value=""))
        self.protocol = stack.enter_context(patch.object(
            liveness, "_login_via_full_web_flow", side_effect=RuntimeError("HTTP 403 blocked"),
        ))
        self.real_browser_login = browser_login.login_with_browser
        self.browser = stack.enter_context(patch.object(browser_login, "login_with_browser", return_value={
            "ok": True, "status": "live", "access_token": "fresh-browser-token",
        }))
        self.account = db.upsert_imported_account(
            email="user@example.com", password="saved-password", totp_secret="JBSWY3DPEHPK3PXP", access_token="expired", session={},
        )
        self.account = db.get_account(self.account["id"])

    def run_check(self, account=None):
        account = db.get_account((account or self.account)["id"])
        self.assertTrue(service._QUEUE_SLOTS.acquire(blocking=False))
        self.assertTrue(db.claim_account_live_check(acc_id=account["id"], trigger="manual"))
        return service._run_live_check(account_id=account["id"], email=account["email"], proxy=self.proxy, trigger="manual")

    def failure_count(self, account=None):
        return db.get_account((account or self.account)["id"]).get("live_check_protocol_failures", 0)

    def protocol_success(self):
        self.protocol.side_effect = None
        session = MagicMock()
        session.proxy = self.proxy
        session.fingerprint_summary.return_value = {}
        session.fingerprint_summary_text.return_value = "test-fingerprint"
        self.protocol.return_value = session, {"accessToken": "fresh-protocol-token"}

    def test_first_three_failures_use_protocol_and_fourth_immediately_uses_roxy(self):
        for count in range(1, 4):
            self.assertFalse(self.run_check()["ok"])
            self.assertEqual(self.failure_count(), count)
            self.browser.assert_not_called()
        self.assertTrue(self.run_check()["ok"])
        self.assertEqual(self.protocol.call_count, 4)
        self.browser.assert_called_once_with(
            "user@example.com", "saved-password", "JBSWY3DPEHPK3PXP", proxy=self.proxy, driver_mode="roxy",
            allow_email_otp=True, email_source=None, purpose="live_check",
            account_id=self.account["id"],
        )
        self.assertEqual(self.failure_count(), 0)
        self.assertEqual(db.get_account(self.account["id"])["access_token"], "fresh-browser-token")
        self.assertFalse(self.run_check()["ok"])
        self.assertEqual(self.failure_count(), 1)
        self.assertEqual(self.browser.call_count, 1)

    def test_configurable_protocol_failure_limit(self):
        from config import roxybrowser as browser_cfg
        with patch.object(browser_cfg, "LIVE_CHECK_PROTOCOL_FAILURE_LIMIT", 2):
            self.assertFalse(self.run_check()["ok"])
            self.assertEqual(self.failure_count(), 1)
            self.browser.assert_not_called()
            # 第二次失败达到阈值 2，立即切换指纹浏览器成功
            self.assertTrue(self.run_check()["ok"])
            self.assertEqual(self.protocol.call_count, 2)
            self.browser.assert_called_once()
            self.assertEqual(self.failure_count(), 0)
    def test_browser_failure_retains_threshold_across_storage_reinitialization(self):
        self.browser.return_value = {"ok": False, "status": "failed", "error": "browser login failed"}
        for _ in range(4):
            self.assertFalse(self.run_check()["ok"])
        self.assertEqual(self.failure_count(), 4)
        db._SQLITE_READY = False
        db._SQLITE_READY_PATH = None
        self.browser.return_value = {"ok": True, "status": "live", "access_token": "new-token"}
        self.assertTrue(self.run_check()["ok"])
        self.assertEqual(self.protocol.call_count, 4)
        self.assertEqual(self.browser.call_count, 2)
        self.assertEqual(self.failure_count(), 0)

    def test_protocol_success_clears_consecutive_failures(self):
        for _ in range(3):
            self.run_check()
        self.protocol_success()
        self.assertTrue(self.run_check()["ok"])
        self.assertEqual(self.failure_count(), 0)
        self.browser.assert_not_called()
        self.protocol.side_effect = RuntimeError("HTTP 403 blocked")
        self.assertFalse(self.run_check()["ok"])
        self.assertEqual(self.failure_count(), 1)

    def test_failure_counts_are_isolated_by_account(self):
        other = db.upsert_imported_account(email="other@example.com", password="other", totp_secret="JBSWY3DPEHPK3PXP", access_token="expired", session={})
        for _ in range(3):
            self.run_check()
        self.run_check(other)
        self.assertEqual(self.failure_count(), 3)
        self.assertEqual(self.failure_count(other), 1)
        self.run_check()
        self.assertEqual(self.failure_count(), 0)
        self.assertEqual(self.failure_count(other), 1)

    def test_deactivated_account_does_not_trigger_browser_fallback(self):
        for _ in range(3):
            self.run_check()
        self.protocol.side_effect = liveness.AccountUnusableError("account_deactivated")
        self.assertEqual(self.run_check()["status"], "deactivated")
        self.assertEqual(self.failure_count(), 3)
        self.browser.assert_not_called()

    def test_proxy_open_failure_does_not_count_as_login_failure(self):
        self.open_proxy.side_effect = RuntimeError("proxy unavailable")
        with self.assertLogs(service.logger, level="ERROR"):
            self.assertFalse(self.run_check()["ok"])
        self.assertEqual(self.failure_count(), 0)
        self.protocol.assert_not_called()
        self.browser.assert_not_called()

    def test_protocol_configuration_does_not_select_an_unconfigured_browser(self):
        with patch.object(roxybrowser, "REGISTRATION_DRIVER", "protocol"):
            for _ in range(5):
                self.assertFalse(self.run_check()["ok"])
        self.assertEqual(self.failure_count(), 5)
        self.browser.assert_not_called()
        self.assertIn("未配置指纹浏览器", liveness.log_path(self.account["email"]).read_text())

    def test_fourth_proxy_login_failure_falls_back_even_when_route_budget_is_exhausted(self):
        proxies = [self.proxy] + [f"socks5://proxy-{n}.example:1080" for n in range(3)]
        with patch.object(proxy_cfg, "PLAN_CHECK_PROXY", proxies), \
             patch.object(proxy_cfg, "PLAN_CHECK_MAX_ATTEMPTS", 4), \
             patch.object(service.random, "choice", side_effect=lambda values: values[0]):
            self.assertTrue(self.run_check()["ok"])
        self.assertEqual(self.protocol.call_count, 4)
        self.browser.assert_called_once()
        self.assertEqual(self.browser.call_args.kwargs["proxy"], proxies[-1])
        self.assertEqual(self.failure_count(), 0)

    def test_reauth_and_internal_protocol_fallback_count_as_one_login_failure(self):
        session = MagicMock()
        session.proxy = self.proxy
        with patch.object(liveness, "_stored_access_token", return_value="expired"), \
             patch.object(liveness, "_account_totp_secret", return_value=""), \
             patch.object(liveness, "_new_fingerprint_pinned_session", return_value=session), \
             patch.object(liveness, "_warm_authenticated_session"), \
             patch.object(liveness, "human_delay"), \
             patch.object(liveness, "_login_via_reauth", side_effect=RuntimeError("HTTP 403 callback")) as reauth:
            self.assertFalse(self.run_check()["ok"])
        reauth.assert_called_once()
        self.protocol.assert_called_once()
        self.assertEqual(self.failure_count(), 1)
        self.browser.assert_not_called()

    def save_registration_profile(self, email="user@example.com", profile_id="saved-profile"):
        return db.insert_account(email=email, access_token="expired", extra={
            "registration_password": "saved-password", "roxybrowser": {"profile_id": profile_id},
        })

    @contextmanager
    def roxy_environment(self):
        client, driver = MagicMock(), MagicMock()
        client.open_profile.return_value = SimpleNamespace(profile_id="saved-profile", raw={})
        self.browser.side_effect = self.real_browser_login
        with patch("core.roxybrowser_client.RoxyBrowserClient", return_value=client), \
             patch.object(roxybrowser, "ROXY_KEEP_BROWSER_OPEN", False), \
             patch.object(pages, "_build_driver", return_value=driver), \
             patch.object(browser_login, "_fingerprint", return_value={}), \
             patch.object(browser_login, "_login_on_page", return_value={
                 "accessToken": "browser-token", "user": {"email": self.account["email"]},
             }) as login:
            yield client, driver, login

    def test_fourth_failure_reuses_saved_registration_environment_and_retains_it(self):
        self.save_registration_profile()
        for _ in range(3):
            self.run_check()
        with self.roxy_environment() as (client, driver, login):
            result = self.run_check()
        self.assertTrue(result["ok"], result.get("error"))
        client.open_profile.assert_called_once_with("saved-profile", reuse=True, proxy=self.proxy)
        self.assertTrue(login.call_args.kwargs["reuse_session"])
        client.cleanup_profile.assert_called_once_with(client.open_profile.return_value, retain=True)
        driver.quit.assert_called_once()
        self.assertEqual(db.get_account(self.account["id"])["roxy_profile_id"], "saved-profile")
        self.assertEqual(result["fingerprint"]["lifecycle"], "account_profile_reused")
        self.assertEqual(self.failure_count(), 0)

    def test_deleted_old_environment_is_replaced_and_login_failure_keeps_new_binding(self):
        self.save_registration_profile()
        for _ in range(3):
            self.run_check()
        fresh = SimpleNamespace(profile_id="replacement-profile", raw={})
        with self.roxy_environment() as (client, driver, login):
            client.open_profile.side_effect = [RoxyProfileMissingError("不存在"), fresh, fresh]
            login.side_effect = RuntimeError("browser login failed")
            self.assertFalse(self.run_check()["ok"])
            self.assertEqual(self.failure_count(), 4)
            self.assertEqual(db.get_account(self.account["id"])["roxy_profile_id"], fresh.profile_id)
            db._SQLITE_READY = False
            db._SQLITE_READY_PATH = None
            login.side_effect = None
            self.assertTrue(self.run_check()["ok"])
        calls = client.open_profile.call_args_list
        self.assertEqual(calls[0].args, ("saved-profile",))
        self.assertEqual(calls[1].kwargs, {"fresh": True, "proxy": self.proxy})
        self.assertEqual(calls[2].args, (fresh.profile_id,))
        self.assertTrue(calls[2].kwargs["reuse"])
        self.assertIsNone(calls[2].kwargs["proxy"])
        self.assertTrue(all(call.kwargs["retain"] for call in client.cleanup_profile.call_args_list))
        self.assertEqual(self.protocol.call_count, 4)

    def test_temporary_roxy_error_does_not_create_a_replacement_profile(self):
        self.save_registration_profile()
        for _ in range(3):
            self.run_check()
        with self.roxy_environment() as (client, driver, login):
            client.open_profile.side_effect = RuntimeError("Roxy connection timed out")
            self.assertFalse(self.run_check()["ok"])
        client.open_profile.assert_called_once_with("saved-profile", reuse=True, proxy=self.proxy)
        login.assert_not_called()
        self.assertEqual(db.get_account_roxy_profile_id(self.account["id"], self.account["email"]), "saved-profile")

    def test_account_without_saved_environment_creates_and_binds_its_own_profile(self):
        for _ in range(3):
            self.run_check()
        with self.roxy_environment() as (client, driver, login):
            self.assertTrue(self.run_check()["ok"])
        client.open_profile.assert_called_once_with(fresh=True, proxy=self.proxy)
        self.assertEqual(db.get_account_roxy_profile_id(self.account["id"], self.account["email"]), "saved-profile")
        self.assertNotIn("reuse_session", login.call_args.kwargs)

    def test_shared_legacy_profile_is_not_reused_across_accounts(self):
        self.save_registration_profile()
        other_id = self.save_registration_profile(email="other@example.com")
        self.assertEqual(db.get_account_roxy_profile_id(self.account["id"], self.account["email"]), "")
        self.assertFalse(db.bind_account_roxy_profile(self.account["id"], self.account["email"], "saved-profile"))
        for _ in range(3):
            self.run_check()
        with self.roxy_environment() as (client, driver, login):
            client.open_profile.return_value = SimpleNamespace(profile_id="private-profile", raw={})
            self.assertTrue(self.run_check()["ok"])
        client.open_profile.assert_called_once_with(fresh=True, proxy=self.proxy)
        self.assertEqual(db.get_account_roxy_profile_id(other_id, "other@example.com"), "")
        self.assertEqual(db.get_account_roxy_profile_id(self.account["id"], self.account["email"]), "private-profile")

    def run_legacy_api_live_check(self, *, profile_exists=True, update_supported=True):
        """从真实 HTTP 错误处理一路覆盖查活队列、浏览器分支和 SQLite 写回。"""
        self.save_registration_profile()
        for _ in range(3):
            self.run_check()
        client = RoxyBrowserClient(token="")
        self.browser.side_effect = self.real_browser_login

        def response(method, url, **kwargs):
            path = urlparse(url).path
            status, payload = 200, {"code": 0}
            if path in {"/browser/detail", "/browser/list_v3"} or (path == "/browser/mdf" and not update_supported):
                status, payload = 404, {"code": 104, "msg": "Not Found", "data": None}
            elif path == "/browser/list":
                payload["data"] = {"total": int(profile_exists), "rows": [{"dirId": "saved-profile"}] if profile_exists else []}
            elif path == roxybrowser.ROXY_CREATE_PATH:
                payload["data"] = {"dirId": "replacement-profile"}
            elif path == roxybrowser.ROXY_OPEN_PATH:
                payload["data"] = {"debuggerAddress": "127.0.0.1:9222"}
            return SimpleNamespace(status_code=status, text=json.dumps(payload), json=lambda: payload)

        with patch("core.roxybrowser_client.RoxyBrowserClient", return_value=client), \
             patch.object(client.http, "request", side_effect=response) as http, \
             patch.object(roxybrowser, "ROXY_CREATE_INTERVAL", 0), \
             patch.object(roxybrowser, "ROXY_KEEP_BROWSER_OPEN", False), \
             patch.object(pages, "_build_driver", return_value=MagicMock()), \
             patch.object(browser_login, "_fingerprint", return_value={}), \
             patch.object(browser_login, "_login_on_page", return_value={
                 "accessToken": "refreshed-token", "user": {"email": self.account["email"]},
             }):
            result = self.run_check()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(self.failure_count(), 0)
        self.assertEqual(db.get_account(self.account["id"])["access_token"], "refreshed-token")
        return result, http.call_args_list

    def test_detail_http_404_does_not_stop_reuse_in_the_live_check_queue(self):
        result, calls = self.run_legacy_api_live_check()
        paths = [urlparse(call.args[1]).path for call in calls]
        self.assertIn("/browser/list", paths)
        self.assertNotIn(roxybrowser.ROXY_CREATE_PATH, paths)
        self.assertEqual(result["fingerprint"]["lifecycle"], "account_profile_reused")
        self.assertEqual(db.get_account(self.account["id"])["roxy_profile_id"], "saved-profile")

    def test_missing_profile_on_legacy_api_creates_and_binds_a_replacement_in_the_same_check(self):
        result, calls = self.run_legacy_api_live_check(profile_exists=False)
        paths = [urlparse(call.args[1]).path for call in calls]
        self.assertEqual(paths.count(roxybrowser.ROXY_CREATE_PATH), 1)
        self.assertNotIn(roxybrowser.ROXY_DELETE_PATH, paths)
        self.assertEqual(db.get_account(self.account["id"])["roxy_profile_id"], "replacement-profile")

    def test_unsupported_proxy_update_creates_a_replacement_without_stopping_the_check(self):
        result, calls = self.run_legacy_api_live_check(update_supported=False)
        paths = [urlparse(call.args[1]).path for call in calls]
        self.assertEqual(paths.count(roxybrowser.ROXY_CREATE_PATH), 1)
        self.assertNotIn(roxybrowser.ROXY_DELETE_PATH, paths)
        self.assertEqual(result["fingerprint"]["lifecycle"], "account_profile_created")
        self.assertEqual(db.get_account(self.account["id"])["roxy_profile_id"], "replacement-profile")

    def test_same_proxy_reuses_retained_profile_without_calling_unsupported_update(self):
        self.run_legacy_api_live_check(update_supported=False)
        with self.roxy_environment() as (client, driver, login):
            client.open_profile.return_value = SimpleNamespace(profile_id="replacement-profile", raw={})
            self.browser.return_value = None
            result = self.real_browser_login(
                self.account["email"], "saved-password", "JBSWY3DPEHPK3PXP", proxy=self.proxy,
                driver_mode="roxy", purpose="live_check", allow_email_otp=True, account_id=self.account["id"],
            )
        self.assertTrue(result["ok"], result.get("error"))
        client.open_profile.assert_called_once_with("replacement-profile", reuse=True, proxy=None)
        self.assertEqual(db.get_account(self.account["id"])["roxy_profile_proxy_key"], hashlib.sha256(self.proxy.encode()).hexdigest())

    def test_changed_proxy_still_updates_the_old_profile_before_reuse(self):
        self.run_legacy_api_live_check(update_supported=False)
        next_proxy = "socks5://different-country.example:1080"
        with self.roxy_environment() as (client, driver, login):
            client.open_profile.return_value = SimpleNamespace(profile_id="replacement-profile", raw={})
            self.real_browser_login(
                self.account["email"], "saved-password", "JBSWY3DPEHPK3PXP", proxy=next_proxy,
                driver_mode="roxy", purpose="live_check", allow_email_otp=True, account_id=self.account["id"],
            )
            # 尚未写回新的登录结果时，旧的成功记录不能覆盖最新出口摘要。
            self.real_browser_login(
                self.account["email"], "saved-password", "JBSWY3DPEHPK3PXP", proxy=self.proxy,
                driver_mode="roxy", purpose="live_check", allow_email_otp=True, account_id=self.account["id"],
            )
        self.assertEqual([call.kwargs["proxy"] for call in client.open_profile.call_args_list], [next_proxy, self.proxy])


if __name__ == "__main__":
    unittest.main()
