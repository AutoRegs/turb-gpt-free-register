# -*- coding: utf-8 -*-
"""导入登录必须遵循注册驱动，并为每个账号创建独立环境。"""
import unittest
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from config import register, roxybrowser
from core import account_liveness as liveness
from core import account_browser_login as browser_login
from core import roxy_registration as pages
from core import roxy_codex_oauth as login_pages
from core.roxybrowser_client import RoxyBrowserClient


class ImportLoginTests(unittest.TestCase):
    def test_protocol_import_generates_distinct_uuid4_identities(self):
        sessions = []

        def login(email, proxy, **kwargs):
            session = liveness._new_fingerprint_pinned_session(email, proxy, kwargs["fingerprint_state"])
            sessions.append(session)
            return session, {"accessToken": "token"}

        with patch.object(roxybrowser, "REGISTRATION_DRIVER", "protocol"), \
             patch.object(register, "PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL", True), \
             patch.object(liveness.BrowserSession, "_detect_exit_geo", return_value={}), \
             patch.object(liveness, "_login_via_full_web_flow", side_effect=login):
            for _ in range(2):
                self.assertTrue(liveness.login_account_with_credentials("a@example.com", "password", "JBSWY3DPEHPK3PXP", proxy="")["ok"])
        self.assertNotEqual(sessions[0].device_id, sessions[1].device_id)
        self.assertNotEqual(sessions[0].oai_session_id, sessions[1].oai_session_id)
        self.assertEqual(sessions[0].fingerprint_seed, "")

    def test_roxy_import_never_falls_back_to_protocol(self):
        with patch.object(roxybrowser, "REGISTRATION_DRIVER", "roxy"), \
             patch("core.roxybrowser_client.RoxyBrowserClient", side_effect=RuntimeError("浏览器不可用")), \
             patch.object(liveness, "_login_via_full_web_flow", side_effect=RuntimeError("HTTP 403")) as protocol:
            result = liveness.login_account_with_credentials(
                "a@example.com", "password", "JBSWY3DPEHPK3PXP", proxy="",
            )
        protocol.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("浏览器不可用", result["error"])

    def test_protocol_import_ignores_registration_fingerprint_reuse(self):
        states = []

        def login(_email, _proxy, **kwargs):
            state = kwargs["fingerprint_state"]
            states.append(state)
            self.assertTrue(state.get("force_fresh"), "导入不应复用注册指纹")
            return MagicMock(proxy=""), {"accessToken": "test-token"}

        with patch.object(roxybrowser, "REGISTRATION_DRIVER", "protocol"), \
             patch.object(register, "PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL", True), \
             patch.object(liveness, "_login_via_full_web_flow", side_effect=login), \
             patch.object(liveness, "_safe_fingerprint_for_account", return_value={}), \
             patch.object(liveness, "_safe_fingerprint_text_for_account", return_value=""), \
             patch.object(liveness, "close_browser_session"):
            for email in ("a@example.com", "b@example.com", "a@example.com"):
                result = liveness.login_account_with_credentials(email, "password", "JBSWY3DPEHPK3PXP", proxy="")
                self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(len({id(state) for state in states}), 3)

    def test_roxy_import_uses_fresh_profiles_and_explicit_credentials(self):
        """通过真实导入入口走浏览器，登录结果来自浏览器而非协议请求。"""
        client = MagicMock()
        client.open_profile.side_effect = [
            SimpleNamespace(profile_id="fresh-a", raw={}),
            SimpleNamespace(profile_id="fresh-b", raw={}),
        ]
        driver = MagicMock()
        session_info = {"accessToken": "token", "user": {"email": "a@example.com"}}
        with patch.object(roxybrowser, "REGISTRATION_DRIVER", "roxy"), \
             patch.object(roxybrowser, "ROXY_KEEP_BROWSER_OPEN", False), \
             patch("core.roxybrowser_client.RoxyBrowserClient", return_value=client), \
             patch.object(pages, "_build_driver", return_value=driver), \
             patch.object(browser_login, "_login_on_page", return_value=session_info) as login, \
             patch.object(browser_login, "_fingerprint", return_value={"driver": "roxy"}), \
             patch.object(liveness, "_login_via_full_web_flow") as protocol:
            for _ in range(2):
                result = liveness.login_account_with_credentials(
                    "a@example.com", "new-password", "JBSWY3DPEHPK3PXP", proxy="http://relay:8080",
                )
                self.assertTrue(result["ok"], result.get("error"))
                self.assertEqual(result["access_token"], "token")
                self.assertEqual(result["proxy_used"], "http://relay:8080")
        protocol.assert_not_called()
        self.assertEqual(client.open_profile.call_count, 2)
        client.open_profile.assert_called_with(fresh=True, proxy="http://relay:8080")
        login.assert_called_with(driver, "a@example.com", "new-password", "JBSWY3DPEHPK3PXP")
        self.assertEqual(driver.quit.call_count, 2)
        self.assertEqual(client.cleanup_profile.call_count, 2)

    def test_browser_failure_closes_created_profile_and_redacts_secrets(self):
        client, driver = MagicMock(), MagicMock()
        with patch.object(roxybrowser, "REGISTRATION_DRIVER", "roxy"), \
             patch.object(roxybrowser, "ROXY_KEEP_BROWSER_OPEN", False), \
             patch("core.roxybrowser_client.RoxyBrowserClient", return_value=client), \
             patch.object(pages, "_build_driver", return_value=driver), \
             patch.object(browser_login, "_login_on_page", side_effect=RuntimeError("bad-password JBSWY3DPEHPK3PXP")):
            result = liveness.login_account_with_credentials("a@example.com", "bad-password", "JBSWY3DPEHPK3PXP", proxy="")
        self.assertFalse(result["ok"])
        self.assertNotIn("bad-password", result["error"])
        self.assertNotIn("JBSWY3DPEHPK3PXP", result["error"])
        driver.quit.assert_called_once()
        client.cleanup_profile.assert_called_once_with(client.open_profile.return_value, retain=False)

    def test_browser_email_otp_is_reported_without_saving_or_protocol_fallback(self):
        with patch.object(roxybrowser, "REGISTRATION_DRIVER", "cloak"), \
             patch.object(browser_login, "login_with_browser", side_effect=liveness.EmailOtpRequiredError), \
             patch.object(liveness, "_login_via_full_web_flow") as protocol:
            result = liveness.login_account_with_credentials("a@example.com", "password", "JBSWY3DPEHPK3PXP")
        self.assertEqual(result["status"], "needs_email_otp")
        protocol.assert_not_called()

    def test_unknown_driver_does_not_silently_use_protocol(self):
        with patch.object(roxybrowser, "REGISTRATION_DRIVER", "unknown"), \
             patch.object(liveness, "_login_via_full_web_flow") as protocol:
            result = liveness.login_account_with_credentials("a@example.com", "password", "JBSWY3DPEHPK3PXP")
        self.assertFalse(result["ok"])
        self.assertIn("不支持的注册方式", result["error"])
        protocol.assert_not_called()

    def test_cloak_import_requests_nonpersistent_random_environment(self):
        driver = MagicMock()
        opened = SimpleNamespace(profile_id="cloak", raw={})
        with patch("core.cloakbrowser_driver.build_cloak_driver", return_value=(driver, opened)) as build, \
             patch("config.cloakbrowser.CLOAK_KEEP_BROWSER_OPEN", False):
            with browser_login._browser_environment("cloak", "") as (actual, _, proxy):
                self.assertIs(actual, driver)
                self.assertEqual(proxy, "")
        build.assert_called_once_with(proxy="", fresh=True)
        driver.quit.assert_called_once()

    def test_browser_session_for_different_email_is_rejected(self):
        @contextmanager
        def environment(*_args):
            yield MagicMock(), "new-profile", ""

        with patch.object(browser_login, "_browser_environment", environment), \
             patch.object(browser_login, "_login_on_page", return_value={"accessToken": "token", "user": {"email": "b@example.com"}}):
            with self.assertRaisesRegex(RuntimeError, "邮箱.*不一致"):
                browser_login.login_with_browser("a@example.com", "password", "totp", proxy="", driver_mode="roxy")

    def test_existing_password_page_is_allowed_only_for_login(self):
        with patch.object(pages, "_current_email_submit_next_state", return_value="login_password"):
            self.assertEqual(pages._submit_email_and_wait_next(MagicMock(), "a@example.com", allow_existing=True), "login_password")
            with self.assertRaisesRegex(RuntimeError, "已注册"):
                pages._submit_email_and_wait_next(MagicMock(), "a@example.com")

    def test_password_and_mfa_use_imported_credentials_without_db_lookup(self):
        driver = MagicMock()
        driver.execute_script.return_value = {"ok": True, "input": "input", "button": "submit"}
        with patch.object(login_pages, "_account_password_for_email", side_effect=AssertionError("禁止读取数据库密码")), \
             patch.object(login_pages, "_account_totp_code_for_email", side_effect=AssertionError("禁止读取数据库2FA")), \
             patch.object(login_pages, "_is_email_verification_page", return_value=False), \
             patch.object(login_pages, "_is_login_password_page", return_value=True), \
             patch.object(login_pages, "_is_mfa_challenge_page", return_value=True), \
             patch.object(login_pages, "_human_type_text") as fill, \
             patch.object(login_pages, "_human_click"), \
             patch.object(login_pages, "human_delay"), \
             patch.object(login_pages, "_fill_mfa_challenge_if_present", return_value=True) as mfa:
            login_pages._fill_login_password_if_present(driver, "a@example.com", password="import-password", totp_secret="import-totp")
        fill.assert_called_once_with(driver, "input", "import-password", clear=True)
        mfa.assert_called_once_with(driver, "a@example.com", timeout=15, totp_secret="import-totp")

    def test_browser_page_login_completes_password_mfa_session_flow(self):
        driver = MagicMock()
        driver.current_url = "https://auth.openai.com/log-in/password"
        info = {"accessToken": "token"}

        def after_password(*_args, **_kwargs):
            driver.current_url = "https://auth.openai.com/mfa-challenge/totp"
            return "next_step"

        def after_mfa(*_args, **_kwargs):
            driver.current_url = "https://chatgpt.com/"
            return True

        with patch.object(pages, "_check_manual_stop"), \
             patch.object(pages, "_maybe_accept"), \
             patch.object(pages, "_submit_email_and_wait_next") as submit, \
             patch.object(pages, "_read_chatgpt_session_once", return_value=info), \
             patch.object(pages, "_is_login_password_page", side_effect=lambda _: "/log-in/password" in driver.current_url), \
             patch.object(login_pages, "_is_mfa_challenge_page", side_effect=lambda _: "/mfa-challenge" in driver.current_url), \
             patch.object(login_pages, "_fill_login_password_if_present", side_effect=after_password), \
             patch.object(login_pages, "_fill_mfa_challenge_if_present", side_effect=after_mfa), \
             patch.object(browser_login.time, "sleep"):
            # 密码函数内部通常处理 MFA，此处模拟 MFA 稍后出现的边界。
            with patch.object(login_pages, "_is_mfa_challenge_page", side_effect=[False, False, True]):
                result = browser_login._login_on_page(driver, "a@example.com", "password", "secret")
        self.assertEqual(result, info)
        submit.assert_called_once_with(driver, "a@example.com", allow_existing=True)

    def test_password_link_waits_for_slow_navigation_instead_of_returning_email_otp(self):
        driver = MagicMock()
        driver.current_url = "https://auth.openai.com/email-verification"
        sleeps = []

        def advance(_delay):
            sleeps.append(_delay)
            if len(sleeps) == 2:
                driver.current_url = "https://chatgpt.com/"

        with patch.object(pages, "_check_manual_stop"), \
             patch.object(pages, "_maybe_accept"), \
             patch.object(pages, "_submit_email_and_wait_next"), \
             patch.object(pages, "_read_chatgpt_session_once", return_value={"accessToken": "token"}), \
             patch.object(pages, "_is_login_password_page", return_value=False), \
             patch.object(login_pages, "_is_mfa_challenge_page", return_value=False), \
             patch.object(pages, "_is_email_verification_page", return_value=True), \
             patch.object(browser_login, "_choose_login_password", return_value=True) as choose, \
             patch.object(browser_login.time, "sleep", side_effect=advance):
            info = browser_login._login_on_page(driver, "a@example.com", "password", "secret")
        self.assertEqual(info["accessToken"], "token")
        choose.assert_called_once()


