# -*- coding: utf-8 -*-
"""Plus 代付后台服务与渠道客户端实现（支持 Foarge 众包代付等）。"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import uuid
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen

try:
    from curl_cffi import requests as curl_requests
except Exception:
    curl_requests = None

from config import payment as cfg
from core import db

logger = logging.getLogger(__name__)


def _runtime_setting(name: str, default=None):
    try:
        from config.env_loader import load_env
        load_env(override=True)
    except Exception:
        pass
    raw = os.getenv(name)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    return getattr(cfg, name, default)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(_runtime_setting(name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _float_setting(name: str, default: float) -> float:
    try:
        return float(_runtime_setting(name, default) or default)
    except (TypeError, ValueError):
        return default


_WORKERS = _int_setting("PAYMENT_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("PAYMENT_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="payment")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)

def is_auto_pay_enabled() -> bool:
    val = _runtime_setting("ENABLE_AUTO_PAY_AFTER_EXTRACT", False)
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("true", "1", "yes", "on")

class FoargeClient:
    """Foarge 发布方 API 客户端 (https://foarge.com/publisher/)。"""

    def __init__(
        self,
        base_url: str | None = None,
        auth_mode: str | None = None,
        pbk_key: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ):
        self.base_url = str(base_url or _runtime_setting("FOARGE_API_BASE", "https://foarge.com/api/publisher/v1")).strip().rstrip("/")
        self.auth_mode = str(auth_mode or _runtime_setting("FOARGE_AUTH_MODE", "pbk")).strip().lower()
        self.pbk_key = str(_runtime_setting("FOARGE_PBK_KEY", "") if pbk_key is None else pbk_key).strip()
        self.api_key = str(_runtime_setting("FOARGE_API_KEY", "") if api_key is None else api_key).strip()
        self.timeout = int(timeout or _int_setting("FOARGE_TIMEOUT", 30, 5, 300))

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        # 优先使用 PBK 卡密，支持 Bearer PBK- 或 X-Publisher-Cdk
        if self.auth_mode == "pbk" or (self.pbk_key and not self.api_key):
            if not self.pbk_key:
                raise ValueError("未配置 FOARGE_PBK_KEY (PBK预付卡密)")
            headers["X-Publisher-Cdk"] = self.pbk_key
            headers["Authorization"] = f"Bearer {self.pbk_key}"
        elif self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-Api-Key"] = self.api_key
        else:
            raise ValueError("未配置 Foarge 认证凭据 (请配置 FOARGE_PBK_KEY 或 FOARGE_API_KEY)")
        return headers

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.base_url}{path}" if path.startswith("/") else f"{self.base_url}/{path}"
        headers = self._headers()
        body_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None

        if curl_requests is not None:
            try:
                resp = curl_requests.request(
                    method=method,
                    url=url,
                    headers=headers,
                    data=body_bytes,
                    timeout=self.timeout,
                    impersonate="chrome120",
                )
                data = resp.json() if resp.content else {}
                if not (200 <= resp.status_code < 300):
                    err_msg = data.get("error") or data.get("message") or f"HTTP {resp.status_code}"
                    raise RuntimeError(f"Foarge API 错误 ({resp.status_code}): {err_msg}")
                return data
            except Exception as exc:
                if not isinstance(exc, RuntimeError):
                    logger.debug("[Foarge] curl_requests 请求失败，回退标准库: %s", exc)
                else:
                    raise

        # 标准库兜底
        req = Request(url=url, data=body_bytes, headers=headers, method=method)
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8")
                return json.loads(text) if text else {}
        except Exception as exc:
            raise RuntimeError(f"Foarge 请求失败: {exc}") from exc

    def query_cdk(self, cdk: str | None = None) -> dict:
        """查询 PBK 卡密额度与状态。"""
        old_key = self.pbk_key
        old_mode = self.auth_mode
        try:
            if cdk:
                self.pbk_key = str(cdk).strip()
                self.auth_mode = "pbk"
            return self._request("GET", "/cdk")
        finally:
            self.pbk_key = old_key
            self.auth_mode = old_mode

    def query_balance(self) -> dict:
        """查询钱包余额（仅钱包模式）。"""
        return self._request("GET", "/balance")

    def create_task(
        self,
        *,
        email: str,
        payment_method: str = "upi",
        markup_cny: float = 0.0,
        preferred_worker_ids: list[str] | str | None = None,
        fallback_sec: int | None = None,
        external_ref: str | None = None,
    ) -> dict:
        """创建代付任务 (排队/等待提交长链)。"""
        body: dict[str, Any] = {
            "account_email": str(email).strip(),
            "payment_method": str(payment_method).strip().lower(),
            "markup_cny": float(markup_cny or 0.0),
        }
        if preferred_worker_ids:
            if isinstance(preferred_worker_ids, str):
                p_ids = [w.strip() for w in preferred_worker_ids.replace(";", ",").split(",") if w.strip()]
            else:
                p_ids = list(preferred_worker_ids)
            if p_ids:
                body["preferred_worker_ids"] = p_ids
        if fallback_sec is not None:
            body["fallback_sec"] = int(fallback_sec)
        if external_ref:
            body["external_ref"] = str(external_ref).strip()

        return self._request("POST", "/tasks", body)

    def submit_checkout(
        self,
        task_id: str,
        *,
        access_token: str,
        pay_link: str,
    ) -> dict:
        """提交支付物 (Stripe 支付长链或 PIX 复制码 + ChatGPT access_token)。"""
        link = str(pay_link or "").strip()
        body = {
            "checkout_data": {
                "access_token": str(access_token).strip(),
                "pay_link": link,
                "upi_redirect_url": link,
                "brcode": link,
            }
        }
        return self._request("POST", f"/tasks/{task_id}/submit-checkout", body)

    def refresh_checkout(
        self,
        task_id: str,
        *,
        pay_link: str,
        access_token: str | None = None,
    ) -> dict:
        """支付码过期时提交新支付长链。"""
        link = str(pay_link or "").strip()
        cd: dict[str, Any] = {
            "pay_link": link,
            "upi_redirect_url": link,
            "brcode": link,
        }
        if access_token:
            cd["access_token"] = str(access_token).strip()
        return self._request("POST", f"/tasks/{task_id}/refresh-checkout", {"checkout_data": cd})

    def get_task(self, task_id: str) -> dict:
        """查询任务最新状态。"""
        return self._request("GET", f"/tasks/{task_id}")

    def cancel_task(self, task_id: str) -> dict:
        """取消任务。"""
        return self._request("POST", f"/tasks/{task_id}/cancel", {})


