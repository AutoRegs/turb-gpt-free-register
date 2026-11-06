# -*- coding: utf-8 -*-
"""查活切换到指定浏览器后，使用独立环境并写回新的访问令牌。"""
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from config import roxybrowser
from core import account_liveness as liveness
from core import account_browser_login as browser_login
from core import live_check_service as live_service, db
from core import roxy_registration as pages, roxy_codex_oauth as login_pages


class BrowserLivenessTests(unittest.TestCase):
    def test_roxy_live_check_does_not_use_protocol_authorize(self):
        refreshed = {"ok": True, "status": "live", "access_token": "fresh-token", "session": {}}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(liveness, "_LOG_DIR", Path(tmp)), \
             patch.object(roxybrowser, "REGISTRATION_DRIVER", "roxy"), \
             patch.object(liveness, "_stored_access_token", return_value="expired-token"), \
             patch.object(liveness, "_account_registration_password", return_value="password"), \
             patch.object(liveness, "_account_totp_secret", return_value="JBSWY3DPEHPK3PXP"), \
             patch.object(liveness, "_login_via_full_web_flow", side_effect=RuntimeError("HTTP Error 403")) as protocol, \
             patch.object(browser_login, "login_with_browser", return_value=refreshed) as browser:
            result = liveness.check_account_liveness("user@example.com", proxy="", email_source="remail", driver_mode="roxy")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["access_token"], "fresh-token")
        protocol.assert_not_called()
        browser.assert_called_once()
        self.assertTrue(browser.call_args.kwargs["allow_email_otp"])
        self.assertEqual(browser.call_args.kwargs["email_source"], "remail")
        self.assertEqual(browser.call_args.kwargs["purpose"], "live_check")

    def test_browser_failure_never_falls_back_to_protocol_and_redacts_credentials(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(liveness, "_LOG_DIR", Path(tmp)), \
             patch.object(roxybrowser, "REGISTRATION_DRIVER", "roxy"), \
             patch.object(liveness, "_account_registration_password", return_value="private-password"), \
             patch.object(liveness, "_account_totp_secret", return_value="private-totp"), \
             patch.object(browser_login, "login_with_browser", side_effect=RuntimeError("private-password private-totp")), \
             patch.object(liveness, "_login_via_full_web_flow") as protocol:
            result = liveness.check_account_liveness("user@example.com", proxy="", driver_mode="roxy")
            log_text = liveness.log_path("user@example.com").read_text()
        self.assertFalse(result["ok"])
        for secret in ("private-password", "private-totp"):
            self.assertNotIn(secret, result["error"])
            self.assertNotIn(secret, log_text)
        protocol.assert_not_called()

    def test_live_check_opens_new_roxy_environment_and_uses_saved_password_and_totp(self):
        client, driver = MagicMock(), MagicMock()
        client.open_profile.return_value = SimpleNamespace(profile_id="fresh-profile", raw={})
        info = {"accessToken": "fresh-token", "user": {"email": "user@example.com"}}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(liveness, "_LOG_DIR", Path(tmp)), \
             patch.object(roxybrowser, "REGISTRATION_DRIVER", "roxy"), \
             patch.object(roxybrowser, "ROXY_KEEP_BROWSER_OPEN", False), \
             patch.object(liveness, "_account_registration_password", return_value="saved-password"), \
             patch.object(liveness, "_account_totp_secret", return_value="saved-totp"), \
             patch("core.roxybrowser_client.RoxyBrowserClient", return_value=client), \
             patch.object(pages, "_build_driver", return_value=driver), \
             patch.object(browser_login, "_login_on_page", return_value=info) as page_login, \
             patch.object(browser_login, "_fingerprint", return_value={}), \
             patch.object(liveness, "BrowserSession", side_effect=AssertionError("不能创建协议会话")):
            result = liveness.check_account_liveness("user@example.com", proxy="http://relay:8080", email_source="remail", driver_mode="roxy")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["fingerprint"]["lifecycle"], "fresh_per_live_check")
        client.open_profile.assert_called_once_with(fresh=True, proxy="http://relay:8080")
        page_login.assert_called_once_with(
            driver, "user@example.com", "saved-password", "saved-totp",
            allow_email_otp=True, email_source="remail", log_prefix="[查活]",
        )
        driver.quit.assert_called_once()
        client.cleanup_profile.assert_called_once()

    def test_browser_otp_only_live_check_uses_saved_email_source(self):
        driver = MagicMock()
        driver.current_url = "https://auth.openai.com/email-verification"

        def submit_otp(*args, **kwargs):
            driver.current_url = "https://chatgpt.com/"

        with patch.object(pages, "_check_manual_stop"), \
             patch.object(pages, "_maybe_accept"), \
             patch.object(pages, "_submit_email_and_wait_next"), \
             patch.object(pages, "_is_login_password_page", return_value=False), \
             patch.object(pages, "_is_email_verification_page", return_value=True), \
             patch.object(login_pages, "_is_mfa_challenge_page", return_value=False), \
             patch.object(pages, "_read_chatgpt_session_once", return_value={"accessToken": "new-token"}), \
             patch.object(browser_login, "_submit_login_email_otp", side_effect=submit_otp) as otp, \
             patch.object(browser_login, "_choose_login_password") as choose, \
             patch.object(browser_login.time, "sleep"):
            info = browser_login._login_on_page(driver, "user@example.com", "", "", allow_email_otp=True, email_source="remail", log_prefix="[查活]")
        self.assertEqual(info["accessToken"], "new-token")
        self.assertEqual(otp.call_args.kwargs["email_source"], "remail")
        choose.assert_not_called()

    def test_reused_environment_reads_an_existing_authenticated_session_first(self):
        driver = MagicMock()
        info = {"accessToken": "current-token", "user": {"email": "user@example.com"}}
        with patch.object(pages, "_check_manual_stop"), \
             patch.object(pages, "_maybe_accept"), \
             patch.object(pages, "_read_chatgpt_session_once", return_value=info), \
             patch.object(browser_login, "_verify_chatgpt_session", return_value=(True, "有效")), \
             patch.object(pages, "_submit_email_and_wait_next") as submit:
            result = browser_login._login_on_page(driver, "user@example.com", "password", "totp", reuse_session=True)
        self.assertEqual(result, info)
        driver.get.assert_called_once_with("https://chatgpt.com/")
        submit.assert_not_called()

    def test_reused_environment_falls_back_to_login_when_session_invalid(self):
        driver = MagicMock()
        stale_info = {"accessToken": "stale-revoked-token", "user": {"email": "user@example.com"}}
        fresh_info = {"accessToken": "fresh-valid-token", "user": {"email": "user@example.com"}}
        read_returns = [stale_info, fresh_info]

        def fake_read_session(_d):
            return read_returns.pop(0) if read_returns else fresh_info

        driver.current_url = "https://chatgpt.com/"
        with patch.object(pages, "_check_manual_stop"), \
             patch.object(pages, "_maybe_accept"), \
             patch.object(pages, "_read_chatgpt_session_once", side_effect=fake_read_session), \
             patch.object(browser_login, "_verify_chatgpt_session", return_value=(False, "HTTP 401 (token_revoked)")), \
             patch.object(pages, "_submit_email_and_wait_next") as submit, \
             patch.object(browser_login.time, "sleep"):
            result = browser_login._login_on_page(driver, "user@example.com", "password", "totp", reuse_session=True)

        self.assertEqual(result["accessToken"], "fresh-valid-token")
        driver.delete_all_cookies.assert_called_once()
        submit.assert_called_once_with(driver, "user@example.com", allow_existing=True)

    def test_verify_chatgpt_session_success(self):
        driver = MagicMock()
        driver.execute_async_script.return_value = {
            "ok": True,
            "status": 200,
            "data": {"email": "user@example.com", "id": "user-123"},
        }
        ok, reason = browser_login._verify_chatgpt_session(driver, "valid-token", expected_email="user@example.com")
        self.assertTrue(ok)
        self.assertEqual(reason, "有效")

    def test_verify_chatgpt_session_email_mismatch(self):
        driver = MagicMock()
        driver.execute_async_script.return_value = {
            "ok": True,
            "status": 200,
            "data": {"email": "other@example.com"},
        }
        ok, reason = browser_login._verify_chatgpt_session(driver, "valid-token", expected_email="user@example.com")
        self.assertFalse(ok)
        self.assertIn("邮箱不匹配", reason)

    def test_verify_chatgpt_session_revoked_401(self):
        driver = MagicMock()
        driver.execute_async_script.return_value = {
            "ok": False,
            "status": 401,
            "data": {
                "error": {
                    "code": "token_revoked",
                    "message": "Encountered invalidated oauth token for user",
                }
            },
        }
        ok, reason = browser_login._verify_chatgpt_session(driver, "revoked-token", expected_email="user@example.com")
        self.assertFalse(ok)
        self.assertIn("401", reason)
        self.assertIn("token_revoked", reason)

    def test_verify_chatgpt_session_exception(self):
        driver = MagicMock()
        driver.execute_async_script.side_effect = RuntimeError("script timeout")
        ok, reason = browser_login._verify_chatgpt_session(driver, "token")
        self.assertFalse(ok)
        self.assertIn("RuntimeError", reason)
    def test_email_otp_invalid_code_retries_only_after_resend(self):
        driver = MagicMock()
        with patch.object(pages, "_check_manual_stop"), \
             patch.object(browser_login, "wait_for_otp", side_effect=["111111", "222222"]) as wait, \
             patch.object(pages, "_clear_otp_inputs"), \
             patch.object(pages, "_type_otp") as fill, \
             patch.object(pages, "_click_continue"), \
             patch.object(pages, "_wait_after_email_otp_submit", side_effect=["invalid", "accepted"]), \
             patch.object(pages, "_click_resend_email_otp") as resend, \
             patch.object(browser_login.time, "time", return_value=200.0):
            browser_login._submit_login_email_otp(driver, "user@example.com", after_ts=100.0, email_source="remail", log_prefix="[查活]")
        self.assertEqual([call.args[1] for call in fill.call_args_list], ["111111", "222222"])
        self.assertEqual(wait.call_args_list[0].kwargs["after_ts"], 100.0)
        self.assertEqual(wait.call_args_list[1].kwargs["after_ts"], 200.0)
        resend.assert_called_once()

    def test_email_otp_stops_after_three_invalid_codes(self):
        with patch.object(pages, "_check_manual_stop"), \
             patch.object(browser_login, "wait_for_otp", return_value="111111") as wait, \
             patch.object(pages, "_clear_otp_inputs"), \
             patch.object(pages, "_type_otp"), \
             patch.object(pages, "_click_continue"), \
             patch.object(pages, "_wait_after_email_otp_submit", return_value="invalid"), \
             patch.object(pages, "_click_resend_email_otp") as resend:
            with self.assertRaisesRegex(RuntimeError, "最大重试"):
                browser_login._submit_login_email_otp(MagicMock(), "user@example.com", after_ts=100, email_source="remail", log_prefix="[查活]")
        self.assertEqual(wait.call_count, 3)
        self.assertEqual(resend.call_count, 2)


