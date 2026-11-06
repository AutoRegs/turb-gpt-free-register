"""查活与导入登录场景下浏览器省流量拦截、本地静态缓存与退出清理测试。"""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from config import browser as browser_cfg, cloakbrowser, roxybrowser
from core import account_browser_login as browser_login
from core.browser_data_saver import BrowserDataSaver
from core.roxy_asset_cache import RoxyLocalAssetCache


class BrowserLoginDataSaverTests(unittest.TestCase):
    def setUp(self):
        self.email = "test@example.com"
        self.password = "password123"
        self.totp = "JBSWY3DPEHPK3PXP"
        self.session_info = {"accessToken": "valid-token", "user": {"email": self.email}}

    def test_roxy_live_check_installs_data_saver_and_starts_asset_cache(self):
        driver = MagicMock()
        opened = SimpleNamespace(profile_id="p-roxy-1", debugger_address="127.0.0.1:9222", raw={})
        client = MagicMock()
        client.open_profile.return_value = opened

        with patch.object(browser_cfg, "BROWSER_DATA_SAVER_MODE", True), \
             patch.object(roxybrowser, "REGISTRATION_DRIVER", "roxy"), \
             patch.object(roxybrowser, "ROXY_KEEP_BROWSER_OPEN", False), \
             patch("core.roxybrowser_client.RoxyBrowserClient", return_value=client), \
             patch("core.roxy_registration._build_driver", return_value=driver), \
             patch.object(browser_login, "_login_on_page", return_value=self.session_info), \
             patch.object(browser_login, "_fingerprint", return_value={"driver": "roxy"}), \
             patch("core.roxy_asset_cache.RoxyLocalAssetCache.start", autospec=True, side_effect=lambda s: s) as mock_cache_start, \
             patch("core.roxy_asset_cache.RoxyLocalAssetCache.stop", autospec=True) as mock_cache_stop, \
             patch("core.browser_data_saver.BrowserDataSaver.install_selenium") as mock_install_selenium, \
             patch("core.browser_data_saver.BrowserDataSaver.enable_post_auth_deep_mode") as mock_deep_mode, \
             patch("core.browser_data_saver.BrowserDataSaver.stop") as mock_saver_stop:

            result = browser_login.login_with_browser(
                self.email, self.password, self.totp,
                proxy="", driver_mode="roxy", purpose="live_check",
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["access_token"], "valid-token")

            # 验证省流量拦截器在登录前安装在 Selenium 上
            mock_install_selenium.assert_called_once_with(driver)
            # 验证本地静态资源缓存被启动
            mock_cache_start.assert_called_once()
            # 验证登录成功后启用了深度省流量
            mock_deep_mode.assert_called_once_with(driver)
            # 验证退出前切到了 about:blank 阻断后续后台流量
            driver.get.assert_called_with("about:blank")
            # 验证清理阶段安全停止拦截器和缓存
            mock_saver_stop.assert_called_once()
            mock_cache_stop.assert_called_once()
            # 验证窗口正常关闭与清理
            driver.quit.assert_called_once()
            client.cleanup_profile.assert_called_once()

    def test_roxy_live_check_account_environment_installs_data_saver(self):
        driver = MagicMock()
        opened = SimpleNamespace(profile_id="p-account-1", debugger_address="127.0.0.1:9222", raw={})
        client = MagicMock()
        client.open_profile.return_value = opened

        with patch.object(browser_cfg, "BROWSER_DATA_SAVER_MODE", True), \
             patch.object(roxybrowser, "REGISTRATION_DRIVER", "roxy"), \
             patch.object(roxybrowser, "ROXY_KEEP_BROWSER_OPEN", False), \
             patch("core.db.get_account_roxy_profile_id", return_value="p-account-1"), \
             patch("core.db.get_account", return_value={"id": 1, "email": self.email}), \
             patch("core.db.bind_account_roxy_profile", return_value=True), \
             patch("core.roxybrowser_client.RoxyBrowserClient", return_value=client), \
             patch("core.roxy_registration._build_driver", return_value=driver), \
             patch.object(browser_login, "_login_on_page", return_value=self.session_info), \
             patch.object(browser_login, "_fingerprint", return_value={"driver": "roxy"}), \
             patch("core.roxy_asset_cache.RoxyLocalAssetCache.start", autospec=True, side_effect=lambda s: s) as mock_cache_start, \
             patch("core.roxy_asset_cache.RoxyLocalAssetCache.stop", autospec=True) as mock_cache_stop, \
             patch("core.browser_data_saver.BrowserDataSaver.install_selenium") as mock_install_selenium, \
             patch("core.browser_data_saver.BrowserDataSaver.enable_post_auth_deep_mode") as mock_deep_mode, \
             patch("core.browser_data_saver.BrowserDataSaver.stop") as mock_saver_stop:

            result = browser_login.login_with_browser(
                self.email, self.password, self.totp,
                proxy="", driver_mode="roxy", purpose="live_check", account_id=1,
            )

            self.assertTrue(result["ok"])
            mock_install_selenium.assert_called_once_with(driver)
            mock_cache_start.assert_called_once()
            mock_deep_mode.assert_called_once_with(driver)
            mock_saver_stop.assert_called_once()
            mock_cache_stop.assert_called_once()

    def test_cloak_import_installs_data_saver_on_playwright_context(self):
        driver = MagicMock()
        driver.context = MagicMock()
        opened = SimpleNamespace(profile_id="p-cloak-1", raw={})

        with patch.object(browser_cfg, "BROWSER_DATA_SAVER_MODE", True), \
             patch.object(cloakbrowser, "CLOAK_KEEP_BROWSER_OPEN", False), \
             patch("core.cloakbrowser_driver.build_cloak_driver", return_value=(driver, opened)), \
             patch.object(browser_login, "_login_on_page", return_value=self.session_info), \
             patch.object(browser_login, "_fingerprint", return_value={"driver": "cloak"}), \
             patch("core.browser_data_saver.BrowserDataSaver.install_playwright") as mock_install_playwright, \
             patch("core.browser_data_saver.BrowserDataSaver.stop") as mock_saver_stop:

            result = browser_login.login_with_browser(
                self.email, self.password, self.totp,
                proxy="", driver_mode="cloak", purpose="import",
            )

            self.assertTrue(result["ok"])
            mock_install_playwright.assert_called_once_with(driver.context)
            mock_saver_stop.assert_called_once()
            driver.quit.assert_called_once()

    def test_keep_browser_open_skips_about_blank_navigation(self):
        driver = MagicMock()
        opened = SimpleNamespace(profile_id="p-roxy-open", debugger_address="127.0.0.1:9222", raw={})
        client = MagicMock()
        client.open_profile.return_value = opened

        with patch.object(browser_cfg, "BROWSER_DATA_SAVER_MODE", True), \
             patch.object(roxybrowser, "ROXY_KEEP_BROWSER_OPEN", True), \
             patch("core.roxybrowser_client.RoxyBrowserClient", return_value=client), \
             patch("core.roxy_registration._build_driver", return_value=driver), \
             patch.object(browser_login, "_login_on_page", return_value=self.session_info), \
             patch.object(browser_login, "_fingerprint", return_value={"driver": "roxy"}), \
             patch("core.roxy_asset_cache.RoxyLocalAssetCache.start"), \
             patch("core.roxy_asset_cache.RoxyLocalAssetCache.stop"), \
             patch("core.browser_data_saver.BrowserDataSaver.install_selenium"), \
             patch("core.browser_data_saver.BrowserDataSaver.enable_post_auth_deep_mode") as mock_deep_mode, \
             patch("core.browser_data_saver.BrowserDataSaver.stop"):

            result = browser_login.login_with_browser(
                self.email, self.password, self.totp,
                proxy="", driver_mode="roxy", purpose="live_check",
            )

            self.assertTrue(result["ok"])
            # 保持窗口打开时，依然开启深度拦截
            mock_deep_mode.assert_called_once_with(driver)
            # 但跳过 driver.get("about:blank")，以便用户在桌面观察
            driver.get.assert_not_called()
            # 且不退出 driver
            driver.quit.assert_not_called()

    def test_data_saver_initialization_failure_does_not_break_login(self):
        driver = MagicMock()
        opened = SimpleNamespace(profile_id="p-roxy-err", debugger_address="127.0.0.1:9222", raw={})
        client = MagicMock()
        client.open_profile.return_value = opened

        with patch.object(browser_cfg, "BROWSER_DATA_SAVER_MODE", True), \
             patch.object(roxybrowser, "ROXY_KEEP_BROWSER_OPEN", False), \
             patch("core.roxybrowser_client.RoxyBrowserClient", return_value=client), \
             patch("core.roxy_registration._build_driver", return_value=driver), \
             patch.object(browser_login, "_login_on_page", return_value=self.session_info), \
             patch.object(browser_login, "_fingerprint", return_value={"driver": "roxy"}), \
             patch("core.roxy_asset_cache.RoxyLocalAssetCache.start", side_effect=RuntimeError("Cache fail")), \
             patch("core.browser_data_saver.BrowserDataSaver.install_selenium", side_effect=RuntimeError("CDP fail")):

            # 即使拦截器或本地缓存初始化报错，也不应中断登录主流程
            result = browser_login.login_with_browser(
                self.email, self.password, self.totp,
                proxy="", driver_mode="roxy", purpose="live_check",
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["access_token"], "valid-token")

    def test_login_with_browser_returns_network_traffic(self):
        driver = MagicMock()
        opened = SimpleNamespace(profile_id="p-traffic-1", debugger_address="127.0.0.1:9222", raw={})
        client = MagicMock()
        client.open_profile.return_value = opened
        mock_traffic = {
            "available": True, "upload_bytes": 1024, "download_bytes": 2048, "total_bytes": 3072,
            "browser_observed_total_bytes": 3072, "label": "Roxy",
        }

        with patch.object(roxybrowser, "REGISTRATION_DRIVER", "roxy"), \
             patch("core.roxybrowser_client.RoxyBrowserClient", return_value=client), \
             patch("core.roxy_registration._build_driver", return_value=driver), \
             patch.object(browser_login, "_login_on_page", return_value=self.session_info), \
             patch.object(browser_login, "_fingerprint", return_value={"driver": "roxy"}), \
             patch.object(browser_login, "_collect_and_teardown_traffic", return_value=mock_traffic):

            result = browser_login.login_with_browser(
                self.email, self.password, self.totp,
                proxy="", driver_mode="roxy", purpose="import",
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result.get("network_traffic"), mock_traffic)

    def test_import_job_saves_network_traffic_in_job_and_account(self):
        from core import account_import_service as import_svc, db
        job_id = 999
        job = {
            "id": job_id, "job_type": "account_import", "status": "pending",
            "email": self.email, "log_file": "/tmp/test_import_job.log",
        }
        credentials = {"password": self.password, "totp_secret": self.totp}
        mock_traffic = {"available": True, "total_bytes": 5000, "upload_bytes": 1000, "download_bytes": 4000}
        login_result = {
            "ok": True, "access_token": "token-1", "user": {"email": self.email},
            "network_traffic": mock_traffic,
        }

        with patch("core.db.get_job", return_value=job), \
             patch("core.db.claim_pending_job", return_value=job), \
             patch("core.db.get_import_credentials", return_value=credentials), \
             patch.object(import_svc, "_login_with_import_retry", return_value=(login_result, None)), \
             patch("core.db.upsert_imported_account", return_value={"id": 101}) as mock_upsert, \
             patch("core.db.update_job") as mock_update_job, \
             patch("core.registration_service.check_stop_requested"):

            import_svc._run_import_job(job_id)

            mock_upsert.assert_called_once()
            self.assertEqual(mock_upsert.call_args.kwargs.get("network_traffic"), mock_traffic)
            self.assertTrue(mock_update_job.called)
            self.assertEqual(mock_update_job.call_args.kwargs.get("network_traffic"), mock_traffic)
            self.assertEqual(mock_update_job.call_args.kwargs.get("status"), "success")

    def test_live_check_browser_emits_traffic_log_line(self):
        from core import live_check_service
        email = "live-check-user@example.com"
        mock_traffic = {
            "available": True, "total_bytes": 2768240, "upload_bytes": 0, "download_bytes": 0,
            "browser_observed_total_bytes": 2768240, "label": "Roxy",
        }
        browser_result = {
            "ok": True, "status": "live", "access_token": "live-tok",
            "network_traffic": mock_traffic,
        }

        with patch("core.live_check_service.check_account_liveness", return_value=browser_result), \
             patch("core.live_check_service.db.record_account_live_check_login_attempt", return_value=0), \
             patch.object(live_check_service, "_append_log") as mock_append_log:

            result, failures = live_check_service._login_attempt(
                account_id=1, email=email, proxy=None,
                driver_mode="roxy", email_source=None, fingerprint_state={},
            )
            self.assertTrue(result["ok"])
            calls = [c.args[1] for c in mock_append_log.call_args_list]
            traffic_logs = [line for line in calls if "代理传输流量" in line]
            self.assertTrue(len(traffic_logs) > 0, f"Expected 代理传输流量 in logs, got {calls}")
            self.assertIn("页面可见 2.64 MiB", traffic_logs[0])
            self.assertIn("[Roxy]", traffic_logs[0])