def _run_account_payment(
    *,
    account_id: int,
    email: str,
    access_token: str,
    pay_link: str | None = None,
    payment_method: str | None = None,
    channel: str = "foarge",
    trigger: str = "manual",
) -> dict:
    """单个账号代付处理完整流程（在后台线程池执行）。"""
    try:
        if not db.mark_account_payment_running(account_id):
            return {"ok": False, "error": "账号已删除或代付任务状态异常"}

        # 检查账号提取的支付链接
        acc = db.get_account(account_id) or {}
        link = str(pay_link or acc.get("extract_link_long_url") or acc.get("extract_link_copy_paste") or "").strip()
        if not link:
            raise ValueError("账号尚未成功提链，缺少有效的支付链接或复制码")
        pay_link = link

        expires_at_raw = acc.get("extract_link_expires_at")
        if expires_at_raw:
            try:
                exp_dt = None
                exp_str = str(expires_at_raw).strip()
                if exp_str.isdigit():
                    ts = int(exp_str)
                    if ts > 1e12:
                        ts = ts / 1000.0
                    exp_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                else:
                    if exp_str.endswith("Z"):
                        exp_str = exp_str[:-1] + "+00:00"
                    exp_dt = datetime.fromisoformat(exp_str)
                if exp_dt:
                    now_dt = datetime.now(timezone.utc) if exp_dt.tzinfo else datetime.now()
                    if now_dt >= exp_dt:
                        raise ValueError("提链支付链接已过期，请重新提链后再发起代付")
            except ValueError as ve:
                if "已过期" in str(ve):
                    raise
            except Exception:
                pass

        token = str(access_token or acc.get("access_token") or "").strip()
        if not token:
            raise ValueError("缺少账号 access_token，无法验证 Plus 订阅")

        # 优先使用显式传入的支付方式，其次提链返回，最后读取系统配置
        extracted_method = str(payment_method or acc.get("extract_link_payment_method") or acc.get("extract_link_type") or "").strip().lower()
        payment_method = extracted_method if extracted_method in {"upi", "pix", "blik"} else str(_runtime_setting("FOARGE_PAYMENT_METHOD", "upi")).strip().lower()

        logger.info("[代付] 开始处理: %s account_id=%s channel=%s method=%s", email, account_id, channel, payment_method)
        db.update_account_payment(account_id, {
            "status": "running",
            "channel": channel,
            "payment_method": payment_method,
            "message": f"正在向 {channel} 创建代付订单...",
        })

        if channel == "foarge":
            client = FoargeClient()
            markup = _float_setting("FOARGE_MARKUP_CNY", 0.0)
            preferred = _runtime_setting("FOARGE_PREFERRED_WORKER_IDS", "")
            fallback = _int_setting("FOARGE_FALLBACK_SEC", 45, 0, 300)

            # 1. 创建任务
            created = client.create_task(
                email=email,
                payment_method=payment_method,
                markup_cny=markup,
                preferred_worker_ids=preferred,
                fallback_sec=fallback,
                external_ref=f"acc_{account_id}_{uuid.uuid4().hex[:8]}",
            )
            task_id = str(created.get("task_id") or created.get("id") or (created.get("task") or {}).get("id") or "")
            if not task_id:
                raise RuntimeError(f"Foarge 任务创建失败: {created}")

            db.update_account_payment(account_id, {
                "status": "running",
                "task_id": task_id,
                "message": f"已创建订单 #{task_id}，正在提交支付码...",
            })

            # 2. 提交支付链接与 access_token
            submitted = client.submit_checkout(
                task_id=task_id,
                access_token=token,
                pay_link=pay_link,
            )
            logger.info("[代付] 已提交支付码至 Foarge: task_id=%s, result=%s", task_id, submitted)
            db.update_account_payment(account_id, {
                "status": "running",
                "task_id": task_id,
                "message": f"已交码进大厅 (#{task_id})，等待工人代付...",
                "result": {"task_id": task_id, "create_response": created, "submit_response": submitted},
            })

            # 3. 轮询等待工人完成付款
            poll_interval = _int_setting("PAYMENT_POLL_INTERVAL", 8, 3, 60)
            max_wait = _int_setting("PAYMENT_MAX_WAIT_SECONDS", 600, 60, 3600)
            end_time = time.time() + max_wait
            final_task = None

            while time.time() < end_time:
                time.sleep(poll_interval)
                res = client.get_task(task_id)
                task_info = res.get("task") if isinstance(res.get("task"), dict) else res
                st = str(task_info.get("status") or "").lower()
                final_task = task_info

                if st == "completed":
                    logger.info("[代付] 成功完成: %s task_id=%s", email, task_id)
                    final = {
                        "ok": True,
                        "status": "success",
                        "channel": channel,
                        "task_id": task_id,
                        "payment_method": payment_method,
                        "message": "代付成功，Plus 订阅已核销激活",
                        "result": task_info,
                    }
                    db.update_account_payment(account_id, final)
                    try:
                        from core import plan_check_service
                        plan_check_service.enqueue_account_plan_check(
                            account_id=account_id,
                            email=email,
                            access_token=token,
                            trigger="payment_success_auto",
                        )
                        logger.info("[代付] 代付成功，已自动触发查套餐入队: %s", email)
                    except Exception as plan_exc:
                        logger.warning("[代付] 自动触发查套餐异常: %s", plan_exc)
                    return final

                if st in {"cancelled", "failed", "subscription_failed"}:
                    reason = task_info.get("error") or task_info.get("message") or f"任务状态已终止: {st}"
                    raise RuntimeError(f"Foarge 代付失败 ({st}): {reason}")

                if st == "assigned":
                    worker_name = task_info.get("worker_name") or "专员"
                    db.update_account_payment(account_id, {
                        "status": "running",
                        "message": f"工人 {worker_name} 正在支付中...",
                    })
                elif st in {"pending", "payment_received"}:
                    db.update_account_payment(account_id, {
                        "status": "running",
                        "message": f"状态: {st}，等待完成...",
                    })

            raise TimeoutError(f"代付等待超时 ({max_wait}秒)，订单状态: {(final_task or {}).get('status')}")

        else:
            raise ValueError(f"暂不支持的支付渠道: {channel}")

    except Exception as exc:
        err_msg = str(exc)[:300].strip()
        import re
        clean_err = re.sub(r"^[A-Za-z_]+Error:\s*", "", err_msg).strip()
        clean_err = re.sub(r"^(?:Foarge\s+)?(?:代付|支付)失败(?:\s*\([^)]*\))?[:：]\s*", "", clean_err).strip()
        if not clean_err:
            clean_err = err_msg or "未知异常"
        pay_err = f"支付失败：{clean_err}"
        logger.error("[支付] 失败: %s account_id=%s error=%s", email, account_id, pay_err)
        result = {
            "ok": False,
            "status": "failed",
            "channel": channel,
            "error": pay_err,
            "message": pay_err,
        }
        db.update_account_payment(account_id, result)
        return result
    finally:
        try:
            _QUEUE_SLOTS.release()
        except ValueError:
            pass


