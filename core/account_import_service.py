# -*- coding: utf-8 -*-
"""批量导入已有账号：显式凭据登录、保存账号并查询套餐。"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from core import account_liveness, db
from core.chatgpt_plan import (
    _mask_proxy,
    check_account_plan,
    open_plan_check_proxy,
    resolve_plan_check_route,
)

logger = logging.getLogger(__name__)

_MAX_ITEMS = 200
_MAX_TEXT_CHARS = 500_000
_MAX_LINE_CHARS = 4_096
_MAX_PASSWORD_CHARS = 1_024
_MAX_TOTP_CHARS = 256
_WORKERS = 3
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="account-import")
_LOCK = threading.RLock()
_TASKS: dict[str, dict] = {}
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_NETWORK_RETRY_HINTS = (
    "403", "408", "425", "429", "500", "502", "503", "504",
    "proxy", "socks", "timeout", "timed out", "connection", "closed",
    "reset", "ssl", "curl", "boring",
)


def parse_import_text(text: str, *, max_items: int = _MAX_ITEMS) -> list[dict]:
    """解析每行 ``邮箱----密码----2FA``，兼容三个或四个短横线分隔。"""
    text = str(text or "")
    if len(text) > _MAX_TEXT_CHARS:
        raise ValueError(f"导入内容过大，单次最多 {_MAX_TEXT_CHARS} 个字符")
    rows: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()
    raw_lines = str(text or "").splitlines()
    if not any(line.strip() for line in raw_lines):
        raise ValueError("请输入至少一行账号凭据")
    for line_no, raw in enumerate(raw_lines, 1):
        line = raw.strip()
        if not line:
            continue
        if len(line) > _MAX_LINE_CHARS:
            errors.append(f"第 {line_no} 行过长")
            continue
        parts = re.split(r"-{3,}", line, maxsplit=2)
        if len(parts) != 3:
            errors.append(f"第 {line_no} 行格式错误，应为：邮箱----密码----2FA密钥")
            continue
        email, password, secret = (part.strip() for part in parts)
        email_key = email.lower()
        if not _EMAIL_RE.fullmatch(email):
            errors.append(f"第 {line_no} 行邮箱格式错误")
            continue
        if not password:
            errors.append(f"第 {line_no} 行密码不能为空")
            continue
        if len(password) > _MAX_PASSWORD_CHARS:
            errors.append(f"第 {line_no} 行密码过长")
            continue
        if not secret:
            errors.append(f"第 {line_no} 行2FA密钥不能为空")
            continue
        if len(secret) > _MAX_TOTP_CHARS:
            errors.append(f"第 {line_no} 行2FA密钥过长")
            continue
        try:
            account_liveness._totp_code_from_secret(secret)
        except Exception:
            errors.append(f"第 {line_no} 行2FA密钥格式错误")
            continue
        if email_key in seen:
            errors.append(f"第 {line_no} 行邮箱重复")
            continue
        seen.add(email_key)
        rows.append({"email": email, "password": password, "totp_secret": secret})
        if len(rows) > max_items:
            raise ValueError(f"单次最多导入 {max_items} 个账号")
    if errors:
        raise ValueError("；".join(errors[:20]) + ("；其余错误已省略" if len(errors) > 20 else ""))
    if not rows:
        raise ValueError("没有可导入的账号")
    return rows


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _public_item(item: dict) -> dict:
    return {
        "email": item.get("email"),
        "status": item.get("status"),
        "account_id": item.get("account_id"),
        "plan_status": item.get("plan_status"),
        "error": item.get("error"),
    }


def _safe_error(exc: object, *secrets: object, limit: int = 300) -> str:
    """错误摘要中移除导入凭据和 Token，避免进入状态接口或日志。"""
    text = str(exc or "")[:limit]
    for secret in secrets:
        value = str(secret or "")
        if value:
            text = text.replace(value, "[已隐藏]")
    return text


def _configured_import_proxies() -> list[str | None]:
    """返回导入登录使用的代理候选，优先复用套餐/查活专用代理池。

    导入登录以前把 ``proxy=None`` 直接交给 BrowserSession。这样虽然会从
    注册代理池随机取一个出口，但 403/SSL 失败时不会切换到其他出口，批量导入
    很容易被单个坏节点整体拖失败。这里显式读取代理池，交给任务级重试逻辑轮换。
    """
    from config import proxy as proxy_cfg

    mode = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "auto") or "auto").strip().lower()
    if mode == "direct":
        return [""]
    dedicated = list(getattr(proxy_cfg, "PLAN_CHECK_PROXY", []) or [])
    fallback = list(getattr(proxy_cfg, "PROXY_POOL", []) or [])
    dedicated = list(dict.fromkeys(str(value).strip() for value in dedicated if str(value).strip()))
    fallback = list(dict.fromkeys(str(value).strip() for value in fallback if str(value).strip()))
    # 专用池是套餐查询/导入登录的明确地区策略。供应商可能把同一个固定
    # 代理重复填满配置，去重后只剩一个出口；这时仍然只使用该出口，不能
    # 因为它失败就跨到 PROXY_POOL（例如 IN 误切到 JP），否则账号登录画像
    # 和地区会被改变，也会把“专用代理不可用”掩盖成随机回退。
    if dedicated:
        values = dedicated
    else:
        values = fallback
    if values:
        return values
    if mode == "proxy":
        return []
    # None 表示让 BrowserSession 按其默认配置决定直连或代理；此时两个池都为空。
    return [None]


def _retryable_login_result(result: dict) -> bool:
    """判断导入登录结果是否属于更换出口后值得重试的网络错误。"""
    if result.get("ok") or result.get("status") in {"needs_email_otp", "deactivated"}:
        return False
    text = str(result.get("error") or "").lower()
    return any(hint in text for hint in _NETWORK_RETRY_HINTS)


def _import_proxy_attempts() -> list[str | None]:
    """按配置生成有限、无重复的导入登录出口顺序。"""
    from config import proxy as proxy_cfg

    candidates = _configured_import_proxies()
    if not candidates:
        return []
    try:
        max_attempts = max(1, min(8, int(getattr(proxy_cfg, "PLAN_CHECK_MAX_ATTEMPTS", 3) or 3)))
    except (TypeError, ValueError):
        max_attempts = 3
    if len(candidates) <= max_attempts:
        return candidates
    # 候选已经按专用池/通用池的地区边界生成；只在同一个候选池内随机
    # 选取尚未尝试的出口，避免跨地区切换。
    return random.sample(candidates, max_attempts)


def _effective_import_proxy(selected_proxy: str | None):
    """为一次导入登录建立实际请求代理及可选的上游中继。"""
    selected_proxy = None if selected_proxy is None else str(selected_proxy).strip()
    if not selected_proxy:
        return selected_proxy, None
    from config import proxy as proxy_cfg

    route = resolve_plan_check_route(explicit_proxy=selected_proxy)
    upstream = str(getattr(proxy_cfg, "PLAN_CHECK_UPSTREAM_PROXY", "") or "").strip()
    if upstream:
        route["upstream_proxy"] = upstream
    timeout = float(getattr(proxy_cfg, "PLAN_CHECK_TIMEOUT", 15.0) or 15.0)
    return open_plan_check_proxy(route, selected_proxy, timeout=timeout)


def _login_with_import_retry(row: dict) -> tuple[dict, str | None]:
    """登录导入账号；403/传输错误时更换尚未使用的代理出口。"""
    from config import proxy as proxy_cfg

    attempts = _import_proxy_attempts()
    if not attempts:
        return {
            "ok": False,
            "status": "failed",
            "error": "套餐/导入登录网络模式为 proxy，但未配置可用代理",
        }, None
    if len(attempts) == 1 and attempts[0]:
        logger.info(
            "[账号导入] 当前代理候选去重后仅剩 1 个出口；同一出口内部预检失败后不会跨地区回退",
        )
    try:
        retry_delay = max(0.0, min(30.0, float(getattr(proxy_cfg, "PLAN_CHECK_RETRY_DELAY", 2.0) or 0.0)))
    except (TypeError, ValueError):
        retry_delay = 2.0
    last_result: dict = {"ok": False, "status": "failed", "error": "导入登录未执行"}
    last_proxy: str | None = None
    for attempt, selected_proxy in enumerate(attempts, 1):
        effective_proxy = None
        relay = None
        last_proxy = selected_proxy
        try:
            effective_proxy, relay = _effective_import_proxy(selected_proxy)
            logger.info(
                "[账号导入] 登录网络出口尝试 %s/%s：%s",
                attempt, len(attempts), _mask_proxy(selected_proxy or "") or "direct",
            )
            result = account_liveness.login_account_with_credentials(
                row["email"],
                row["password"],
                row["totp_secret"],
                proxy=effective_proxy,
            )
            last_result = result if isinstance(result, dict) else {
                "ok": False,
                "status": "failed",
                "error": "登录返回结果格式错误",
            }
        except Exception as exc:
            last_result = {
                "ok": False,
                "status": "failed",
                "error": f"{type(exc).__name__}: {_safe_error(exc, row.get('password'), row.get('totp_secret'), limit=300)}",
            }
        finally:
            if relay is not None:
                try:
                    relay.close()
                except Exception:
                    logger.debug("[账号导入] 关闭代理中继失败", exc_info=True)

        if not _retryable_login_result(last_result) or attempt >= len(attempts):
            return last_result, last_proxy
        logger.warning(
            "[账号导入] 当前出口登录失败，将切换下一代理：%s",
            _safe_error(last_result.get("error"), row.get("password"), row.get("totp_secret"), limit=240),
        )
        if retry_delay > 0:
            time.sleep(retry_delay)
    return last_result, last_proxy


def _check_import_plan(access_token: str, selected_proxy: str | None) -> dict:
    """沿用登录成功的出口查询套餐，避免再次随机命中坏代理。"""
    effective_proxy = None
    relay = None
    try:
        effective_proxy, relay = _effective_import_proxy(selected_proxy)
        return check_account_plan(access_token, proxy=effective_proxy)
    finally:
        if relay is not None:
            try:
                relay.close()
            except Exception:
                logger.debug("[账号导入] 关闭套餐查询代理中继失败", exc_info=True)


def _run_one(task_id: str, index: int, row: dict) -> None:
    with _LOCK:
        task = _TASKS.get(task_id)
        if not task:
            return
        item = task["items"][index]
        item["status"] = "logging_in"
    email = row["email"]
    access_token = ""
    try:
        login, selected_proxy = _login_with_import_retry(row)
        network_traffic = login.get("network_traffic") if isinstance(login, dict) else None
        if not login.get("ok"):
            login_error = _safe_error(login.get("error"), row.get("password"), row.get("totp_secret"), login.get("access_token"), limit=300)
            with _LOCK:
                item.update({"status": login.get("status") or "failed", "error": login_error or "登录失败", "network_traffic": network_traffic})
            return
        access_token = str(login.get("access_token") or "").strip()
        session = login.get("session") if isinstance(login.get("session"), dict) else {}
        saved = db.upsert_imported_account(
            email=email,
            password=row["password"],
            totp_secret=row["totp_secret"],
            access_token=access_token,
            session=session,
            proxy_used=login.get("proxy_used"),
            fingerprint=login.get("fingerprint") if isinstance(login.get("fingerprint"), dict) else None,
            fingerprint_text=login.get("fingerprint_text"),
            network_traffic=network_traffic,
        )
        with _LOCK:
            item.update({"status": "checking_plan", "account_id": saved["id"], "network_traffic": network_traffic})
        try:
            plan_result = _check_import_plan(access_token, selected_proxy)
        except Exception as exc:
            plan_result = {"ok": False, "error": f"{type(exc).__name__}: {_safe_error(exc, access_token, limit=240)}"}
        db.update_account_plan_check(acc_id=saved["id"], result=plan_result)
        plan_error = _safe_error(plan_result.get("error"), access_token, limit=300)
        with _LOCK:
            item.update({
                "status": "success",
                "plan_status": "success" if plan_result.get("ok") else "failed",
                "error": None if plan_result.get("ok") else f"套餐查询失败：{plan_error or '未知错误'}",
            })
    except Exception as exc:
        safe_error = _safe_error(exc, row.get("password"), row.get("totp_secret"), access_token, limit=300)
        logger.warning("[账号导入] 处理失败：%s %s: %s", email, type(exc).__name__, safe_error)
        with _LOCK:
            item.update({"status": "failed", "error": f"{type(exc).__name__}: {safe_error}"})
    finally:
        with _LOCK:
            task = _TASKS.get(task_id)
            if task:
                task["completed"] = sum(1 for x in task["items"] if x["status"] in {"success", "failed", "needs_email_otp", "deactivated"})
                if task["completed"] >= task["total"]:
                    task["status"] = "completed"


def _run_task(task_id: str, rows: list[dict]) -> None:
    futures = [_EXECUTOR.submit(_run_one, task_id, idx, row) for idx, row in enumerate(rows)]
    # 当前函数本身运行在短生命周期调度线程中，等待子任务可让状态在异常时也正常收束。
    for future in futures:
        try:
            future.result()
        except Exception:
            logger.exception("[账号导入] 子任务异常: task_id=%s", task_id)


def _append_job_log(job: dict, message: str, *, level: str = "INFO") -> None:
    """向注册页任务日志追加一行，不记录密码、2FA 或 Token。"""
    log_file = str(job.get("log_file") or "").strip()
    if not log_file:
        return
    try:
        from pathlib import Path
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H:%M:%S")
        with Path(log_file).open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} [{level}] [账号导入] {message}\n")
    except Exception:
        logger.debug("[账号导入] 写入任务日志失败", exc_info=True)


def _run_import_job(job_id: int) -> None:
    """注册页账号导入任务 worker。"""
    from config import register as register_cfg
    from core import registration_service as registration_svc

    job = db.get_job(int(job_id))
    if not job or job.get("job_type") != "account_import":
        return
    job = db.claim_pending_job(int(job_id), started_at=_now())
    if not job:
        return
    registration_svc._activate_job(job_id)
    job_log_context = None
    access_token = ""
    row = {"email": str(job.get("email") or "").strip(), "password": "", "totp_secret": ""}
    try:
        _append_job_log(job, f"开始登录：{job.get('email') or '-'}")
        credentials = db.get_import_credentials(job_id)
        if not credentials:
            error = "导入凭据不存在，无法重试"
            db.update_job(job_id, status="failed", error=error, completed_at=_now())
            _append_job_log(job, error, level="ERROR")
            return
        row.update(password=credentials["password"], totp_secret=credentials["totp_secret"])
        from core.registration_service import _JobLogContext
        job_log_context = _JobLogContext(
            str(job.get("log_file") or ""),
            redact_values=[row["password"], row["totp_secret"]],
        )
        job_log_context.__enter__()
        registration_svc.check_stop_requested()
        login, selected_proxy = _login_with_import_retry(row)
        network_traffic = login.get("network_traffic") if isinstance(login, dict) else None
        if not login.get("ok"):
            error = _safe_error(
                login.get("error"), row["password"], row["totp_secret"],
                login.get("access_token"), limit=500,
            ) or "登录失败"
            current_job = db.get_job(job_id) or {}
            stopped = current_job.get("status") in {"stopping", "stopped", "cancelled"}
            db.update_job(
                job_id,
                status="stopped" if stopped else (login.get("status") or "failed"),
                email=row["email"],
                network_traffic=network_traffic,
                error="用户手动停止" if stopped else error,
                completed_at=_now(),
            )
            _append_job_log(job, "用户手动停止" if stopped else error, level="WARNING" if stopped else "ERROR")
            return

        current_job = db.get_job(job_id) or {}
        if current_job.get("status") in {"stopping", "stopped", "cancelled"}:
            db.update_job(job_id, status="stopped", network_traffic=network_traffic, error="用户手动停止", completed_at=_now())
            _append_job_log(job, "用户手动停止，未保存登录结果", level="WARNING")
            return

        access_token = str(login.get("access_token") or "").strip()
        job_log_context.add_redactions(access_token)
        saved = db.upsert_imported_account(
            email=row["email"],
            password=row["password"],
            totp_secret=row["totp_secret"],
            access_token=access_token,
            session=login.get("session") if isinstance(login.get("session"), dict) else {},
            proxy_used=login.get("proxy_used"),
            fingerprint=login.get("fingerprint") if isinstance(login.get("fingerprint"), dict) else None,
            fingerprint_text=login.get("fingerprint_text"),
            network_traffic=network_traffic,
        )
        db.update_job(job_id, status="success", email=row["email"], account_id=saved.get("id"), network_traffic=network_traffic, completed_at=_now())
        _append_job_log(job, f"登录成功，账号已保存：id={saved.get('id')}")

        # 导入成功和套餐查询解耦：账号先落库；是否自动查询由注册配置决定。
        try:
            auto_plan = bool(getattr(register_cfg, "AUTO_PLAN_CHECK_AFTER_REGISTER", False))
            if auto_plan:
                from core.plan_check_service import enqueue_account_plan_check
                queued = enqueue_account_plan_check(
                    account_id=int(saved["id"]),
                    email=row["email"],
                    access_token=access_token,
                    trigger="registration_auto",
                    proxy=selected_proxy,
                )
                if queued.get("accepted"):
                    _append_job_log(job, "已按配置提交套餐查询")
                else:
                    _append_job_log(job, f"套餐查询未入队：{queued.get('error') or '未知原因'}", level="WARNING")
            else:
                _append_job_log(job, "已跳过自动套餐查询（配置已关闭）")
        except Exception as exc:
            plan_error = _safe_error(exc, access_token, limit=300)
            _append_job_log(job, f"套餐查询入队异常（账号已保存）：{type(exc).__name__}: {plan_error}", level="WARNING")
            logger.warning("[账号导入] 套餐查询入队异常但账号已保存：job_id=%s %s: %s", job_id, type(exc).__name__, plan_error)
    except registration_svc.StopRequested:
        db.update_job(job_id, status="stopped", email=job.get("email"), error="用户手动停止", completed_at=_now())
        _append_job_log(job, "用户手动停止，未保存登录结果", level="WARNING")
    except Exception as exc:
        error = _safe_error(exc, row.get("password"), row.get("totp_secret"), access_token, limit=500)
        db.update_job(job_id, status="failed", email=row["email"], error=f"{type(exc).__name__}: {error}", completed_at=_now())
        _append_job_log(job, f"{type(exc).__name__}: {error}", level="ERROR")
        logger.error("[账号导入] 注册页任务失败: job_id=%s %s: %s", job_id, type(exc).__name__, error)
    finally:
        if job_log_context is not None:
            job_log_context.__exit__(None, None, None)
        registration_svc._deactivate_job(job_id)


def submit_import_jobs(text: str, *, workers: int | None = None) -> dict:
    """创建注册页账号导入任务，并立即返回任务列表。"""
    rows = parse_import_text(text)
    from core import registration_service as registration_svc

    try:
        worker_count = max(1, min(16, int(workers or registration_svc.get_executor_workers())))
    except (TypeError, ValueError):
        worker_count = registration_svc.get_executor_workers()
    task_id = uuid.uuid4().hex
    jobs: list[dict] = []
    with registration_svc._executor_lock:
        executor = registration_svc.get_executor(max_workers=worker_count)
        for row in rows:
            job = db.create_import_job(
                email=row["email"], password=row["password"], totp_secret=row["totp_secret"],
            )
            jobs.append(job)
            try:
                executor.submit(_run_import_job, int(job["id"]))
            except Exception as exc:
                db.update_job(job["id"], status="failed", error=f"队列提交失败：{type(exc).__name__}: {exc}"[:500], completed_at=_now())
    with _LOCK:
        _TASKS[task_id] = {
            "id": task_id,
            "job_ids": [int(job["id"]) for job in jobs],
            "created_at": _now(),
        }
    return {"task_id": task_id, "jobs": jobs}


def submit_import(text: str) -> str:
    rows = parse_import_text(text)
    task_id = uuid.uuid4().hex
    task = {
        "id": task_id,
        "status": "queued",
        "created_at": _now(),
        "total": len(rows),
        "completed": 0,
        "items": [{"email": row["email"], "status": "queued", "account_id": None, "plan_status": None, "error": None} for row in rows],
    }
    with _LOCK:
        _TASKS[task_id] = task
        task["status"] = "running"
    threading.Thread(target=_run_task, args=(task_id, rows), name=f"account-import-task-{task_id[:8]}", daemon=True).start()
    return task_id


def get_import_status(task_id: str) -> dict | None:
    with _LOCK:
        task = _TASKS.get(str(task_id or ""))
        if not task:
            return None
        if "job_ids" in task:
            rows = [db.get_job(job_id) for job_id in task["job_ids"]]
            rows = [row for row in rows if row]
            completed_states = {"success", "failed", "stopped", "cancelled", "needs_email_otp", "deactivated"}
            completed = sum(1 for row in rows if row.get("status") in completed_states)
            return {
                "task_id": task["id"], "status": "completed" if completed >= len(task["job_ids"]) else "running",
                "created_at": task["created_at"], "total": len(task["job_ids"]), "completed": completed,
                "success_count": sum(1 for row in rows if row.get("status") == "success"),
                "failed_count": sum(1 for row in rows if row.get("status") in {"failed", "stopped", "cancelled", "needs_email_otp", "deactivated"}),
                "items": [{"email": row.get("email"), "status": row.get("status"), "account_id": row.get("account_id"), "error": row.get("error_message")} for row in rows],
            }
        return {
            "task_id": task["id"],
            "status": task["status"],
            "created_at": task["created_at"],
            "total": task["total"],
            "completed": task["completed"],
            "success_count": sum(1 for item in task["items"] if item["status"] == "success"),
            "failed_count": sum(1 for item in task["items"] if item["status"] in {"failed", "needs_email_otp", "deactivated"}),
            "items": [_public_item(item) for item in task["items"]],
        }


def clear_finished_tasks(max_age_seconds: int = 3600) -> None:
    cutoff = datetime.now().timestamp() - max_age_seconds
    with _LOCK:
        for task_id, task in list(_TASKS.items()):
            if "job_ids" in task:
                states = [db.get_job(job_id) for job_id in task.get("job_ids") or []]
                if not states or not all(row and row.get("status") in {"success", "failed", "stopped", "cancelled", "needs_email_otp", "deactivated"} for row in states):
                    continue
            elif task.get("status") != "completed":
                continue
            try:
                stamp = datetime.fromisoformat(str(task.get("created_at"))).timestamp()
            except (TypeError, ValueError):
                stamp = cutoff
            if stamp < cutoff:
                _TASKS.pop(task_id, None)
