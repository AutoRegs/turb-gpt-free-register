# -*- coding: utf-8 -*-
"""账号浏览器登录：导入与查活共享独占环境、凭据验证和会话读取。"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlparse

from core import roxy_registration as pages
from core.account_liveness import EmailOtpRequiredError, _now
from core.email_provider import wait_for_otp
from core.openai_auth import AccountUnusableError, detect_account_unusable_text

logger = logging.getLogger(__name__)

_DRIVERS = {
    "roxy": "roxy", "roxybrowser": "roxy", "fingerprint": "roxy", "browser": "roxy",
    "cloak": "cloak", "cloakbrowser": "cloak",
    "browser_use": "browser_use", "browseruse": "browser_use", "browser-use": "browser_use", "bu": "browser_use",
    "skyvern": "skyvern", "sv": "skyvern",
}


def _cleanup(action) -> None:
    """清理故障单独记录，避免覆盖已经成功取得的登录结果。"""
    try:
        action()
    except Exception as exc:
        logger.warning("[账号登录] 清理浏览器资源失败：%s", type(exc).__name__)


def _setup_browser_data_saver_and_cache(
    driver: Any,
    driver_mode: str,
    opened: Any,
    *,
    log_prefix: str = "[账号登录]",
) -> tuple[Any | None, Any | None, Any | None]:
    """为导入/查活浏览器安装省流量规则、本地静态资源缓存与流量统计器。

    Roxy: 挂载 RoxyLocalAssetCache + SeleniumTrafficTracker + BrowserDataSaver.install_selenium
    Cloak: 挂载 PlaywrightTrafficTracker + BrowserDataSaver.install_playwright
    """
    asset_cache = None
    data_saver = None
    traffic_tracker = None
    label = "Roxy" if driver_mode == "roxy" else "Cloak" if driver_mode == "cloak" else driver_mode.capitalize()
    if driver_mode == "roxy":
        debugger_address = getattr(opened, "debugger_address", None)
        if isinstance(debugger_address, str) and debugger_address.strip() and not debugger_address.startswith("<"):
            try:
                from core.roxy_asset_cache import RoxyLocalAssetCache

                asset_cache = RoxyLocalAssetCache(debugger_address, label=label).start()
            except Exception as exc:
                logger.warning("%s 初始化本地静态资源缓存失败，继续联网加载：%s: %s", log_prefix, type(exc).__name__, str(exc)[:180])
        try:
            from core.browser_traffic import SeleniumTrafficTracker

            traffic_tracker = SeleniumTrafficTracker(driver, label=label)
        except Exception as exc:
            logger.debug("%s 初始化浏览器流量统计失败，继续：%s: %s", log_prefix, type(exc).__name__, str(exc)[:180])
        try:
            from core.browser_data_saver import BrowserDataSaver

            data_saver = BrowserDataSaver(label=label)
            if traffic_tracker is not None:
                traffic_tracker.attach_data_saver(data_saver)
                traffic_tracker.attach_local_asset_cache(asset_cache)
            data_saver.install_selenium(driver)
        except Exception as exc:
            logger.warning("%s 初始化省流量拦截器失败，继续常规加载：%s: %s", log_prefix, type(exc).__name__, str(exc)[:180])
    elif driver_mode == "cloak":
        context = getattr(driver, "context", None)
        if context is not None:
            try:
                from core.browser_traffic import PlaywrightTrafficTracker

                traffic_tracker = PlaywrightTrafficTracker(context, label=label)
            except Exception as exc:
                logger.debug("%s 初始化浏览器流量统计失败，继续：%s: %s", log_prefix, type(exc).__name__, str(exc)[:180])
            try:
                from core.browser_data_saver import BrowserDataSaver

                data_saver = BrowserDataSaver(label=label)
                if traffic_tracker is not None:
                    traffic_tracker.attach_data_saver(data_saver)
                data_saver.install_playwright(context)
            except Exception as exc:
                logger.warning("%s 初始化省流量拦截器失败，继续常规加载：%s: %s", log_prefix, type(exc).__name__, str(exc)[:180])

    if data_saver is not None:
        try:
            driver._data_saver = data_saver
        except Exception:
            pass
    if asset_cache is not None:
        try:
            driver._asset_cache = asset_cache
        except Exception:
            pass
    if traffic_tracker is not None:
        try:
            driver._traffic_tracker = traffic_tracker
        except Exception:
            pass

    return data_saver, asset_cache, traffic_tracker


def _collect_and_teardown_traffic(
    driver: Any,
    client: Any | None,
    driver_mode: str,
    *,
    data_saver: Any | None = None,
    asset_cache: Any | None = None,
    traffic_tracker: Any | None = None,
    label: str = "Roxy",
) -> dict | None:
    """结算本轮浏览器的流量统计，并生成与注册对齐的 network_traffic 对象。"""
    asset_cache_snapshot = None
    network_traffic = None
    if asset_cache is None and driver is not None:
        asset_cache = getattr(driver, "_asset_cache", None)
    if data_saver is None and driver is not None:
        data_saver = getattr(driver, "_data_saver", None)
    if traffic_tracker is None and driver is not None:
        traffic_tracker = getattr(driver, "_traffic_tracker", None)

    if asset_cache is not None:
        try:
            asset_cache_snapshot = asset_cache.stop()
        except Exception:
            pass

    if traffic_tracker is not None:
        try:
            traffic_tracker.checkpoint()
        except Exception:
            pass
        try:
            network_traffic = traffic_tracker.stop()
        except Exception:
            pass

    if asset_cache_snapshot is not None:
        if not isinstance(network_traffic, dict):
            network_traffic = {}
        network_traffic["local_asset_cache"] = asset_cache_snapshot

    transport_snapshot = None
    if driver_mode == "roxy" and client is not None:
        try:
            transport_snapshot = client.proxy_transport_snapshot()
        except Exception:
            transport_snapshot = None
    elif driver_mode == "cloak" and driver is not None:
        try:
            transport_snapshot = getattr(driver, "proxy_transport_snapshot", lambda: None)()
        except Exception:
            transport_snapshot = None

    if network_traffic is not None or transport_snapshot is not None:
        from core.browser_traffic import merge_proxy_transport_traffic

        try:
            network_traffic = merge_proxy_transport_traffic(
                network_traffic, transport_snapshot, label=label,
            )
        except Exception as exc:
            logger.debug("[%s] 合并代理传输流量异常: %s", label, exc)

    if data_saver is not None:
        try:
            data_saver.stop()
        except Exception:
            pass

    return network_traffic

def _apply_post_login_data_saver(
    driver: Any,
    data_saver: Any | None = None,
    *,
    log_prefix: str = "[账号登录]",
    mode: str = "roxy",
) -> None:
    """登录成功取得 Token 后，收紧拦截规则并断开后续 SPA 页面长轮询。"""
    if data_saver is None:
        data_saver = getattr(driver, "_data_saver", None)
    if data_saver is not None:
        try:
            data_saver.enable_post_auth_deep_mode(driver)
            logger.info("%s 已启用登录后深度省流量拦截", log_prefix)
        except Exception as exc:
            logger.debug("%s 开启登录后深度省流量拦截失败: %s", log_prefix, exc)
    from config import roxybrowser as roxy_cfg
    from config import cloakbrowser as cloak_cfg

    keep_open = (
        bool(getattr(roxy_cfg, "ROXY_KEEP_BROWSER_OPEN", False))
        if mode == "roxy"
        else bool(getattr(cloak_cfg, "CLOAK_KEEP_BROWSER_OPEN", False))
    )
    if not keep_open:
        try:
            driver.get("about:blank")
            logger.info("%s 已切换 about:blank，停止登录后的页面后台流量", log_prefix)
        except Exception as exc:
            logger.debug("%s 切换空白页失败，保留深度拦截: %s", log_prefix, exc)


@contextmanager
def _browser_environment(driver_mode: str, proxy: str | None, log_prefix: str = "[账号导入]", state: dict | None = None):
    """每次调用创建新环境；不复用任何已配置的固定账号 Profile。"""
    if driver_mode == "roxy":
        from config import roxybrowser as cfg
        from core.roxybrowser_client import RoxyBrowserClient

        client = RoxyBrowserClient()
        driver = opened = None
        data_saver = asset_cache = traffic_tracker = None
        retained = False
        try:
            opened = client.open_profile(fresh=True, proxy=proxy)
            driver = pages._build_driver(opened)
            driver._registration_log_prefix = f"{log_prefix}[Roxy]"
            driver.set_page_load_timeout(int(cfg.ROXY_SELENIUM_TIMEOUT))
            data_saver, asset_cache, traffic_tracker = _setup_browser_data_saver_and_cache(
                driver, "roxy", opened, log_prefix=f"{log_prefix}[Roxy]",
            )
            yield driver, opened.profile_id, (opened.raw or {}).get("proxy_pool_target", proxy)
            retained = True
        finally:
            traffic = _collect_and_teardown_traffic(
                driver, client, "roxy",
                data_saver=data_saver, asset_cache=asset_cache, traffic_tracker=traffic_tracker,
                label="Roxy",
            )
            if state is not None and isinstance(state, dict):
                state["network_traffic"] = traffic
            if driver is not None and not cfg.ROXY_KEEP_BROWSER_OPEN:
                _cleanup(driver.quit)
            _cleanup(lambda: client.cleanup_profile(opened, retain=retained))
        return

    if driver_mode == "cloak":
        from config import cloakbrowser as cfg
        from core.cloakbrowser_driver import build_cloak_driver

        driver, opened = build_cloak_driver(proxy=proxy, fresh=True)
        driver._registration_log_prefix = f"{log_prefix}[Cloak]"
        data_saver, asset_cache, traffic_tracker = _setup_browser_data_saver_and_cache(
            driver, "cloak", opened, log_prefix=f"{log_prefix}[Cloak]",
        )
        try:
            yield driver, opened.profile_id, (opened.raw or {}).get("proxy_pool_target", proxy)
        finally:
            traffic = _collect_and_teardown_traffic(
                driver, None, "cloak",
                data_saver=data_saver, asset_cache=asset_cache, traffic_tracker=traffic_tracker,
                label="Cloak",
            )
            if state is not None and isinstance(state, dict):
                state["network_traffic"] = traffic
            if not cfg.CLOAK_KEEP_BROWSER_OPEN:
                _cleanup(driver.quit)
        return

    from playwright.sync_api import sync_playwright
    from core.cloakbrowser_driver import CloakSeleniumDriver

    if driver_mode == "skyvern":
        from core.skyvern_client import SkyvernClient
        client = SkyvernClient()
    else:
        from core.browser_use_client import BrowserUseClient
        client = BrowserUseClient()
    opened = client.open_session(fresh=True)
    browser = None
    try:
        with sync_playwright() as playwright:
            try:
                kwargs = {"headers": client.cdp_headers()} if driver_mode == "skyvern" else {}
                browser = playwright.chromium.connect_over_cdp(opened.connect_url, **kwargs)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.pages[0] if context.pages else context.new_page()
                driver = CloakSeleniumDriver(browser=browser, context=context, page=page)
                driver._registration_log_prefix = f"[账号导入][{driver_mode}]"
                logger.info("[账号登录] 云端浏览器使用供应商配置的网络出口")
                yield driver, str(getattr(opened, "session_id", "") or "新建云端会话"), None
            finally:
                if browser is not None:
                    _cleanup(browser.close)
    finally:
        session_id = getattr(opened, "session_id", "")
        if driver_mode == "skyvern" and session_id:
            _cleanup(lambda: client.close_browser_session(session_id))


@contextmanager
def _roxy_account_environment(account_id: int, email: str, proxy: str | None, state: dict, log_prefix: str = "[查活]"):
    """查活优先使用账号原环境；新建替代环境在登录前绑定，结束只关窗口。"""
    from config import roxybrowser as cfg
    from core import db
    from core.roxybrowser_client import RoxyBrowserClient, RoxyProfileMissingError, RoxyProfileReuseUnavailableError

    profile_id = db.get_account_roxy_profile_id(account_id, email)
    saved = db.get_account(account_id) or {}
    proxy_key = hashlib.sha256(proxy.encode("utf-8")).hexdigest() if proxy is not None else None
    fingerprint = saved.get("live_check_fingerprint") or {}
    # 同一账号已设置过相同出口时直接打开环境。部分旧版接口不返回 proxyInfo，
    # 因此保留出口摘要；兼容此前成功查活已经保存的环境/出口记录。
    same_saved_proxy = bool(
        profile_id and saved.get("roxy_profile_id") == profile_id and proxy_key is not None
        and (saved.get("roxy_profile_proxy_key") == proxy_key if saved.get("roxy_profile_proxy_key") else (
            isinstance(fingerprint, dict) and fingerprint.get("driver") == "roxy"
            and fingerprint.get("profile_id") == profile_id and saved.get("live_check_proxy_used") == proxy
        ))
    )
    client = RoxyBrowserClient()
    driver = opened = None
    data_saver = asset_cache = traffic_tracker = None
    retained = False
    try:
        if profile_id:
            logger.info("%s 优先复用当前账号的 Roxy 环境：%s", log_prefix, profile_id)
            try:
                if same_saved_proxy:
                    logger.info("%s 账号原环境已使用当前查活出口，直接复用，无需更新代理", log_prefix)
                opened = client.open_profile(profile_id, reuse=True, proxy=None if same_saved_proxy else proxy)
                state["reused"] = True
            except RoxyProfileMissingError:
                logger.info("%s 原 Roxy 环境已删除，创建新的账号独占环境", log_prefix)
            except RoxyProfileReuseUnavailableError:
                logger.info("%s 当前 Roxy 版本无法复用旧环境，保留原环境并新建账号独占环境", log_prefix)
        if opened is None:
            opened = client.open_profile(fresh=True, proxy=proxy)
            state["reused"] = False
        retained = db.bind_account_roxy_profile(account_id, email, opened.profile_id, proxy_key=proxy_key)
        if not retained:
            raise RuntimeError("保存 Roxy 环境绑定失败：账号已删除、邮箱已变更或环境已属于其他账号")
        driver = pages._build_driver(opened)
        driver._registration_log_prefix = f"{log_prefix}[Roxy]"
        driver.set_page_load_timeout(int(cfg.ROXY_SELENIUM_TIMEOUT))
        data_saver, asset_cache, traffic_tracker = _setup_browser_data_saver_and_cache(
            driver, "roxy", opened, log_prefix=f"{log_prefix}[Roxy]",
        )
        yield driver, opened.profile_id, (opened.raw or {}).get("proxy_pool_target", proxy)
    finally:
        traffic = _collect_and_teardown_traffic(
            driver, client, "roxy",
            data_saver=data_saver, asset_cache=asset_cache, traffic_tracker=traffic_tracker,
            label="Roxy",
        )
        if state is not None and isinstance(state, dict):
            state["network_traffic"] = traffic
        if driver is not None and not cfg.ROXY_KEEP_BROWSER_OPEN:
            _cleanup(driver.quit)
        _cleanup(lambda: client.cleanup_profile(opened, retain=retained))


def _choose_login_password(driver) -> bool:
    """邮箱 OTP 页面只选择已有账号的密码登录，禁止跳到创建密码页。"""
    target = driver.execute_script(r"""
    const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    const button = [...document.querySelectorAll('a,button,[role="button"]')].find(el => {
      if (!visible(el) || el.disabled) return false;
      const href = el.getAttribute('href') || '';
      if (/create-account|signup/i.test(href)) return false;
      const text = (el.innerText || el.textContent || '').replace(/\s+/g, '').toLowerCase();
      return /\/log-in\/password/.test(href)
        || /continuewithpassword|usepassword|密码.*登录|密碼.*登入|密码.*继续|パスワード.*続行/.test(text);
    });
    return button;
    """)
    if target is None:
        return False
    pages._human_click(driver, target, label="import_use_password")
    return True


def _submit_login_email_otp(driver, email: str, *, after_ts: float, email_source: str | None, log_prefix: str) -> None:
    """查活使用账号保存的邮箱来源取码；仅明确无效时重发，最多三次。"""
    for attempt in range(1, 4):
        pages._check_manual_stop()
        logger.info("%s 等待登录邮箱验证码（第 %s/3 次），来源=%s", log_prefix, attempt, email_source or "账号/系统配置")
        code = wait_for_otp(email, after_ts=after_ts, email_source=email_source)
        pages._clear_otp_inputs(driver)
        pages._type_otp(driver, code)
        pages._click_continue(driver)
        if pages._wait_after_email_otp_submit(driver, timeout=15) != "invalid":
            return
        if attempt < 3:
            pages._click_resend_email_otp(driver)
            after_ts = time.time()
    raise RuntimeError("邮箱验证码连续无效/过期，已达到最大重试次数")

def _is_session_expired(expires_raw: str | None, access_token: str | None) -> bool:
    """本地 0 流量初筛：判断会话/accessToken 是否已经过期。

    1. 优先检查 Next.js session 中的 expires 字符串（如 2026-10-30T...）；
    2. 其次解析 accessToken (JWT) Payload 中的 exp 时间戳；
    3. 预留 60 秒安全缓冲，若已过期则返回 True。
    """
    now = time.time()
    if expires_raw:
        raw_str = str(expires_raw).strip()
        try:
            clean_iso = raw_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_iso)
            exp_ts = dt.timestamp()
            if exp_ts <= now + 60:
                return True
        except Exception:
            pass

    if access_token:
        try:
            parts = str(access_token).strip().split(".")
            if len(parts) >= 2:
                seg = parts[1]
                padded = seg + "=" * ((4 - len(seg) % 4) % 4)
                payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
                exp_ts = float(payload.get("exp") or 0)
                if exp_ts > 0 and exp_ts <= now + 60:
                    return True
        except Exception:
            pass

    return False

def _verify_chatgpt_session(driver, access_token: str, *, expected_email: str | None = None) -> tuple[bool, str]:
    """在浏览器当前页面校验 accessToken 是否真实有效。

    返回 (is_valid, reason)。
    """
    token = str(access_token or "").strip()
    if not token:
        return False, "accessToken 为空"
    script = r"""
    const done = arguments[arguments.length - 1];
    const token = String(arguments[0] || '').trim();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);
    fetch('/backend-api/me', {
      method: 'GET',
      headers: {
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/json'
      },
      signal: controller.signal
    })
    .then(async r => {
      clearTimeout(timer);
      let data = null;
      try { data = await r.json(); } catch(e) {}
      done({ok: r.ok, status: r.status, data: data});
    })
    .catch(e => {
      clearTimeout(timer);
      done({ok: false, status: 0, error: String(e)});
    });
    """
    try:
        result = driver.execute_async_script(script, token)
    except Exception as exc:
        return False, f"执行校验脚本异常: {type(exc).__name__}: {exc}"
    if not isinstance(result, dict):
        return False, f"校验脚本返回格式异常: {result}"
    if result.get("ok"):
        user_data = result.get("data") or {}
        actual_email = str(user_data.get("email") or "").strip()
        if expected_email and actual_email and actual_email.lower() != expected_email.strip().lower():
            return False, f"会话用户邮箱不匹配({actual_email} != {expected_email})"
        return True, "有效"
    status = result.get("status")
    error_detail = result.get("error")
    data = result.get("data")
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            msg = err.get("message")
            error_detail = f"{code}: {msg}" if code and msg else (code or msg or error_detail)
    return False, f"HTTP {status} ({error_detail or 'token 无效'})"


def _clear_browser_session_state(driver) -> None:
    """清除浏览器中的旧会话 Cookie 和本地存储，确保后续走干净的登录流程。"""
    try:
        driver.delete_all_cookies()
    except Exception as exc:
        logger.debug("[账号登录] 清除 cookies 失败: %s", exc)
    try:
        driver.execute_script("try { window.localStorage.clear(); window.sessionStorage.clear(); } catch(e){}")
    except Exception as exc:
        logger.debug("[账号登录] 清除 storage 失败: %s", exc)


def _login_on_page(
    driver, email: str, password: str, totp_secret: str, *,
    allow_email_otp: bool = False, email_source: str | None = None,
    log_prefix: str = "[账号导入]",
    reuse_session: bool = False,
) -> dict:
    """仅执行已有账号登录，不领取邮箱、不注册、不读取数据库凭据。"""
    from core import roxy_codex_oauth as login_pages

    pages._check_manual_stop()
    if reuse_session:
        driver.get("https://chatgpt.com/")
        pages._maybe_accept(driver)
        info = pages._read_chatgpt_session_once(driver)
        if info and info.get("accessToken"):
            if _is_session_expired(info.get("expires"), info.get("accessToken")):
                logger.info("%s 原账号环境会话已过期（expires=%s），清除旧会话并重新登录", log_prefix, info.get("expires"))
                _clear_browser_session_state(driver)
            else:
                is_valid, reason = _verify_chatgpt_session(driver, info["accessToken"], expected_email=email)
                if is_valid:
                    logger.info("%s 原账号环境登录态有效，直接读取当前会话", log_prefix)
                    return info
                logger.warning("%s 原账号环境虽有会话但已失效（%s），清除旧会话并重新登录", log_prefix, reason)
                _clear_browser_session_state(driver)
    driver.get("https://chatgpt.com/auth/login")
    pages._maybe_accept(driver)
    otp_after_ts = time.time()
    pages._submit_email_and_wait_next(driver, email, allow_existing=True)
    deadline = time.monotonic() + 120
    password_submitted = mfa_submitted = password_selected = False
    password_select_deadline = 0.0
    otp_submitted = False
    while time.monotonic() < deadline:
        pages._check_manual_stop()
        url = str(driver.current_url or "")
        path = urlparse(url).path.lower()
        if any(part in path for part in ("/create-account", "/signup", "/about-you")):
            raise RuntimeError("该邮箱进入注册/资料补全页面，请确认是已完成注册的账号")
        if urlparse(url).hostname == "chatgpt.com":
            info = pages._read_chatgpt_session_once(driver)
            if info:
                return info
        if login_pages._is_mfa_challenge_page(driver):
            if mfa_submitted:
                raise RuntimeError("MFA 验证未通过，请检查 2FA 密钥和系统时间")
            if not totp_secret:
                raise RuntimeError("登录需要 2FA 验证，但账号没有保存 2FA 密钥")
            logger.info("%s 使用本次账号的 2FA 密钥验证", log_prefix)
            if not login_pages._fill_mfa_challenge_if_present(driver, email, totp_secret=totp_secret):
                raise RuntimeError("MFA 页面未能提交验证码")
            mfa_submitted = True
        elif pages._is_login_password_page(driver):
            if password_submitted:
                raise RuntimeError("密码登录未通过，请检查账号密码")
            if not password:
                raise RuntimeError("登录需要密码，但账号没有保存登录密码")
            outcome = login_pages._fill_login_password_if_present(
                driver, email, password=password, totp_secret=totp_secret,
            )
            password_submitted = True
            # 密码提交函数可能同时处理 MFA；循环不能再次提交同一个 challenge。
            if login_pages._is_mfa_challenge_page(driver) and outcome == "next_step":
                raise RuntimeError("MFA 验证未通过，请检查 2FA 密钥和系统时间")
        elif pages._is_email_verification_page(driver):
            if otp_submitted:
                # 验证码已被接受时等待回调，不能因页面跳转慢再次取码提交。
                time.sleep(0.5)
                continue
            needs_email_otp = not password or password_submitted
            if not needs_email_otp and not password_selected:
                needs_email_otp = not _choose_login_password(driver)
                password_selected = True
                password_select_deadline = time.monotonic() + 15
            elif password_selected and time.monotonic() >= password_select_deadline:
                needs_email_otp = True
            if needs_email_otp:
                if not allow_email_otp:
                    raise EmailOtpRequiredError("登录需要邮箱验证码")
                _submit_login_email_otp(driver, email, after_ts=otp_after_ts, email_source=email_source, log_prefix=log_prefix)
                otp_submitted = True
                deadline = max(deadline, time.monotonic() + 45)
        else:
            # 只读取错误摘要，不输出页面内容或完整授权地址。
            body = driver.execute_script("return (document.body?.innerText || '').slice(0, 4000)")
            dead_code = detect_account_unusable_text(str(body or ""))
            if dead_code:
                raise AccountUnusableError("账号不可用", error_code=dead_code)
        time.sleep(0.5)
    raise RuntimeError("浏览器登录超时：未取得 ChatGPT 会话，请检查浏览器页面和代理出口")


def _fingerprint(driver, driver_mode: str) -> dict:
    """从实际浏览器读取环境画像，避免拿协议模拟画像冒充浏览器指纹。"""
    data = driver.execute_script("""
    return {user_agent:navigator.userAgent, navigator_platform:navigator.platform,
      navigator_language:navigator.language, navigator_languages:[...navigator.languages],
      timezone_iana:Intl.DateTimeFormat().resolvedOptions().timeZone,
      timezone_offset_minutes:-new Date().getTimezoneOffset(),
      screen_width:screen.width, screen_height:screen.height,
      device_pixel_ratio:devicePixelRatio, hardware_concurrency:navigator.hardwareConcurrency,
      device_memory:navigator.deviceMemory};
    """)
    return {**(data if isinstance(data, dict) else {}), "driver": driver_mode, "lifecycle": "fresh_per_import"}


def login_with_browser(
    email: str, password: str, totp_secret: str, *, proxy: str | None, driver_mode: str,
    allow_email_otp: bool = False, email_source: str | None = None, purpose: str = "import",
    account_id: int | None = None,
) -> dict:
    mode = _DRIVERS.get(driver_mode)
    if mode is None:
        raise ValueError(f"不支持的注册方式：{driver_mode}")
    log_prefix = "[查活]" if purpose == "live_check" else "[账号导入]"
    state: dict = {}
    account_environment = mode == "roxy" and purpose == "live_check" and account_id is not None
    environment = (_roxy_account_environment(account_id, email, proxy, state, log_prefix)
                   if account_environment else _browser_environment(mode, proxy, log_prefix, state))
    with environment as (driver, profile_id, proxy_used):
        driver._registration_log_prefix = f"{log_prefix}[{mode}]"
        lifecycle_label = ("复用当前账号已保存的浏览器环境" if state.get("reused") else
                           "新建并保留账号独占浏览器环境" if account_environment else "本次新建独立随机浏览器环境")
        logger.info("%s 指纹生命周期：%s；driver=%s profile=%s", log_prefix, lifecycle_label, mode, profile_id)
        if allow_email_otp or purpose == "live_check":
            reuse_kwargs = {"reuse_session": True} if state.get("reused") else {}
            session_info = _login_on_page(
                driver, email, password, totp_secret,
                allow_email_otp=allow_email_otp, email_source=email_source, log_prefix=log_prefix,
                **reuse_kwargs,
            )
        else:
            session_info = _login_on_page(driver, email, password, totp_secret)
        access_token = str(session_info.get("accessToken") or "").strip()
        if not access_token:
            raise RuntimeError("浏览器登录后未拿到 accessToken")
        actual_email = str((session_info.get("user") or {}).get("email") or "").strip()
        if actual_email and actual_email.lower() != email.lower():
            raise RuntimeError("浏览器返回的会话邮箱与本次登录账号不一致，已拒绝保存")
        fingerprint = _fingerprint(driver, mode)
        fingerprint["lifecycle"] = (("account_profile_reused" if state.get("reused") else "account_profile_created")
                                    if account_environment else f"fresh_per_{purpose}")
        if mode == "roxy":
            fingerprint["profile_id"] = profile_id
        _apply_post_login_data_saver(driver, getattr(driver, "_data_saver", None), log_prefix=log_prefix, mode=mode)
    return {
        "ok": True, "status": "live", "checked_at": _now(),
        "access_token": access_token, "session": session_info,
        "proxy_used": proxy_used or None, "fingerprint": fingerprint,
        "fingerprint_text": f"driver={mode} lifecycle={fingerprint['lifecycle']} ua={fingerprint.get('user_agent', '')}",
        "network_traffic": state.get("network_traffic"),
    }
