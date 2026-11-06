# -*- coding: utf-8 -*-
import tempfile
import time
import unittest
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from config import proxy as proxy_config
from core import account_import_service as service, db
from webui.app import create_app


class AccountImportModalTests(unittest.TestCase):
    def test_account_import_modal_is_a_centered_viewport_overlay(self):
        html = (Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        modal_rule = re.search(
            r"#outlookImportModal:not\(\.hidden\),\s*"
            r"#emailChangeModalV2:not\(\.hidden\),\s*"
            r"#accountImportModalV2:not\(\.hidden\)\s*\{(?P<body>.*?)\}",
            html,
            flags=re.S,
        )
        self.assertIsNotNone(modal_rule, "账号导入弹窗必须使用与其他弹窗相同的视口覆盖层规则")
        body = modal_rule.group("body")
        self.assertRegex(body, r"position:\s*fixed")
        self.assertRegex(body, r"inset:\s*0")
        self.assertRegex(body, r"display:\s*flex")
        self.assertRegex(body, r"align-items:\s*center")
        self.assertRegex(body, r"justify-content:\s*center")


class AccountImportServiceTests(unittest.TestCase):
    def setUp(self):
        with service._LOCK:
            service._TASKS.clear()

    def test_parse_supports_three_or_four_dashes_and_rejects_duplicate(self):
        rows = service.parse_import_text(
            "a@example.com----password----JBSWY3DPEHPK3PXP\n"
            "b@example.com---password---KRUGS4ZANFZSAYJA"
        )
        self.assertEqual([row["email"] for row in rows], ["a@example.com", "b@example.com"])
        with self.assertRaisesRegex(ValueError, "邮箱重复"):
            service.parse_import_text("a@example.com---password---JBSWY3DPEHPK3PXP\na@example.com---password---JBSWY3DPEHPK3PXP")

    def test_parse_enforces_batch_and_credential_limits(self):
        line = "a@example.com----password----JBSWY3DPEHPK3PXP"
        with self.assertRaisesRegex(ValueError, "最多导入 1"):
            service.parse_import_text(f"{line}\n{line.replace('a@', 'b@')}", max_items=1)
        with self.assertRaisesRegex(ValueError, "密码过长"):
            service.parse_import_text("a@example.com----" + ("x" * (service._MAX_PASSWORD_CHARS + 1)) + "----JBSWY3DPEHPK3PXP")

    def test_success_saves_and_plan_failure_keeps_account(self):
        login = {"ok": True, "access_token": "token", "session": {"user": {"id": "u1"}}}
        saved = {"id": 12, "created": True, "updated": False}
        with patch.object(service.account_liveness, "login_account_with_credentials", return_value=login), \
             patch.object(service.db, "upsert_imported_account", return_value=saved) as save, \
             patch.object(service, "check_account_plan", return_value={"ok": False, "error": "timeout"}), \
             patch.object(service.db, "update_account_plan_check") as plan_update:
            task_id = service.submit_import("a@example.com----password----JBSWY3DPEHPK3PXP")
            for _ in range(100):
                status = service.get_import_status(task_id)
                if status and status["status"] == "completed":
                    break
                time.sleep(0.01)
            self.assertEqual(status["success_count"], 1)
            self.assertEqual(status["items"][0]["plan_status"], "failed")
            save.assert_called_once()
            plan_update.assert_called_once()
            self.assertNotIn("token", str(status))

    def test_login_failure_does_not_save(self):
        login = {"ok": False, "status": "needs_email_otp", "error": "需要验证码"}
        with patch.object(service.account_liveness, "login_account_with_credentials", return_value=login), \
             patch.object(service.db, "upsert_imported_account") as save:
            task_id = service.submit_import("a@example.com----password----JBSWY3DPEHPK3PXP")
            for _ in range(100):
                status = service.get_import_status(task_id)
                if status and status["status"] == "completed":
                    break
                time.sleep(0.01)
            self.assertEqual(status["items"][0]["status"], "needs_email_otp")
            save.assert_not_called()

    def test_status_redacts_token_from_plan_error(self):
        login = {"ok": True, "access_token": "secret-token", "session": {}}
        saved = {"id": 13, "created": True, "updated": False}
        with patch.object(service.account_liveness, "login_account_with_credentials", return_value=login), \
             patch.object(service.db, "upsert_imported_account", return_value=saved), \
             patch.object(service, "check_account_plan", return_value={"ok": False, "error": "bad secret-token"}), \
             patch.object(service.db, "update_account_plan_check"):
            task_id = service.submit_import("a@example.com----password----JBSWY3DPEHPK3PXP")
            for _ in range(100):
                status = service.get_import_status(task_id)
                if status and status["status"] == "completed":
                    break
                time.sleep(0.01)
            self.assertNotIn("secret-token", str(status))

    def test_login_network_403_rotates_to_another_proxy(self):
        failed = {"ok": False, "status": "failed", "error": "HTTPError: HTTP Error 403:"}
        succeeded = {"ok": True, "access_token": "token", "session": {}}
        saved = {"id": 14, "created": True, "updated": False}
        calls = []

        def login(_email, _password, _totp, *, proxy=None):
            calls.append(proxy)
            return failed if len(calls) == 1 else succeeded

        with patch.object(service, "_configured_import_proxies", return_value=["socks5h://proxy-a:1", "socks5h://proxy-b:2"]), \
             patch.object(service.account_liveness, "login_account_with_credentials", side_effect=login), \
             patch.object(service.db, "upsert_imported_account", return_value=saved), \
             patch.object(service, "check_account_plan", return_value={"ok": True}), \
             patch.object(service.db, "update_account_plan_check"), \
             patch.object(proxy_config, "PLAN_CHECK_RETRY_DELAY", 0):
            task_id = service.submit_import("a@example.com----password----JBSWY3DPEHPK3PXP")
            for _ in range(100):
                status = service.get_import_status(task_id)
                if status and status["status"] == "completed":
                    break
                time.sleep(0.01)

        self.assertEqual(status["success_count"], 1)
        self.assertEqual(calls, ["socks5h://proxy-a:1", "socks5h://proxy-b:2"])

    def test_single_dedicated_proxy_keeps_only_dedicated_entry(self):
        with patch.object(proxy_config, "PLAN_CHECK_PROXY", ["socks5h://same:1"] * 3), \
             patch.object(proxy_config, "PROXY_POOL", ["socks5h://same:1", "socks5h://other:2"]):
            self.assertEqual(
                service._configured_import_proxies(),
                ["socks5h://same:1"],
            )

    def test_dedicated_proxy_pool_never_crosses_to_registration_pool(self):
        """专用代理存在时，导入登录不能从 IN 回退到其他地区的通用池。"""
        with patch.object(proxy_config, "PLAN_CHECK_PROXY", ["socks5h://user-IN:1"]), \
             patch.object(proxy_config, "PROXY_POOL", ["socks5h://user-JP:2"]):
            self.assertEqual(
                service._configured_import_proxies(),
                ["socks5h://user-IN:1"],
            )


class AccountImportApiTests(unittest.TestCase):
    def test_api_validates_and_does_not_return_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            missing = root / "missing.json"
            storage = {
                "_ACCOUNTS_JSON": root / "accounts.json", "_OUTLOOK_JSON": root / "outlook.json",
                "_GENERIC_API_EMAIL_JSON": root / "generic.json", "_DOMAIN_EMAIL_JSON": root / "domain.json",
                "_JOBS_JSON": root / "jobs.json", "_LEGACY_SQLITE": missing,
                "_LEGACY_ACCOUNTS_JSON": missing, "_LEGACY_OUTLOOK_JSON": missing, "_LEGACY_JOBS_JSON": missing,
                "_SQLITE_READY": False, "_SQLITE_READY_PATH": None,
            }
            with patch.multiple(db, **storage):
                app = create_app(auth_code="test-auth")
                client = app.test_client()
                with patch.object(service, "submit_import_jobs", side_effect=ValueError("格式错误")):
                    invalid = client.post("/api/accounts/import", json={"text": "bad"}, headers={"X-Auth-Code": "test-auth"})
                self.assertEqual(invalid.status_code, 400)
                non_object = client.post("/api/accounts/import", json=["bad"], headers={"X-Auth-Code": "test-auth"})
                self.assertEqual(non_object.status_code, 400)
                with patch.object(service, "submit_import_jobs", return_value={"task_id": "task-1", "jobs": []}):
                    accepted = client.post("/api/accounts/import", json={"text": "a@example.com----password----JBSWY3DPEHPK3PXP"}, headers={"X-Auth-Code": "test-auth"})
                self.assertEqual(accepted.status_code, 202)
                self.assertNotIn("password", str(accepted.get_json()))
                status = client.get("/api/accounts/import/task-1", headers={"X-Auth-Code": "test-auth"})
                self.assertEqual(status.status_code, 404)


class RegistrationImportJobTests(unittest.TestCase):
    def _storage(self, root: Path):
        missing = root / "missing"
        return {
            "_ACCOUNTS_JSON": root / "accounts.json",
            "_OUTLOOK_JSON": root / "outlook.json",
            "_GENERIC_API_EMAIL_JSON": root / "generic.json",
            "_DOMAIN_EMAIL_JSON": root / "domain.json",
            "_JOBS_JSON": root / "jobs.json",
            "_LEGACY_SQLITE": missing,
            "_LEGACY_ACCOUNTS_JSON": missing,
            "_LEGACY_OUTLOOK_JSON": missing,
            "_LEGACY_JOBS_JSON": missing,
            "_SQLITE_READY": False,
            "_SQLITE_READY_PATH": None,
            "_LOG_DIR": root / "logs",
        }

    def test_import_job_encrypts_credentials_and_deletes_them_with_task(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage(root)):
                job = db.create_import_job(
                    email="a@example.com",
                    password="login-password",
                    totp_secret="JBSWY3DPEHPK3PXP",
                )
                self.assertNotIn("import_credentials", job)
                self.assertNotIn("login-password", str(job))
                self.assertEqual(db.get_import_credentials(job["id"])["password"], "login-password")
                db.update_job(job["id"], status="failed", error="登录失败")
                self.assertTrue(db.delete_job(job["id"]))
                self.assertIsNone(db.get_import_credentials(job["id"]))

    def test_cancelled_import_job_cannot_be_claimed_by_worker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage(root)):
                job = db.create_import_job(
                    email="a@example.com",
                    password="login-password",
                    totp_secret="JBSWY3DPEHPK3PXP",
                )
                db.update_job(job["id"], status="cancelled")
                self.assertIsNone(db.claim_pending_job(job["id"]))
                self.assertEqual(db.get_job(job["id"])["status"], "cancelled")

    def test_registration_import_api_returns_compact_job_without_credentials(self):
        job = {
            "id": 22,
            "job_type": "account_import",
            "email_source": "import",
            "email": "a@example.com",
            "status": "pending",
            "import_credentials_enc": "encrypted-value",
        }
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self._storage(Path(td))), \
             patch.object(service, "submit_import_jobs", return_value={"task_id": "t1", "jobs": [job]}), \
             patch.object(db, "get_job", return_value=job):
            app = create_app(auth_code="test-auth")
            client = app.test_client()
            response = client.post(
                "/api/jobs/import",
                json={"text": "a@example.com----login-password----JBSWY3DPEHPK3PXP"},
                headers={"X-Auth-Code": "test-auth"},
            )
        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertNotIn("login-password", str(body))
        self.assertNotIn("import_credentials", str(body))
        self.assertEqual(body["jobs"][0]["job_type"], "account_import")