class RoxyImportEnvironmentTests(unittest.TestCase):
    def test_delete_profile_includes_single_profile_id_required_by_roxy(self):
        client = RoxyBrowserClient(token="")
        with patch.object(client, "request", return_value={"code": 0}) as request:
            client.delete_profile("12345")
        body = request.call_args.kwargs["json_body"]
        self.assertEqual(body["dirId"], 12345)
        self.assertEqual(body["dirIds"], [12345])

    def test_new_profile_ignores_fixed_profile_and_passes_selected_proxy(self):
        client = RoxyBrowserClient(token="")
        with patch.object(roxybrowser, "ROXY_PROFILE_ID", "other-account-profile"), \
             patch.object(roxybrowser, "ROXY_ONE_PROFILE_PER_ACCOUNT", False), \
             patch.object(roxybrowser, "ROXY_OPEN_EXTRA_PARAMS", {"dirId": "wrong-profile"}), \
             patch.object(client, "create_profile", return_value="new-profile") as create, \
             patch.object(client, "request", return_value={"debuggerAddress": "127.0.0.1:9222"}) as request:
            opened = client.open_profile(fresh=True, proxy="socks5://selected:1080")
        create.assert_called_once_with(proxy="socks5://selected:1080")
        self.assertEqual(opened.profile_id, "new-profile")
        self.assertTrue(opened.created_by_run)
        self.assertEqual(request.call_args.kwargs["json_body"]["dirId"], "new-profile")

    def test_explicit_proxy_overrides_payload_and_never_picks_registration_pool(self):
        for proxy in ("", "http://127.0.0.1:8080"):
            with self.subTest(proxy=proxy):
                client = RoxyBrowserClient(token="")
                with patch.object(roxybrowser, "ROXY_CREATE_USE_PROXY_POOL", True), \
                     patch.object(roxybrowser, "ROXY_PROFILE_CREATE_PAYLOAD", {"proxyInfo": {"host": "wrong-proxy"}}), \
                     patch("config.proxy.pick_proxy", side_effect=AssertionError("禁止抽取注册代理池")), \
                     patch("core.roxybrowser_client._wait_for_create_slot"), \
                     patch.object(client, "request", return_value={"dirId": "new-profile"}) as request:
                    client.create_profile(proxy=proxy)
                body = request.call_args.kwargs["json_body"]
                if proxy:
                    self.assertEqual(body["proxyInfo"]["host"], "127.0.0.1")
                    self.assertEqual(body["proxyInfo"]["port"], "8080")
                else:
                    self.assertEqual(body["proxyInfo"]["proxyCategory"], "noproxy")
                    self.assertNotIn("host", body["proxyInfo"])

    def test_open_failure_cleans_created_profile(self):
        client = RoxyBrowserClient(token="")
        with patch.object(client, "create_profile", return_value="fresh-profile"), \
             patch.object(client, "request", side_effect=RuntimeError("open failed")), \
             patch.object(client, "cleanup_profile") as cleanup:
            with self.assertRaisesRegex(RuntimeError, "open failed"):
                client.open_profile(fresh=True, proxy="")
        self.assertEqual(cleanup.call_args.args[0].profile_id, "fresh-profile")