class LiveCheckPersistenceTests(unittest.TestCase):
    def test_successful_browser_live_check_updates_stored_access_token(self):
        @contextmanager
        def environment(*_args):
            yield MagicMock(), "new-profile", ""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing"
            storage = {
                "_ACCOUNTS_JSON": root / "accounts.json", "_OUTLOOK_JSON": root / "outlook.json",
                "_GENERIC_API_EMAIL_JSON": root / "generic.json", "_DOMAIN_EMAIL_JSON": root / "domain.json",
                "_JOBS_JSON": root / "jobs.json", "_LEGACY_SQLITE": missing,
                "_LEGACY_ACCOUNTS_JSON": missing, "_LEGACY_OUTLOOK_JSON": missing, "_LEGACY_JOBS_JSON": missing,
                "_SQLITE_READY": False, "_SQLITE_READY_PATH": None,
            }
            with patch.multiple(db, **storage), \
                 patch.object(liveness, "_LOG_DIR", root / "logs"), \
                 patch.object(roxybrowser, "REGISTRATION_DRIVER", "roxy"), \
                 patch.object(live_service, "_QUEUE_SLOTS", MagicMock()), \
                 patch.object(live_service, "_configured_live_check_proxies", return_value=[]), \
                 patch.object(live_service, "resolve_plan_check_route", return_value={"proxy": "", "network_route": "direct"}), \
                 patch.object(live_service, "open_plan_check_proxy", return_value=("", None)), \
                 patch.object(browser_login, "_roxy_account_environment", environment), \
                 patch.object(browser_login, "_fingerprint", return_value={}), \
                 patch.object(browser_login, "_login_on_page", return_value={"accessToken": "fresh-token", "user": {"email": "user@example.com"}}), \
                 patch.object(liveness, "_login_via_full_web_flow", side_effect=RuntimeError("HTTP 403")):
                saved = db.upsert_imported_account(email="user@example.com", password="password", totp_secret="JBSWY3DPEHPK3PXP", access_token="expired-token", session={})
                for _ in range(3):
                    db.record_account_live_check_login_attempt(saved["id"], {"ok": False, "status": "failed"}, driver_mode="protocol")
                db.claim_account_live_check(acc_id=saved["id"], trigger="manual")
                result = live_service._run_live_check(account_id=saved["id"], email="user@example.com", proxy="", trigger="manual")
                stored = db.get_account(saved["id"])
            self.assertTrue(result["ok"], result.get("error"))
            self.assertEqual(stored["access_token"], "fresh-token")
            self.assertEqual(stored["live_check_status"], "live")
            self.assertEqual(stored["live_check_protocol_failures"], 0)
            self.assertEqual(stored["live_check_fingerprint"]["lifecycle"], "account_profile_created")

    def test_live_check_proxy_candidates_are_deduplicated(self):
        config = SimpleNamespace(PLAN_CHECK_PROXY=["socks5://dedicated:1080"] * 3, PROXY_POOL=["socks5://other-country:1080"])
        self.assertEqual(live_service._configured_live_check_proxies(config), ["socks5://dedicated:1080"])

    def test_is_session_expired_identifies_expired_and_valid(self):
        import time, base64, json
        now = time.time()
        # Expired ISO
        self.assertTrue(browser_login._is_session_expired("2020-01-01T00:00:00Z", None))
        # Valid future ISO
        self.assertFalse(browser_login._is_session_expired("2035-01-01T00:00:00Z", None))

        # Expired JWT exp
        payload_exp = base64.urlsafe_b64encode(json.dumps({"exp": now - 3600}).encode()).decode().rstrip("=")
        token_exp = f"header.{payload_exp}.sig"
        self.assertTrue(browser_login._is_session_expired(None, token_exp))

        # Valid JWT exp
        payload_valid = base64.urlsafe_b64encode(json.dumps({"exp": now + 86400 * 5}).encode()).decode().rstrip("=")
        token_valid = f"header.{payload_valid}.sig"
        self.assertFalse(browser_login._is_session_expired(None, token_valid))

    def test_verify_chatgpt_session_script_structure(self):
        mock_driver = MagicMock()
        mock_driver.execute_async_script.return_value = {
            "ok": True,
            "status": 200,
            "data": {"email": "test@example.com"}
        }
        valid, msg = browser_login._verify_chatgpt_session(mock_driver, "tok123", expected_email="test@example.com")
        self.assertTrue(valid)
        self.assertEqual(msg, "有效")
        # Ensure arguments in script uses arguments.length - 1 for done callback
        script = mock_driver.execute_async_script.call_args[0][0]
        self.assertIn("arguments[arguments.length - 1]", script)
        self.assertIn("arguments[0]", script)

    def test_ab_chatgpt_is_in_blocked_saver_rules(self):
        from config import browser
        self.assertIn("**://ab.chatgpt.com/**", browser.BROWSER_DATA_SAVER_BLOCKED_URL_PATTERNS)
        self.assertIn("ab.chatgpt.com", browser.BROWSER_DATA_SAVER_BLOCKED_PROXY_HOSTS)


if __name__ == "__main__":
    unittest.main()
