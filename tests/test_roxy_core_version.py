# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

from config import roxybrowser as _cfg
from core.roxybrowser_client import (
    RoxyBrowserClient,
    get_available_roxy_core_versions,
    _random_roxy_core_version,
)


class RoxyCoreVersionTests(unittest.TestCase):
    def test_blacklisted_version_155_is_always_filtered(self):
        with patch.object(_cfg, "ROXY_CORE_VERSION_POOL", "143,149,150,154,155,156"):
            versions = get_available_roxy_core_versions()
            self.assertNotIn("155", versions, "155 带有强制云端验签自杀暗桩，必须被硬编码拦截排除")
            self.assertNotIn("156", versions)
            for v in versions:
                self.assertIn(v, ["143", "149", "150", "151", "152", "153", "154"])

    def test_random_version_disabled_returns_none(self):
        with patch.object(_cfg, "ROXY_RANDOM_CORE_VERSION", False):
            self.assertIsNone(_random_roxy_core_version())

    def test_random_version_enabled_returns_version_from_pool(self):
        with patch.object(_cfg, "ROXY_RANDOM_CORE_VERSION", True), \
             patch.object(_cfg, "ROXY_CORE_VERSION_POOL", "150,152,154"):
            ver = _random_roxy_core_version()
            self.assertIn(ver, ["150", "152", "154"])

    def test_create_profile_injects_random_core_version(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50100", token="test-token")
        client.request = MagicMock(return_value={"code": 0, "data": {"dirId": "mock-pid-1"}})

        with patch.object(_cfg, "ROXY_RANDOM_CORE_VERSION", True), \
             patch.object(_cfg, "ROXY_CORE_VERSION_POOL", "152"):
            pid = client.create_profile()
            self.assertEqual(pid, "mock-pid-1")
            client.request.assert_called_once()
            _, kwargs = client.request.call_args
            body = kwargs.get("json_body") or {}
            self.assertEqual(body.get("coreVersion"), "152")

    def test_create_profile_respects_explicit_core_version(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50100", token="test-token")
        client.request = MagicMock(return_value={"code": 0, "data": {"dirId": "mock-pid-2"}})

        with patch.object(_cfg, "ROXY_RANDOM_CORE_VERSION", True), \
             patch.object(_cfg, "ROXY_CORE_VERSION_POOL", "152,154"):
            pid = client.create_profile(payload={"coreVersion": "150"})
            self.assertEqual(pid, "mock-pid-2")
            _, kwargs = client.request.call_args
            body = kwargs.get("json_body") or {}
            self.assertEqual(body.get("coreVersion"), "150")

    def test_create_profile_omits_core_version_when_disabled(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50100", token="test-token")
        client.request = MagicMock(return_value={"code": 0, "data": {"dirId": "mock-pid-3"}})

        with patch.object(_cfg, "ROXY_RANDOM_CORE_VERSION", False):
            client.create_profile()
            _, kwargs = client.request.call_args
            body = kwargs.get("json_body") or {}
            self.assertNotIn("coreVersion", body)


if __name__ == "__main__":
    unittest.main()