class OtherBrowserIsolationTests(unittest.TestCase):
    def test_cloak_fresh_launch_ignores_persistent_directory_and_fixed_seed(self):
        from core import cloakbrowser_driver as cloak

        module = MagicMock()
        with patch.dict(sys.modules, {"cloakbrowser": module}), \
             patch.object(cloak._cfg, "CLOAK_USER_DATA_DIR", "/other-account"), \
             patch.object(cloak._cfg, "CLOAK_FINGERPRINT_SEED", "123"), \
             patch.object(cloak._cfg, "CLOAK_EXTRA_ARGS", ["--fingerprint=456", "--disable-gpu"]), \
             patch.object(cloak, "_build_cloak_locale_options", return_value={}):
            for _ in range(2):
                driver, _ = cloak.build_cloak_driver(proxy="", fresh=True)
                driver.quit()
        module.launch_persistent_context.assert_not_called()
        calls = module.launch.call_args_list
        seeds = [[arg for arg in call.kwargs["args"] if arg.startswith("--fingerprint=")] for call in calls]
        self.assertEqual([len(seed) for seed in seeds], [1, 1])
        self.assertNotEqual(seeds[0], seeds[1])
        self.assertNotEqual(seeds[0], ["--fingerprint=123"])
        self.assertNotEqual(seeds[0], ["--fingerprint=456"])
        self.assertIn("--disable-gpu", calls[0].kwargs["args"])

    def test_browser_use_fresh_session_does_not_load_configured_profile(self):
        from core import browser_use_client as bu
        from urllib.parse import parse_qs, urlparse

        with patch.object(bu._cfg, "BROWSER_USE_PROFILE_ID", "other-account"), \
             patch.object(bu._cfg, "BROWSER_USE_EXTRA_QUERY", {"profileId": "also-other-account"}):
            opened = bu.BrowserUseClient(api_key="test-key").open_session(fresh=True)
        self.assertNotIn("profileId", parse_qs(urlparse(opened.connect_url).query))
        self.assertEqual(opened.profile_id, "")

    def test_skyvern_fresh_session_does_not_load_configured_profile(self):
        from core import skyvern_client as skyvern

        response = MagicMock(status_code=200)
        response.json.return_value = {"session_id": "new-session", "browser_address": "http://example.test:9222"}
        with patch.object(skyvern._cfg, "SKYVERN_BROWSER_PROFILE_ID", "other-account"), \
             patch.object(skyvern.requests, "post", return_value=response) as post:
            opened = skyvern.SkyvernClient(api_key="test-key").open_session(fresh=True)
        self.assertNotIn("browser_profile_id", post.call_args.kwargs["json"])
        self.assertEqual(opened.profile_id, "")


if __name__ == "__main__":
    unittest.main()
