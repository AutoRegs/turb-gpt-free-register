# -*- coding: utf-8 -*-
"""Codex 授权环境生命周期与根据 ROXY_DELETE_PROFILE_AFTER_RUN 保留/删除的测试。"""
import unittest
from unittest.mock import MagicMock, patch

from config import roxybrowser as roxy_cfg
from core import db
from core.roxy_codex_oauth import _run_roxy_codex_oauth_once
from core.roxybrowser_client import RoxyBrowserClient, RoxyOpenResult


class CodexProfileCleanupTests(unittest.TestCase):
    def setUp(self):
        self.email = "codex.test@example.com"

    @patch("core.roxy_codex_oauth._detect_browser_kind", return_value="Roxy")
    @patch("core.roxy_codex_oauth._build_driver")
    @patch("core.roxy_codex_oauth._center_browser_window")
    @patch("core.roxy_codex_oauth._fill_email_and_otp")
    @patch("core.roxy_codex_oauth._do_phone_verification_if_present")
    @patch("core.roxy_codex_oauth._finish_consent_workspace", return_value="http://localhost:1455/auth/callback?code=test-code&state=st123")
    @patch("core.codex_oauth._codex_auth_url_source", return_value="cpa")
    @patch("core.codex_oauth._request_cpa_authorize_url", return_value={"state": "st123", "auth_url": "https://auth.openai.com/authorize"})
    @patch("core.codex_oauth._submit_cpa_callback", return_value={"status": 200, "message": "cpa ok"})
    @patch("core.codex_oauth._save_cpa_local_record", return_value="/tmp/test.json")
    def test_codex_oauth_retains_profile_when_delete_after_run_false(
        self, mock_save, mock_submit, mock_cpa_auth, mock_auth_source, mock_finish,
        mock_phone, mock_fill, mock_center, mock_driver, mock_detect
    ):
        """当 ROXY_DELETE_PROFILE_AFTER_RUN=False 时，Codex 授权成功后不删除环境，只关闭窗口。"""
        fake_opened = RoxyOpenResult(profile_id="new-profile-123", raw={}, created_by_run=True)
        fake_driver = MagicMock()
        mock_driver.return_value = fake_driver

        with patch.object(roxy_cfg, "ROXY_DELETE_PROFILE_AFTER_RUN", False), \
             patch.object(roxy_cfg, "ROXY_KEEP_BROWSER_OPEN", False), \
             patch.object(db, "get_account_by_email", return_value={"id": 999, "email": self.email}), \
             patch.object(db, "get_account_roxy_profile_id", return_value=""), \
             patch.object(db, "bind_account_roxy_profile") as mock_bind, \
             patch.object(RoxyBrowserClient, "open_profile", return_value=fake_opened), \
             patch.object(RoxyBrowserClient, "close_profile") as mock_close, \
             patch.object(RoxyBrowserClient, "delete_profile") as mock_delete, \
             patch.object(RoxyBrowserClient, "close_proxy_pool_relay"):

            res = _run_roxy_codex_oauth_once(self.email, force=True)
            self.assertTrue(res.get("ok"))
            self.assertEqual(res.get("status"), "success")

            # 环境应被关闭
            mock_close.assert_called_once_with("new-profile-123")
            # 因为 ROXY_DELETE_PROFILE_AFTER_RUN=False，delete_profile 绝不能被调用！
            mock_delete.assert_not_called()
            # 成功后应将环境绑定到账号
            mock_bind.assert_called_once_with(999, self.email, "new-profile-123")

    @patch("core.roxy_codex_oauth._detect_browser_kind", return_value="Roxy")
    @patch("core.roxy_codex_oauth._build_driver")
    @patch("core.roxy_codex_oauth._center_browser_window")
    @patch("core.roxy_codex_oauth._fill_email_and_otp")
    @patch("core.roxy_codex_oauth._do_phone_verification_if_present")
    @patch("core.roxy_codex_oauth._finish_consent_workspace", return_value="http://localhost:1455/auth/callback?code=test-code&state=st123")
    @patch("core.codex_oauth._codex_auth_url_source", return_value="cpa")
    @patch("core.codex_oauth._request_cpa_authorize_url", return_value={"state": "st123", "auth_url": "https://auth.openai.com/authorize"})
    @patch("core.codex_oauth._submit_cpa_callback", return_value={"status": 200, "message": "cpa ok"})
    @patch("core.codex_oauth._save_cpa_local_record", return_value="/tmp/test.json")
    def test_codex_oauth_deletes_profile_when_delete_after_run_true(
        self, mock_save, mock_submit, mock_cpa_auth, mock_auth_source, mock_finish,
        mock_phone, mock_fill, mock_center, mock_driver, mock_detect
    ):
        """当 ROXY_DELETE_PROFILE_AFTER_RUN=True 时，Codex 授权成功后按配置删除临时环境。"""
        fake_opened = RoxyOpenResult(profile_id="new-profile-456", raw={}, created_by_run=True)
        fake_driver = MagicMock()
        mock_driver.return_value = fake_driver

        with patch.object(roxy_cfg, "ROXY_DELETE_PROFILE_AFTER_RUN", True), \
             patch.object(roxy_cfg, "ROXY_KEEP_BROWSER_OPEN", False), \
             patch.object(db, "get_account_by_email", return_value={"id": 999, "email": self.email}), \
             patch.object(db, "get_account_roxy_profile_id", return_value=""), \
             patch.object(db, "bind_account_roxy_profile"), \
             patch.object(RoxyBrowserClient, "open_profile", return_value=fake_opened), \
             patch.object(RoxyBrowserClient, "close_profile") as mock_close, \
             patch.object(RoxyBrowserClient, "delete_profile") as mock_delete, \
             patch.object(RoxyBrowserClient, "close_proxy_pool_relay"):

            res = _run_roxy_codex_oauth_once(self.email, force=True)
            self.assertTrue(res.get("ok"))
            self.assertEqual(res.get("status"), "success")

            mock_close.assert_called_once_with("new-profile-456")
            mock_delete.assert_called_once_with("new-profile-456")

    @patch("core.roxy_codex_oauth._detect_browser_kind", return_value="Roxy")
    @patch("core.roxy_codex_oauth._build_driver")
    @patch("core.roxy_codex_oauth._center_browser_window")
    @patch("core.roxy_codex_oauth._fill_email_and_otp")
    @patch("core.roxy_codex_oauth._do_phone_verification_if_present")
    @patch("core.roxy_codex_oauth._finish_consent_workspace", return_value="http://localhost:1455/auth/callback?code=test-code&state=st123")
    @patch("core.codex_oauth._codex_auth_url_source", return_value="cpa")
    @patch("core.codex_oauth._request_cpa_authorize_url", return_value={"state": "st123", "auth_url": "https://auth.openai.com/authorize"})
    @patch("core.codex_oauth._submit_cpa_callback", return_value={"status": 200, "message": "cpa ok"})
    @patch("core.codex_oauth._save_cpa_local_record", return_value="/tmp/test.json")
    def test_codex_oauth_reuses_existing_account_profile_without_deletion(
        self, mock_save, mock_submit, mock_cpa_auth, mock_auth_source, mock_finish,
        mock_phone, mock_fill, mock_center, mock_driver, mock_detect
    ):
        """当账号已有绑定的 Roxy 环境时，优先复用该环境，且无论开关状态如何绝不删除已有环境。"""
        # 复用已有环境，created_by_run 应为 False
        reused_opened = RoxyOpenResult(profile_id="existing-profile-789", raw={}, created_by_run=False)
        fake_driver = MagicMock()
        mock_driver.return_value = fake_driver

        with patch.object(roxy_cfg, "ROXY_DELETE_PROFILE_AFTER_RUN", True), \
             patch.object(roxy_cfg, "ROXY_KEEP_BROWSER_OPEN", False), \
             patch.object(db, "get_account_by_email", return_value={"id": 888, "email": self.email}), \
             patch.object(db, "get_account_roxy_profile_id", return_value="existing-profile-789"), \
             patch.object(RoxyBrowserClient, "open_profile", return_value=reused_opened) as mock_open, \
             patch.object(RoxyBrowserClient, "close_profile") as mock_close, \
             patch.object(RoxyBrowserClient, "delete_profile") as mock_delete, \
             patch.object(RoxyBrowserClient, "close_proxy_pool_relay"):

            res = _run_roxy_codex_oauth_once(self.email, force=True)
            self.assertTrue(res.get("ok"))

            # 验证是以 reuse=True 打开账号原有环境
            mock_open.assert_called_once_with("existing-profile-789", reuse=True, proxy=None)
            # 结束后仅关闭窗口，已有环境绝不被删除
            mock_close.assert_called_once_with("existing-profile-789")
            mock_delete.assert_not_called()

    @patch("core.roxy_codex_oauth._detect_browser_kind", return_value="Roxy")
    @patch("core.roxy_codex_oauth._build_driver")
    @patch("core.roxy_codex_oauth._center_browser_window")
    @patch("core.roxy_codex_oauth._do_phone_verification_if_present")
    @patch("core.roxy_codex_oauth._finish_consent_workspace", return_value="http://localhost:1455/auth/callback?code=test-code&state=st123")
    @patch("core.codex_oauth._codex_auth_url_source", return_value="cpa")
    @patch("core.codex_oauth._request_cpa_authorize_url", return_value={"state": "st123", "auth_url": "https://auth.openai.com/authorize"})
    @patch("core.codex_oauth._submit_cpa_callback", return_value={"status": 200, "message": "cpa ok"})
    @patch("core.codex_oauth._save_cpa_local_record", return_value="/tmp/test.json")
    def test_choose_an_account_page_auto_clicks_and_proceeds(
        self, mock_save, mock_submit, mock_cpa_auth, mock_auth_source, mock_finish,
        mock_phone, mock_center, mock_driver, mock_detect
    ):
        """验证处于 choose-an-account 页面时，自动点击账号卡片并顺利流转。"""
        reused_opened = RoxyOpenResult(profile_id="account-profile-choose", raw={}, created_by_run=False)
        fake_driver = MagicMock()
        fake_driver.current_url = "https://auth.openai.com/choose-an-account"
        mock_driver.return_value = fake_driver

        def fake_execute_script(script, *args):
            if "choose-an-account" in script:
                return True
            return {"ok": True, "clicked": True, "text": self.email}

        fake_driver.execute_script.side_effect = fake_execute_script

        with patch.object(roxy_cfg, "ROXY_DELETE_PROFILE_AFTER_RUN", False), \
             patch.object(roxy_cfg, "ROXY_KEEP_BROWSER_OPEN", False), \
             patch.object(db, "get_account_by_email", return_value={"id": 789, "email": self.email}), \
             patch.object(db, "get_account_roxy_profile_id", return_value="account-profile-choose"), \
             patch.object(RoxyBrowserClient, "open_profile", return_value=reused_opened), \
             patch.object(RoxyBrowserClient, "close_profile") as mock_close, \
             patch.object(RoxyBrowserClient, "delete_profile") as mock_delete, \
             patch.object(RoxyBrowserClient, "close_proxy_pool_relay"):

            res = _run_roxy_codex_oauth_once(self.email, force=True)
            self.assertTrue(res.get("ok"))
            self.assertEqual(res.get("status"), "success")
            mock_close.assert_called_once_with("account-profile-choose")
            mock_delete.assert_not_called()

if __name__ == "__main__":
    unittest.main()