class RoxyImportProfileCleanupTests(unittest.TestCase):
    def _storage(self, root: Path) -> dict:
        missing = root / "missing"
        return {
            "_ACCOUNTS_JSON": root / "accounts.json", "_OUTLOOK_JSON": root / "outlook.json",
            "_GENERIC_API_EMAIL_JSON": root / "generic.json", "_DOMAIN_EMAIL_JSON": root / "domain.json",
            "_JOBS_JSON": root / "jobs.json", "_LEGACY_SQLITE": missing,
            "_LEGACY_ACCOUNTS_JSON": missing, "_LEGACY_OUTLOOK_JSON": missing, "_LEGACY_JOBS_JSON": missing,
            "_SQLITE_READY": False, "_SQLITE_READY_PATH": None,
        }
    def test_roxy_import_success_when_delete_after_run_false_retains_profile(self):
        from config import roxybrowser
        from core import account_browser_login as browser_login
        from core.roxybrowser_client import RoxyOpenResult

        client = MagicMock()
        client._proxy_pool_relay = None
        opened = RoxyOpenResult(profile_id="imported-profile-1", raw={}, created_by_run=True)
        client.open_profile.return_value = opened
        mock_driver = MagicMock()
        mock_driver.current_url = "https://chatgpt.com/"

        with patch.object(roxybrowser, "ROXY_DELETE_PROFILE_AFTER_RUN", False), \
             patch.object(roxybrowser, "ROXY_KEEP_BROWSER_OPEN", False), \
             patch("core.roxybrowser_client.RoxyBrowserClient", return_value=client), \
             patch("core.roxy_registration._build_driver", return_value=mock_driver), \
             patch.object(browser_login, "_setup_browser_data_saver_and_cache", return_value=(None, None, None)), \
             patch.object(browser_login, "_login_on_page", return_value={"accessToken": "tok1", "user": {"email": "import@example.com"}}), \
             patch.object(browser_login, "_fingerprint", return_value={"driver": "roxy"}), \
             patch.object(browser_login, "_apply_post_login_data_saver"):
            result = browser_login.login_with_browser("import@example.com", "pass", "totp", proxy="socks5://127.0.0.1:1080", driver_mode="roxy")

        self.assertTrue(result.get("ok"))
        client.cleanup_profile.assert_called_once_with(opened, retain=True)

    def test_roxy_import_failure_when_delete_after_run_false_does_not_retain_profile(self):
        from config import roxybrowser
        from core import account_browser_login as browser_login
        from core.roxybrowser_client import RoxyOpenResult

        client = MagicMock()
        client._proxy_pool_relay = None
        opened = RoxyOpenResult(profile_id="failed-profile-1", raw={}, created_by_run=True)
        client.open_profile.return_value = opened
        mock_driver = MagicMock()

        with patch.object(roxybrowser, "ROXY_DELETE_PROFILE_AFTER_RUN", False), \
             patch.object(roxybrowser, "ROXY_KEEP_BROWSER_OPEN", False), \
             patch("core.roxybrowser_client.RoxyBrowserClient", return_value=client), \
             patch("core.roxy_registration._build_driver", return_value=mock_driver), \
             patch.object(browser_login, "_setup_browser_data_saver_and_cache", return_value=(None, None, None)), \
             patch.object(browser_login, "_login_on_page", side_effect=RuntimeError("密码错误")):
            with self.assertRaises(RuntimeError):
                browser_login.login_with_browser("import@example.com", "wrong-pass", "totp", proxy="", driver_mode="roxy")

        client.cleanup_profile.assert_called_once_with(opened, retain=False)

    def test_roxy_cleanup_profile_deletes_failed_environment_even_when_delete_after_run_false(self):
        from config import roxybrowser
        from core.roxybrowser_client import RoxyBrowserClient, RoxyOpenResult

        client = RoxyBrowserClient()
        opened = RoxyOpenResult(profile_id="failed-pid", raw={}, created_by_run=True)
        with patch.object(roxybrowser, "ROXY_DELETE_PROFILE_AFTER_RUN", False), \
             patch.object(roxybrowser, "ROXY_KEEP_BROWSER_OPEN", False), \
             patch.object(client, "close_profile") as mock_close, \
             patch.object(client, "delete_profile") as mock_delete:
            client.cleanup_profile(opened, retain=False)
            mock_close.assert_called_once_with("failed-pid")
            mock_delete.assert_called_once_with("failed-pid")

    def test_roxy_cleanup_profile_retains_success_environment_when_delete_after_run_false(self):
        from config import roxybrowser
        from core.roxybrowser_client import RoxyBrowserClient, RoxyOpenResult

        client = RoxyBrowserClient()
        opened = RoxyOpenResult(profile_id="success-pid", raw={}, created_by_run=True)
        with patch.object(roxybrowser, "ROXY_DELETE_PROFILE_AFTER_RUN", False), \
             patch.object(roxybrowser, "ROXY_KEEP_BROWSER_OPEN", False), \
             patch.object(client, "close_profile") as mock_close, \
             patch.object(client, "delete_profile") as mock_delete:
            client.cleanup_profile(opened, retain=True)
            mock_close.assert_called_once_with("success-pid")
            mock_delete.assert_not_called()

    def test_roxy_cleanup_profile_deletes_both_when_delete_after_run_true(self):
        from config import roxybrowser
        from core.roxybrowser_client import RoxyBrowserClient, RoxyOpenResult

        client = RoxyBrowserClient()
        for is_retained in (True, False):
            opened = RoxyOpenResult(profile_id=f"pid-{is_retained}", raw={}, created_by_run=True)
            with patch.object(roxybrowser, "ROXY_DELETE_PROFILE_AFTER_RUN", True), \
                 patch.object(roxybrowser, "ROXY_KEEP_BROWSER_OPEN", False), \
                 patch.object(client, "close_profile") as mock_close, \
                 patch.object(client, "delete_profile") as mock_delete:
                client.cleanup_profile(opened, retain=is_retained)
                mock_close.assert_called_once_with(f"pid-{is_retained}")
                mock_delete.assert_called_once_with(f"pid-{is_retained}")

    def test_upsert_imported_account_binds_roxy_profile_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage(root)):
                saved = db.upsert_imported_account(
                    email="imported.roxy@example.com",
                    password="password123",
                    totp_secret="JBSWY3DPEHPK3PXP",
                    access_token="test-token",
                    proxy_used="socks5://127.0.0.1:1080",
                    fingerprint={"driver": "roxy", "profile_id": "roxy-saved-profile-888"},
                )
                account_id = saved["id"]
                bound_pid = db.get_account_roxy_profile_id(account_id, "imported.roxy@example.com")
                self.assertEqual(bound_pid, "roxy-saved-profile-888")
                account = db.get_account(account_id)
                self.assertEqual(account.get("roxy_profile_id"), "roxy-saved-profile-888")
                self.assertTrue(account.get("roxy_profile_proxy_key"))


if __name__ == "__main__":
    unittest.main()