def enqueue_account_payment(
    *,
    account_id: int,
    email: str,
    access_token: str | None = None,
    pay_link: str | None = None,
    payment_method: str | None = None,
    channel: str | None = None,
    trigger: str = "manual",
) -> dict:
    """将账号代付任务加入后台执行队列。"""
    acc = db.get_account(account_id)
    if acc and acc.get("status") == "sold":
        return {"accepted": False, "busy": False, "error": "账号已出售，不可操作"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "代付任务队列已满，请稍后重试"}

    ch = str(channel or _runtime_setting("PAYMENT_CHANNEL", "foarge")).strip().lower()
    if not db.claim_account_payment(account_id, trigger=trigger, channel=ch):
        _QUEUE_SLOTS.release()
        acc = db.get_account(account_id)
        if acc and acc.get("status") == "sold":
            return {"accepted": False, "busy": False, "error": "账号已出售，不可操作"}
        return {"accepted": False, "busy": True, "error": "该账号正在代付中，请勿重复发起"}

    future = _EXECUTOR.submit(
        _run_account_payment,
        account_id=account_id,
        email=email,
        access_token=str(access_token or "").strip(),
        pay_link=str(pay_link or "").strip() or None,
        payment_method=str(payment_method or "").strip() or None,
        channel=ch,
        trigger=trigger,
    )
    return {"accepted": True, "busy": False, "future": future}
