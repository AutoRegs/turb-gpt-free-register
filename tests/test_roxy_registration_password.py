# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

from core import roxy_registration as rr


class RoxyRegistrationPasswordTests(unittest.TestCase):
    def test_signup_password_page_uses_inherited_password_and_calls_callback(self):
        mock_driver = MagicMock()
        mock_driver.execute_script.side_effect = [
            {"inputs": [], "buttons": []},  # _password_page_state
            {"ok": True, "input": MagicMock(), "button": MagicMock()},  # password_targets
            {"ok": True, "button": MagicMock()},  # enabled_submit_target
        ]

        on_password_set = MagicMock()

        with patch.object(rr, "_is_email_verification_page", side_effect=[False, True]), \
             patch.object(rr, "_has_access_token", return_value=False), \
             patch.object(rr, "_is_signup_password_page", return_value=True), \
             patch.object(rr, "_is_login_password_page", return_value=False), \
             patch.object(rr, "_human_type_text"), \
             patch.object(rr, "human_delay"), \
             patch.object(rr, "_human_click"):

            result_pwd = rr._fill_password_page_if_present(
                mock_driver,
                "test@example.com",
                timeout=5,
                on_password_set=on_password_set,
                inherited_password="InheritedSecret123!",
            )

        self.assertEqual(result_pwd, "InheritedSecret123!")
        on_password_set.assert_called_with("InheritedSecret123!")

    def test_login_password_page_tries_inherited_password_first(self):
        mock_driver = MagicMock()

        on_password_set = MagicMock()

        with patch.object(rr, "_is_email_verification_page", side_effect=[False, True]), \
             patch.object(rr, "_has_access_token", return_value=False), \
             patch.object(rr, "_is_signup_password_page", return_value=False), \
             patch.object(rr, "_is_login_password_page", side_effect=[True, False]), \
             patch.object(rr, "_submit_login_password", return_value={"ok": True}), \
             patch("time.sleep"):

            result_pwd = rr._fill_password_page_if_present(
                mock_driver,
                "test@example.com",
                timeout=5,
                on_password_set=on_password_set,
                inherited_password="ExistingLoginPass888!",
            )

        self.assertEqual(result_pwd, "ExistingLoginPass888!")
        on_password_set.assert_called_with("ExistingLoginPass888!")

    def test_login_password_page_passwordless_fallback_preserves_inherited_password(self):
        mock_driver = MagicMock()

        with patch.object(rr, "_is_email_verification_page", side_effect=[False, False, True]), \
             patch.object(rr, "_has_access_token", return_value=False), \
             patch.object(rr, "_is_signup_password_page", return_value=False), \
             patch.object(rr, "_is_login_password_page", return_value=True), \
             patch.object(rr, "_submit_login_password", return_value={"ok": False}), \
             patch.object(rr, "_click_passwordless_signup_if_present", return_value={"ok": True}), \
             patch("time.sleep"):

            result_pwd = rr._fill_password_page_if_present(
                mock_driver,
                "test@example.com",
                timeout=5,
                inherited_password="FallbackPass456!",
            )

        self.assertEqual(result_pwd, "FallbackPass456!")
    def test_run_roxy_registration_cleanup_profile_retains_on_success_and_deletes_on_failure(self):
        mock_opened = MagicMock(profile_id="p-success-1", raw={})
        mock_client = MagicMock()
        mock_client.open_profile.return_value = mock_opened
        mock_driver = MagicMock()

        with patch.object(rr, "RoxyBrowserClient", return_value=mock_client), \
             patch.object(rr, "_build_driver", return_value=mock_driver), \
             patch.object(rr, "RoxyLocalAssetCache"), \
             patch.object(rr, "SeleniumTrafficTracker"), \
             patch.object(rr, "BrowserDataSaver"), \
             patch.object(rr, "_maybe_accept"), \
             patch.object(rr, "_submit_email_and_wait_next"), \
             patch.object(rr, "_fill_password_page_if_present", return_value="pwd123"), \
             patch.object(rr, "wait_for_otp", return_value="123456"), \
             patch.object(rr, "_clear_otp_inputs"), \
             patch.object(rr, "_type_otp"), \
             patch.object(rr, "_click_continue"), \
             patch.object(rr, "_wait_after_email_otp_submit", return_value="accepted"), \
             patch.object(rr, "_complete_profile_page"), \
             patch.object(rr, "_fetch_chatgpt_session", return_value={"accessToken": "tok-123", "user": {}, "account": {}, "expires": "2026-11-01"}), \
             patch.object(rr, "post_register_dwell"), \
             patch.object(rr, "save_account_data", return_value=888), \
             patch("time.sleep"):

            res = rr.run_roxy_registration("succ@example.com", "Test User", "1995-01-01")
            self.assertTrue(res["success"])
            # 验证成功时 retain=True
            mock_client.cleanup_profile.assert_called_with(mock_opened, retain=True)

        # 失败场景
        mock_client_fail = MagicMock()
        mock_opened_fail = MagicMock(profile_id="p-fail-1", raw={})
        mock_client_fail.open_profile.return_value = mock_opened_fail
        with patch.object(rr, "RoxyBrowserClient", return_value=mock_client_fail), \
             patch.object(rr, "_build_driver", side_effect=RuntimeError("connection error")):
            res_fail = rr.run_roxy_registration("fail@example.com", "Fail User", "1995-01-01")
            self.assertFalse(res_fail["success"])
            # 验证失败时 retain=False
            mock_client_fail.cleanup_profile.assert_called_with(mock_opened_fail, retain=False)


if __name__ == "__main__":
    unittest.main()
