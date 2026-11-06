import json
import unittest
from unittest.mock import MagicMock, patch

from core import db, registration_service as svc


class RegistrationRetryEmailTests(unittest.TestCase):
    def test_failed_registration_job_retry_inherits_email(self):
        source_job = {
            "id": 100,
            "status": "failed",
            "job_type": "registration",
            "email_source": "remail",
            "email": "wold-lites1g@icloud.com",
            "error": "ProxyError: connection timed out",
            "account_id": None,
        }

        with patch("core.db.get_job", return_value=source_job), \
             patch("core.db.get_successful_retry_for_job", return_value=None), \
             patch("core.db.get_account_by_email", return_value=None), \
             patch("core.db.create_retry_job") as mock_create_retry, \
             patch.object(svc, "get_executor"):

            mock_create_retry.return_value = ({"id": 101, "email": "wold-lites1g@icloud.com", "log_file": "/tmp/test.log"}, True)

            result = svc.retry_job(100)

            self.assertTrue(result["ok"])
            mock_create_retry.assert_called_once()
            # 确认 create_retry_job 时传入了上一轮领取的邮箱
            self.assertEqual(mock_create_retry.call_args.kwargs.get("email"), "wold-lites1g@icloud.com")

    def test_failed_registration_job_with_unusable_email_does_not_inherit_email(self):
        source_job = {
            "id": 102,
            "status": "failed",
            "job_type": "registration",
            "email_source": "remail",
            "email": "bad-user@icloud.com",
            "error": "RuntimeError: 该邮箱已注册 (already registered)",
            "account_id": None,
        }

        with patch("core.db.get_job", return_value=source_job), \
             patch("core.db.get_successful_retry_for_job", return_value=None), \
             patch("core.db.get_account_by_email", return_value=None), \
             patch("core.db.create_retry_job") as mock_create_retry, \
             patch.object(svc, "get_executor"):

            mock_create_retry.return_value = ({"id": 103, "email": None, "log_file": "/tmp/test.log"}, True)

            result = svc.retry_job(102)

            self.assertTrue(result["ok"])
            mock_create_retry.assert_called_once()
            # 确认如果错误是“已注册”，则不继承废弃邮箱，允许重新领号
            self.assertIsNone(mock_create_retry.call_args.kwargs.get("email"))

    def test_prepare_registration_args_prefers_inherited_email(self):
        email, name, birthday = svc._prepare_registration_args(inherited_email="inherited@example.com")
        self.assertEqual(email, "inherited@example.com")
        self.assertTrue(bool(name))
        self.assertTrue(bool(birthday))

    def test_failed_jobs_filter_excludes_already_registered_emails(self):
        from core.db import _sqlite_conn, closing
        with closing(_sqlite_conn()) as conn:
            # 插入两个失败任务，其中一个的邮箱在 accounts 中存在
            test_email_success = "succ_user@test.org"
            test_email_still_failed = "still_fail@test.org"
            conn.execute("INSERT OR REPLACE INTO accounts(id, email, status, archived, created_at, updated_at, payload) VALUES(?, ?, 'normal', 0, '2026-10-23', '2026-10-23', json(?))", (99998, test_email_success, json.dumps({"id": 99998, "email": test_email_success})))
            conn.execute("INSERT OR REPLACE INTO registration_jobs(id, email, status, archived, created_at, updated_at, payload) VALUES(?, ?, 'failed', 0, '2026-10-23', '2026-10-23', json(?))", (99998, test_email_success, json.dumps({"id": 99998, "email": test_email_success, "status": "failed"})))
            conn.execute("INSERT OR REPLACE INTO registration_jobs(id, email, status, archived, created_at, updated_at, payload) VALUES(?, ?, 'failed', 0, '2026-10-23', '2026-10-23', json(?))", (99999, test_email_still_failed, json.dumps({"id": 99999, "email": test_email_still_failed, "status": "failed"})))
            conn.commit()

        try:
            res = db.list_jobs_page(limit=100, status="failed")
            job_emails = [item.get("email") for item in res.get("items", [])]
            # 确认已经成功的邮箱不出现在失败列表中
            self.assertNotIn(test_email_success, job_emails)
            # 确认依然处于失败的邮箱正常出现在失败列表中
            self.assertIn(test_email_still_failed, job_emails)
        finally:
            with closing(_sqlite_conn()) as conn:
                conn.execute("DELETE FROM accounts WHERE id=99998")
                conn.execute("DELETE FROM registration_jobs WHERE id IN (99998, 99999)")
                conn.commit()
    def test_status_filter_only_shows_latest_job_for_each_email(self):
        from core.db import _sqlite_conn, closing
        multi_fail_email = "user_multi@test.org"
        retry_succ_email = "user_succ_after_fail@test.org"
        retry_running_email = "user_running_after_fail@test.org"
        with closing(_sqlite_conn()) as conn:
            # multi_fail: job 70001(failed) -> job 70002(failed)
            conn.execute("INSERT OR REPLACE INTO registration_jobs(id, email, status, archived, created_at, updated_at, payload) VALUES(?, ?, 'failed', 0, '2026-10-24T10:00:00', '2026-10-24T10:00:00', json(?))", (70001, multi_fail_email, json.dumps({"id": 70001, "email": multi_fail_email, "status": "failed"})))
            conn.execute("INSERT OR REPLACE INTO registration_jobs(id, email, status, archived, created_at, updated_at, payload) VALUES(?, ?, 'failed', 0, '2026-10-24T10:05:00', '2026-10-24T10:05:00', json(?))", (70002, multi_fail_email, json.dumps({"id": 70002, "email": multi_fail_email, "status": "failed"})))
            # retry_succ: job 70003(failed) -> job 70004(success)
            conn.execute("INSERT OR REPLACE INTO registration_jobs(id, email, status, archived, created_at, updated_at, payload) VALUES(?, ?, 'failed', 0, '2026-10-24T10:00:00', '2026-10-24T10:00:00', json(?))", (70003, retry_succ_email, json.dumps({"id": 70003, "email": retry_succ_email, "status": "failed"})))
            conn.execute("INSERT OR REPLACE INTO registration_jobs(id, email, status, archived, created_at, updated_at, payload) VALUES(?, ?, 'success', 0, '2026-10-24T10:05:00', '2026-10-24T10:05:00', json(?))", (70004, retry_succ_email, json.dumps({"id": 70004, "email": retry_succ_email, "status": "success"})))
            # retry_running: job 70005(failed) -> job 70006(running)
            conn.execute("INSERT OR REPLACE INTO registration_jobs(id, email, status, archived, created_at, updated_at, payload) VALUES(?, ?, 'failed', 0, '2026-10-24T10:00:00', '2026-10-24T10:00:00', json(?))", (70005, retry_running_email, json.dumps({"id": 70005, "email": retry_running_email, "status": "failed"})))
            conn.execute("INSERT OR REPLACE INTO registration_jobs(id, email, status, archived, created_at, updated_at, payload) VALUES(?, ?, 'running', 0, '2026-10-24T10:05:00', '2026-10-24T10:05:00', json(?))", (70006, retry_running_email, json.dumps({"id": 70006, "email": retry_running_email, "status": "running"})))
            conn.commit()

        try:
            # 1. 筛选 failed 状态
            res_failed = db.list_jobs_page(limit=100, status="failed")
            failed_ids = [item.get("id") for item in res_failed.get("items", [])]
            # 确认 multi_fail 只展示最新的 70002，不展示旧的 70001
            self.assertIn(70002, failed_ids)
            self.assertNotIn(70001, failed_ids)
            # 确认已重试成功或正在运行的任务不出现在失败列表中
            self.assertNotIn(70003, failed_ids)
            self.assertNotIn(70005, failed_ids)

            # 2. 筛选 success 状态
            res_success = db.list_jobs_page(limit=100, status="success")
            success_ids = [item.get("id") for item in res_success.get("items", [])]
            self.assertIn(70004, success_ids)

            # 3. 筛选 running 状态
            res_running = db.list_jobs_page(limit=100, status="running")
            running_ids = [item.get("id") for item in res_running.get("items", [])]
            self.assertIn(70006, running_ids)

            # 4. 全部状态（未按状态筛选）：全量流水历史均应存在
            res_all = db.list_jobs_page(limit=500)
            all_ids = [item.get("id") for item in res_all.get("items", [])]
            self.assertIn(70001, all_ids)
            self.assertIn(70002, all_ids)
            self.assertIn(70003, all_ids)
            self.assertIn(70004, all_ids)
            self.assertIn(70005, all_ids)
            self.assertIn(70006, all_ids)
        finally:
            with closing(_sqlite_conn()) as conn:
                conn.execute("DELETE FROM registration_jobs WHERE id IN (70001, 70002, 70003, 70004, 70005, 70006)")
                conn.commit()

    def test_failed_registration_job_retry_inherits_password(self):
        source_job = {
            "id": 200,
            "status": "failed",
            "job_type": "registration",
            "email_source": "remail",
            "email": "pass-user@icloud.com",
            "registration_password": "MySecretPassword123!",
            "error": "TimeoutError: waiting for about-you page",
            "account_id": None,
        }

        with patch("core.db.get_job", return_value=source_job), \
             patch("core.db.get_successful_retry_for_job", return_value=None), \
             patch("core.db.get_account_by_email", return_value=None), \
             patch("core.db.create_retry_job") as mock_create_retry, \
             patch.object(svc, "get_executor"):

            mock_create_retry.return_value = ({"id": 201, "email": "pass-user@icloud.com", "registration_password": "MySecretPassword123!", "log_file": "/tmp/test.log"}, True)
            result = svc.retry_job(200)

            self.assertTrue(result["ok"])
            mock_create_retry.assert_called_once()
            self.assertEqual(mock_create_retry.call_args.kwargs.get("email"), "pass-user@icloud.com")
            self.assertEqual(mock_create_retry.call_args.kwargs.get("registration_password"), "MySecretPassword123!")

    def test_db_create_retry_job_persists_inherited_password(self):
        from core.db import _sqlite_conn, closing
        with closing(_sqlite_conn()) as conn:
            conn.execute("INSERT OR REPLACE INTO registration_jobs(id, email, status, archived, created_at, updated_at, payload) VALUES(?, ?, 'failed', 0, '2026-10-24T10:00:00', '2026-10-24T10:00:00', json(?))",
                         (60001, "inherit_pwd@test.org", json.dumps({"id": 60001, "email": "inherit_pwd@test.org", "status": "failed", "registration_password": "PassTest987!"})))
            conn.commit()

        try:
            job, created = db.create_retry_job(60001, job_type="registration", email_source="remail", email="inherit_pwd@test.org")
            self.assertTrue(created)
            self.assertEqual(job.get("registration_password"), "PassTest987!")
            new_job_id = job.get("id")
            persisted = db.get_job(new_job_id)
            self.assertEqual(persisted.get("registration_password"), "PassTest987!")
        finally:
            with closing(_sqlite_conn()) as conn:
                conn.execute("DELETE FROM registration_jobs WHERE id IN (60001, ?)", (new_job_id if 'new_job_id' in locals() else 0,))
                conn.commit()
