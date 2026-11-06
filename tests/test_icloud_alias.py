# -*- coding: utf-8 -*-
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import email as email_cfg, register as register_cfg
from core import db, email_provider, generic_api_mail_client, imap_mail_client, registration_service


class IcloudAliasTests(unittest.TestCase):
    def _storage_patches(self, root: Path) -> dict:
        return {
            "_ACCOUNTS_JSON": root / "accounts.json",
            "_OUTLOOK_JSON": root / "outlook.json",
            "_GENERIC_API_EMAIL_JSON": root / "generic.json",
            "_DOMAIN_EMAIL_JSON": root / "domain.json",
            "_JOBS_JSON": root / "jobs.json",
            "_LEGACY_ACCOUNTS_JSON": root / "legacy-accounts.json",
            "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json",
            "_LEGACY_JOBS_JSON": root / "legacy-jobs.json",
            "_LEGACY_SQLITE": root / "legacy.db",
            "_CODEX_DIR": root / "codex_accounts",
            "_CODEX_AGENT_DIR": root / "codex_agent_accounts",
            "_LEGACY_CODEX_EXPORT_STATE": root / "codex-export.json",
            "_SQLITE_READY": False,
            "_SQLITE_READY_PATH": None,
        }

    def test_icloud_helper_functions(self):
        # 1. is_icloud_email
        self.assertTrue(email_provider.is_icloud_email("test@icloud.com"))
        self.assertTrue(email_provider.is_icloud_email("TEST@ICLOUD.COM"))
        self.assertTrue(email_provider.is_icloud_email("test+alias@icloud.com"))
        self.assertFalse(email_provider.is_icloud_email("test@gmail.com"))
        self.assertFalse(email_provider.is_icloud_email("test@outlook.com"))
        self.assertFalse(email_provider.is_icloud_email(""))
        self.assertFalse(email_provider.is_icloud_email(None))

        # 2. parse_email_alias & has_email_alias
        base, alias, domain = email_provider.parse_email_alias("myuser@icloud.com")
        self.assertEqual((base, alias, domain), ("myuser", None, "icloud.com"))
        self.assertFalse(email_provider.has_email_alias("myuser@icloud.com"))

        base, alias, domain = email_provider.parse_email_alias("myuser+sub123@icloud.com")
        self.assertEqual((base, alias, domain), ("myuser", "sub123", "icloud.com"))
        self.assertTrue(email_provider.has_email_alias("myuser+sub123@icloud.com"))

        # 3. get_base_email
        self.assertEqual(email_provider.get_base_email("myuser@icloud.com"), "myuser@icloud.com")
        self.assertEqual(email_provider.get_base_email("myuser+sub123@icloud.com"), "myuser@icloud.com")
        self.assertEqual(email_provider.get_base_email("other+abc@gmail.com"), "other@gmail.com")

        # 4. generate_random_alias
        alias = email_provider.generate_random_alias(6)
        self.assertEqual(len(alias), 6)
        self.assertTrue(re.match(r"^[a-z0-9]{6}$", alias))

        # 5. generate_icloud_alias_email
        full_alias = email_provider.generate_icloud_alias_email("myuser@icloud.com", "abc123")
        self.assertEqual(full_alias, "myuser+abc123@icloud.com")

        random_alias_email = email_provider.generate_icloud_alias_email("myuser@icloud.com")
        self.assertTrue(re.match(r"^myuser\+[a-z0-9]{6}@icloud\.com$", random_alias_email))

    def test_generic_api_pool_two_round_claiming(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.import_generic_api_emails([
                    {"email": "alpha@icloud.com", "code_url": "https://api.mail/alpha"},
                    {"email": "beta@outlook.com", "code_url": "https://api.mail/beta"},
                ])

                with patch.object(email_cfg, "ENABLE_ICLOUD_EMAIL_ALIAS", True):
                    # 第 1 次领取：优先首轮未使用的邮箱，应该领取到 alpha@icloud.com 原邮箱
                    claim1 = db.claim_next_generic_api_email()
                    self.assertIsNotNone(claim1)
                    self.assertEqual(claim1["email"], "alpha@icloud.com")

                    # 第 2 次领取：首轮中的 beta@outlook.com
                    claim2 = db.claim_next_generic_api_email()
                    self.assertIsNotNone(claim2)
                    self.assertEqual(claim2["email"], "beta@outlook.com")

                    # 第 3 次领取：首轮已无可用邮箱，进入第二轮别名领取，领取 alpha 的别名邮箱
                    claim3 = db.claim_next_generic_api_email()
                    self.assertIsNotNone(claim3)
                    self.assertTrue(claim3["email"].startswith("alpha+"))
                    self.assertTrue(claim3["email"].endswith("@icloud.com"))
                    self.assertEqual(len(claim3["email"]), len("alpha+123456@icloud.com"))

                    # 取码上下文反查：使用生成的别名邮箱能通过 base_email 正确反查到 code_url
                    ctx = generic_api_mail_client.get_account_context(claim3["email"])
                    self.assertIsNotNone(ctx)
                    self.assertEqual(ctx.code_url, "https://api.mail/alpha")

                    # db.get_generic_api_email_by_email 也能查到
                    row = db.get_generic_api_email_by_email(claim3["email"])
                    self.assertIsNotNone(row)
                    self.assertEqual(row["code_url"], "https://api.mail/alpha")

                    # 第 4 次领取：所有邮箱均已用尽，返回 None
                    claim4 = db.claim_next_generic_api_email()
                    self.assertIsNone(claim4)

    def test_generic_api_unconsumed_release_keeps_stages(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.import_generic_api_emails([
                    {"email": "gamma@icloud.com", "code_url": "https://api.mail/gamma"},
                ])

                with patch.object(email_cfg, "ENABLE_ICLOUD_EMAIL_ALIAS", True):
                    # 第 1 轮领取 gamma@icloud.com
                    c1 = db.claim_next_generic_api_email()
                    self.assertEqual(c1["email"], "gamma@icloud.com")

                    # 模拟第 1 轮任务失败释放未消耗邮箱 -> 应退回第 1 轮
                    db.release_unconsumed_generic_api_email("gamma@icloud.com")

                    # 再次领取，应重新在第 1 轮以原邮箱领取
                    c1_retry = db.claim_next_generic_api_email()
                    self.assertEqual(c1_retry["email"], "gamma@icloud.com")

                    # 第 2 轮领取别名
                    c2 = db.claim_next_generic_api_email()
                    self.assertTrue(c2["email"].startswith("gamma+"))

                    # 模拟第 2 轮任务失败释放未消耗别名邮箱 -> 应退回第 2 轮可用
                    db.release_unconsumed_generic_api_email(c2["email"])

                    # 再次领取，仍为第 2 轮可用
                    c2_retry = db.claim_next_generic_api_email()
                    self.assertTrue(c2_retry["email"].startswith("gamma+"))

    def test_imap_pool_two_round_claiming(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.import_imap_emails([
                    {
                        "email": "imapbox@icloud.com",
                        "password": "app-password-123",
                        "server": "imap.mail.me.com",
                        "port": 993,
                    },
                ])

                with patch.object(email_cfg, "ENABLE_ICLOUD_EMAIL_ALIAS", True):
                    # 第 1 次：原邮箱
                    c1 = db.claim_next_imap_email()
                    self.assertIsNotNone(c1)
                    self.assertEqual(c1["email"], "imapbox@icloud.com")

                    # 第 2 次：生成别名
                    c2 = db.claim_next_imap_email()
                    self.assertIsNotNone(c2)
                    self.assertTrue(c2["email"].startswith("imapbox+"))
                    self.assertTrue(c2["email"].endswith("@icloud.com"))

                    # 取码上下文反查：使用生成的别名邮箱通过 base_email 反查到 IMAP 配置
                    ctx = imap_mail_client.get_account_context(c2["email"])
                    self.assertIsNotNone(ctx)
                    self.assertEqual(ctx.server, "imap.mail.me.com")
                    self.assertEqual(ctx.password, "app-password-123")

                    # 第 3 次：池耗尽
                    c3 = db.claim_next_imap_email()
                    self.assertIsNone(c3)

    def test_manual_mode_icloud_alias_selection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                with patch.object(email_cfg, "USE_EMAIL_SERVICE", False), \
                     patch.object(email_cfg, "ENABLE_ICLOUD_EMAIL_ALIAS", True), \
                     patch.object(register_cfg, "REGISTER_EMAIL", "solo@icloud.com"):
                    # 第一次：数据库中尚无 solo@icloud.com 账号 -> 使用原邮箱
                    email, name, birthday = registration_service._prepare_registration_args()
                    self.assertEqual(email, "solo@icloud.com")

                    # 模拟第一次注册成功入库
                    db.insert_account(email="solo@icloud.com", access_token="token1")

                    # 第二次：数据库中已有 solo@icloud.com -> 自动生成 6 位别名
                    email2, name2, birthday2 = registration_service._prepare_registration_args()
                    self.assertTrue(email2.startswith("solo+"))
                    self.assertTrue(email2.endswith("@icloud.com"))
                    self.assertEqual(len(email2), len("solo+123456@icloud.com"))

                    # 任务重试时，继承了原任务邮箱，不应再次随机别名
                    email_inherited, _, _ = registration_service._prepare_registration_args(inherited_email=email2)
                    self.assertEqual(email_inherited, email2)

    def test_switch_disabled_behavior(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.import_generic_api_emails([
                    {"email": "single@icloud.com", "code_url": "https://api.mail/single"},
                ])

                with patch.object(email_cfg, "ENABLE_ICLOUD_EMAIL_ALIAS", False):
                    # 开关关闭时，只领取一次并直接标记为 used
                    c1 = db.claim_next_generic_api_email()
                    self.assertEqual(c1["email"], "single@icloud.com")

                    # 第二次领取为 None
                    c2 = db.claim_next_generic_api_email()
                    self.assertIsNone(c2)


if __name__ == "__main__":
    unittest.main()
