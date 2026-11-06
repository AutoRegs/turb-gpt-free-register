# -*- coding: utf-8 -*-
"""Roxy 账号环境复用的 API 参数、代理更新和清理边界。"""
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from config import roxybrowser as cfg
from core.roxybrowser_client import RoxyBrowserClient, RoxyOpenResult, RoxyProfileMissingError, RoxyProfileReuseUnavailableError


class RoxyProfileReuseTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.multiple(cfg, ROXY_ONE_PROFILE_PER_ACCOUNT=True,
            ROXY_PROFILE_ID="other-account", ROXY_KEEP_BROWSER_OPEN=False,
            ROXY_DELETE_PROFILE_AFTER_RUN=True, ROXY_OPEN_EXTRA_PARAMS={"dirId": "wrong-profile"}))
        self.client = RoxyBrowserClient(token="")
        self.create = stack.enter_context(patch.object(self.client, "create_profile"))
        self.request = stack.enter_context(patch.object(self.client, "request", side_effect=self.response))

    def response(self, method, path, **kwargs):
        if path == "/browser/detail":
            return {"code": 0, "data": {"rows": [{"dirId": "account-profile"}]}}
        if path == cfg.ROXY_OPEN_PATH:
            return {"debuggerAddress": "127.0.0.1:9222"}
        return {"code": 0}

    def test_reuse_bypasses_global_profile_and_updates_only_selected_proxy(self):
        opened = self.client.open_profile("account-profile", reuse=True, proxy="socks5://selected:1080")
        self.assertFalse(opened.created_by_run)
        self.assertEqual(opened.profile_id, "account-profile")
        self.create.assert_not_called()
        calls = self.request.call_args_list
        self.assertEqual([call.args[1] for call in calls], ["/browser/detail", "/browser/mdf", cfg.ROXY_OPEN_PATH])
        modified = calls[1].kwargs["json_body"]
        self.assertEqual(set(modified), {"workspaceId", "dirId", "proxyInfo"})
        self.assertEqual(modified["dirId"], "account-profile")
        self.assertEqual(modified["proxyInfo"]["host"], "selected")
        self.assertEqual(calls[2].kwargs["json_body"]["dirId"], "account-profile")

    def test_explicit_direct_route_clears_the_old_proxy(self):
        self.client.open_profile("account-profile", reuse=True, proxy="")
        modified = self.request.call_args_list[1].kwargs["json_body"]
        self.assertEqual(modified["proxyInfo"]["proxyCategory"], "noproxy")
        self.assertNotIn("host", modified["proxyInfo"])

    def test_unspecified_proxy_preserves_the_existing_route(self):
        self.client.open_profile("account-profile", reuse=True)
        self.assertEqual([call.args[1] for call in self.request.call_args_list], ["/browser/detail", cfg.ROXY_OPEN_PATH])

    def test_empty_detail_means_profile_missing_without_deleting_any_profile(self):
        self.request.return_value = {"code": 0, "data": {"total": 0, "rows": []}}
        self.request.side_effect = None
        with patch.object(self.client, "delete_profile") as delete:
            with self.assertRaises(RoxyProfileMissingError):
                self.client.open_profile("account-profile", reuse=True, proxy="")
        self.create.assert_not_called()
        delete.assert_not_called()

    def test_temporary_detail_error_is_not_classified_as_profile_deletion(self):
        self.request.side_effect = RuntimeError("Roxy API HTTP 503")
        with self.assertRaisesRegex(RuntimeError, "503") as error:
            self.client.open_profile("account-profile", reuse=True, proxy="")
        self.assertNotIsInstance(error.exception, RoxyProfileMissingError)
        self.create.assert_not_called()

    def test_detail_404_104_uses_profile_list_to_reuse_an_existing_environment(self):
        def response(method, path, **kwargs):
            if path == "/browser/detail":
                raise RuntimeError('Roxy API 请求失败 GET /browser/detail HTTP 404: {"code":104,"msg":"Not Found","data":null}')
            if path == "/browser/list_v3":
                return {"code": 0, "data": {"total": 1, "rows": [{"dirId": "account-profile"}]}}
            return self.response(method, path, **kwargs)

        self.request.side_effect = response
        opened = self.client.open_profile("account-profile", reuse=True, proxy="socks5://selected:1080")
        self.assertEqual(opened.profile_id, "account-profile")
        self.assertFalse(opened.created_by_run)
        self.create.assert_not_called()
        self.assertEqual([call.args[1] for call in self.request.call_args_list], [
            "/browser/detail", "/browser/list_v3", "/browser/mdf", cfg.ROXY_OPEN_PATH,
        ])

    def test_detail_404_104_and_empty_profile_list_allow_creating_a_replacement(self):
        def response(method, path, **kwargs):
            if path == "/browser/detail":
                raise RuntimeError('Roxy API 请求失败 GET /browser/detail HTTP 404: {"code":104,"msg":"Not Found","data":null}')
            if path == "/browser/list_v3":
                return {"code": 0, "data": {"total": 0, "rows": []}}
            return self.response(method, path, **kwargs)

        self.request.side_effect = response
        with self.assertRaises(RoxyProfileMissingError):
            self.client.open_profile("account-profile", reuse=True, proxy="socks5://selected:1080")
        self.create.assert_not_called()

    def test_legacy_list_is_used_when_detail_and_v3_are_unavailable(self):
        def response(method, path, **kwargs):
            if path in {"/browser/detail", "/browser/list_v3"}:
                raise RuntimeError(f"Roxy API 请求失败 GET {path} HTTP 404: Not Found")
            if path == "/browser/list":
                self.assertEqual(kwargs["params"]["dirIds"], "account-profile")
                return {"code": 0, "data": {"total": 2, "rows": [
                    {"dirId": "other-account-profile"}, {"dirId": "account-profile"},
                ]}}
            return self.response(method, path, **kwargs)

        self.request.side_effect = response
        opened = self.client.open_profile("account-profile", reuse=True, proxy="")
        self.assertEqual(opened.profile_id, "account-profile")
        self.create.assert_not_called()

    def test_paginated_list_is_exhausted_before_declaring_profile_missing(self):
        def response(method, path, **kwargs):
            if path == "/browser/detail":
                raise RuntimeError("Roxy API HTTP 404")
            if path == "/browser/list_v3":
                row_id = "other-account-profile" if kwargs["params"]["page_index"] == 1 else "account-profile"
                return {"code": 0, "data": {"total": 2, "rows": [{"dirId": row_id}]}}
            return self.response(method, path, **kwargs)

        self.request.side_effect = response
        self.assertEqual(self.client.get_profile_detail("account-profile")["dirId"], "account-profile")
        pages = [call.kwargs["params"]["page_index"] for call in self.request.call_args_list if call.args[1] == "/browser/list_v3"]
        self.assertEqual(pages, [1, 2])

    def test_unavailable_query_endpoints_do_not_prevent_direct_reuse(self):
        def response(method, path, **kwargs):
            if path in {"/browser/detail", "/browser/list_v3", "/browser/list"}:
                raise RuntimeError("Roxy API HTTP 404")
            return self.response(method, path, **kwargs)

        self.request.side_effect = response
        self.assertEqual(self.client.open_profile("account-profile", reuse=True, proxy="").profile_id, "account-profile")
        self.create.assert_not_called()

    def test_unsupported_proxy_update_is_classified_for_replacement_without_deleting_the_old_profile(self):
        def response(method, path, **kwargs):
            if path == "/browser/mdf":
                raise RuntimeError("Roxy API HTTP 404")
            return self.response(method, path, **kwargs)

        self.request.side_effect = response
        with patch.object(self.client, "delete_profile") as delete:
            with self.assertRaises(RoxyProfileReuseUnavailableError):
                self.client.open_profile("account-profile", reuse=True, proxy="")
        delete.assert_not_called()

    def test_authentication_errors_do_not_trigger_query_or_profile_fallback(self):
        self.request.side_effect = RuntimeError("Roxy API HTTP 401")
        with self.assertRaisesRegex(RuntimeError, "401"):
            self.client.open_profile("account-profile", reuse=True, proxy="")
        self.request.assert_called_once()
        self.create.assert_not_called()

    def test_cleanup_profile_when_delete_after_run_enabled(self):
        # 打开结束后删除环境时：成功与失败的临时环境都删除
        opened_succ = RoxyOpenResult("account-profile-succ", {}, created_by_run=True)
        opened_fail = RoxyOpenResult("account-profile-fail", {}, created_by_run=True)
        with patch.object(cfg, "ROXY_DELETE_PROFILE_AFTER_RUN", True), \
             patch.object(self.client, "close_profile") as close, \
             patch.object(self.client, "delete_profile") as delete:
            # 成功
            self.client.cleanup_profile(opened_succ, retain=True)
            close.assert_called_with("account-profile-succ")
            delete.assert_called_with("account-profile-succ")
            # 失败
            self.client.cleanup_profile(opened_fail, retain=False)
            close.assert_called_with("account-profile-fail")
            delete.assert_called_with("account-profile-fail")

    def test_cleanup_profile_when_delete_after_run_disabled(self):
        # 关闭结束后删除环境时：成功只关闭不删除，失败依然删除
        opened_succ = RoxyOpenResult("account-profile-succ", {}, created_by_run=True)
        opened_fail = RoxyOpenResult("account-profile-fail", {}, created_by_run=True)
        with patch.object(cfg, "ROXY_DELETE_PROFILE_AFTER_RUN", False), \
             patch.object(self.client, "close_profile") as close, \
             patch.object(self.client, "delete_profile") as delete:
            # 成功：只关闭不删除
            self.client.cleanup_profile(opened_succ, retain=True)
            close.assert_called_with("account-profile-succ")
            delete.assert_not_called()
            # 失败：依然删除
            self.client.cleanup_profile(opened_fail, retain=False)
            close.assert_called_with("account-profile-fail")
            delete.assert_called_once_with("account-profile-fail")

    def test_failed_reuse_open_closes_the_old_profile_without_deletion(self):
        def response(method, path, **kwargs):
            if path == cfg.ROXY_OPEN_PATH:
                raise RuntimeError("open failed")
            return self.response(method, path, **kwargs)

        self.request.side_effect = response
        with patch.object(self.client, "close_profile") as close, \
             patch.object(self.client, "delete_profile") as delete:
            with self.assertRaisesRegex(RuntimeError, "open failed"):
                self.client.open_profile("account-profile", reuse=True, proxy="")
        close.assert_called_once_with("account-profile")
        delete.assert_not_called()

    def test_reuse_requires_explicit_account_profile_id(self):
        for kwargs in ({"reuse": True}, {"profile_id": "account-profile", "reuse": True, "fresh": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.client.open_profile(**kwargs)
        self.request.assert_not_called()

class RoxyCreateProfileProxyTests(unittest.TestCase):
    def setUp(self):
        self.client = RoxyBrowserClient(token="")

    def test_create_profile_with_explicit_proxy(self):
        captured = {}
        def fake_request(method, path, **kwargs):
            captured["method"] = method
            captured["path"] = path
            captured["json_body"] = kwargs.get("json_body")
            return {"code": 0, "data": {"dirId": "p-123"}}

        with patch.object(self.client, "request", side_effect=fake_request), \
             patch.multiple(cfg, ROXY_WORKSPACE_ID="ws-1", ROXY_CREATE_USE_PROXY_POOL=False):
            pid = self.client.create_profile(proxy="socks5://prem.country.iprocket.io:9595")

        self.assertEqual(pid, "p-123")
        body = captured["json_body"]
        self.assertEqual(body["proxy"], "socks5://prem.country.iprocket.io:9595")
        self.assertEqual(body["proxyInfo"]["host"], "prem.country.iprocket.io")
        self.assertEqual(body["proxyInfo"]["port"], "9595")
        self.assertEqual(body["proxyInfo"]["protocol"], "SOCKS5")
        self.assertEqual(self.client._proxy_pool_target, "socks5://prem.country.iprocket.io:9595")

    def test_create_profile_with_explicit_direct(self):
        captured = {}
        def fake_request(method, path, **kwargs):
            captured["json_body"] = kwargs.get("json_body")
            return {"code": 0, "data": {"dirId": "p-direct"}}

        with patch.object(self.client, "request", side_effect=fake_request), \
             patch.multiple(cfg, ROXY_WORKSPACE_ID="ws-1", ROXY_CREATE_USE_PROXY_POOL=True):
            pid = self.client.create_profile(proxy="")

        self.assertEqual(pid, "p-direct")
        body = captured["json_body"]
        self.assertEqual(body["proxy"], "direct")
        self.assertEqual(body["proxyInfo"]["proxyCategory"], "noproxy")
        self.assertEqual(self.client._proxy_pool_target, "")

    def test_create_profile_uses_proxy_pool_and_relay(self):
        captured = {}
        def fake_request(method, path, **kwargs):
            captured["json_body"] = kwargs.get("json_body")
            return {"code": 0, "data": {"dirId": "p-pool"}}

        from types import SimpleNamespace
        from unittest.mock import MagicMock
        fake_relay = SimpleNamespace(
            proxy_url="http://127.0.0.1:54321",
            close=MagicMock(),
            traffic_snapshot=MagicMock(return_value={"destinations": []}),
        )

        with patch.object(self.client, "request", side_effect=fake_request), \
             patch.multiple(cfg, ROXY_WORKSPACE_ID="ws-1", ROXY_CREATE_USE_PROXY_POOL=True), \
             patch("config.proxy.pick_proxy", return_value="socks5://prem.country.iprocket.io:9595"), \
             patch("core.proxy_chain.open_proxy_pool_proxy", return_value=("http://127.0.0.1:54321", fake_relay)):
            pid = self.client.create_profile()
        self.assertEqual(pid, "p-pool")
        body = captured["json_body"]
        self.assertEqual(body["proxy"], "http://127.0.0.1:54321")
        self.assertEqual(body["proxyInfo"]["host"], "127.0.0.1")
        self.assertEqual(body["proxyInfo"]["port"], "54321")
        self.assertEqual(self.client._proxy_pool_target, "socks5://prem.country.iprocket.io:9595")

        self.client.close_proxy_pool_relay()
        self.assertIsNone(self.client._proxy_pool_relay)

    def test_create_profile_without_proxy_or_pool_forces_direct_to_prevent_leak(self):
        captured = {}
        def fake_request(method, path, **kwargs):
            captured["json_body"] = kwargs.get("json_body")
            return {"code": 0, "data": {"dirId": "p-nodefault"}}

        with patch.object(self.client, "request", side_effect=fake_request), \
             patch.multiple(cfg, ROXY_WORKSPACE_ID="ws-1", ROXY_CREATE_USE_PROXY_POOL=False):
            pid = self.client.create_profile()

        self.assertEqual(pid, "p-nodefault")
        body = captured["json_body"]
        self.assertEqual(body["proxy"], "direct")
        self.assertEqual(body["proxyInfo"]["proxyCategory"], "noproxy")


if __name__ == "__main__":
    unittest.main()
