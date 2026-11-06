# -*- coding: utf-8 -*-
"""RoxyBrowser 本地 API 客户端。"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests

from config import roxybrowser as _cfg

logger = logging.getLogger(__name__)

# Roxy 的 /browser/create 在多 worker 同时到达时可能返回“正在创建中”。
# 为所有客户端实例共享创建时隙，确保进程内请求起始时间至少错开配置的间隔。
_CREATE_SLOT_LOCK = threading.Lock()
_NEXT_CREATE_SLOT = 0.0
# 创建 Profile 到拿到调试地址期间串行化，避免 Roxy 内核/端口竞争。
_ROXY_WINDOW_CREATE_LOCK = threading.Lock()


def _wait_for_create_slot() -> None:
    global _NEXT_CREATE_SLOT

    interval = max(0.0, float(getattr(_cfg, "ROXY_CREATE_INTERVAL", 1.5) or 0.0))
    if interval <= 0:
        return

    with _CREATE_SLOT_LOCK:
        now = time.monotonic()
        wait_for = max(0.0, _NEXT_CREATE_SLOT - now)
        _NEXT_CREATE_SLOT = max(now, _NEXT_CREATE_SLOT) + interval

    if wait_for > 0:
        logger.info("[Roxy] /browser/create 请求错峰，等待 %.2fs", wait_for)
        time.sleep(wait_for)


@dataclass
class RoxyOpenResult:
    profile_id: str
    raw: dict
    debugger_address: str | None = None
    webdriver_url: str | None = None
    ws_endpoint: str | None = None
    created_by_run: bool = False


class RoxyProfileMissingError(RuntimeError):
    """账号保存的 Roxy 环境已经不存在，可以新建替代环境。"""


class RoxyProfileReuseUnavailableError(RuntimeError):
    """当前 Roxy 版本无法完成旧环境复用，需要创建替代环境。"""


class RoxyApiError(RuntimeError):
    """保留 API 状态码，区分接口不支持、账号环境缺失与临时故障。"""

    def __init__(self, message: str, *, status_code: int, code=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _endpoint_unavailable(exc: RuntimeError) -> bool:
    """兼容已有调用方包装的异常文本，以及当前 API 的结构化错误。"""
    return (getattr(exc, "status_code", None) == 404
            or "http 404" in str(exc).lower()
            or (str(getattr(exc, "code", "")) == "104" and "not found" in str(exc).lower()))


def _strip_slashes(value: str) -> str:
    return str(value or "").strip().strip("/")


def _join_url(base: str, path: str) -> str:
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _mask_proxy(proxy_url: str) -> str:
    parsed = urlparse(str(proxy_url or "").strip())
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://***:***@{host}{port}"
    return str(proxy_url or "").strip()


def _proxy_url_to_roxy_info(proxy_url: str) -> dict:
    """
    将 config/proxy.py 里的代理 URL 转成 Roxy /browser/create 的 proxyInfo。

    支持：
      http://user:pass@host:port
      https://user:pass@host:port
      socks5://user:pass@host:port
      socks5h://user:pass@host:port  -> Roxy 侧按 SOCKS5 处理
    """
    text = str(proxy_url or "").strip()
    if not text:
        raise ValueError("代理为空")
    parsed = urlparse(text)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https", "socks5", "socks5h"):
        raise ValueError(f"Roxy 暂不支持该代理协议: {scheme or '-'}")
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"代理格式缺少 host/port: {_mask_proxy(text)}")

    protocol = {
        "http": "HTTP",
        "https": "HTTPS",
        "socks5": "SOCKS5",
        "socks5h": "SOCKS5",
    }[scheme]
    # Roxy /browser/create 官方字段是：
    # proxyMethod / proxyCategory / ipType / protocol / host / port / proxyUserName / proxyPassword / checkChannel
    # 之前误用了 proxyType/proxyHost/proxyPort/proxyAccount，Roxy 会忽略，导致创建窗口实际未设置代理。
    info = {
        "moduleId": 0,
        "proxyMethod": "custom",
        "proxyCategory": protocol,
        "ipType": "IPV4",
        "protocol": protocol,
        "host": parsed.hostname,
        "port": str(parsed.port),
    }
    if parsed.username:
        info["proxyUserName"] = unquote(parsed.username)
    if parsed.password:
        info["proxyPassword"] = unquote(parsed.password)
    check_channel = str(getattr(_cfg, "ROXY_PROXY_CHECK_CHANNEL", "") or "").strip()
    if check_channel:
        info["checkChannel"] = check_channel
    return info


def _dig(payload: dict, *keys: str):
    cur = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _first(payload: dict, paths: list[tuple[str, ...]]) -> str:
    for path in paths:
        value = _dig(payload, *path)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _workspace_id_value() -> str | int:
    raw = str(getattr(_cfg, "ROXY_WORKSPACE_ID", "") or "").strip()
    if not raw:
        return ""
    return int(raw) if raw.isdigit() else raw


def _project_id_value() -> str | int:
    raw = str(getattr(_cfg, "ROXY_PROJECT_ID", "") or "").strip()
    if not raw:
        return ""
    return int(raw) if raw.isdigit() else raw


def _apply_data_saver_open_args(params: dict) -> dict:
    """在 Roxy 启动参数中尽早关闭图片加载，覆盖无扩展名图片 URL。

    Network.setBlockedURLs 只能按 URL 后缀拦截，而 Roxy 浏览器在 Selenium 连接
    前就已经启动；使用 Chromium 开关可以让图片在首个页面请求前就被禁用。该开关
    只在用户明确开启省流量模式且包含 image 类型时追加。
    """
    try:
        from config import browser as _browser_cfg

        if not bool(getattr(_browser_cfg, "BROWSER_DATA_SAVER_MODE", False)):
            return params
        raw_types = getattr(_browser_cfg, "BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES", [])
        if isinstance(raw_types, str):
            types = {item.strip().lower() for item in raw_types.replace(",", "\n").splitlines() if item.strip()}
        else:
            types = {str(item or "").strip().lower() for item in (raw_types or []) if str(item or "").strip()}
        current = params.get("args")
        if isinstance(current, (list, tuple)):
            args = list(current)
        elif current:
            args = [str(current)]
        else:
            args = []
        switches = [
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-domain-reliability",
            "--disable-sync",
            "--disable-client-side-phishing-detection",
            "--disable-default-apps",
            "--disable-features=OptimizationGuideModelDownloading,OptimizationHintsFetching,OptimizationTargetPrediction,MediaRouter",
        ]
        if "image" in types or "images" in types or "img" in types:
            switches.append("--blink-settings=imagesEnabled=false")
        for s in switches:
            if s not in args:
                args.append(s)
        params["args"] = args
    except Exception as exc:
        logger.debug("[Roxy] 添加省流量图片启动参数失败，继续使用原参数：%s", exc)
    return params


def _random_roxy_os() -> str:
    raw = str(getattr(_cfg, "ROXY_RANDOM_OS_CHOICES", "Windows,macOS") or "Windows,macOS")
    choices = [
        x.strip()
        for part in raw.replace("\n", ",").replace(";", ",").split(",")
        for x in [part]
        if x.strip()
    ]
    valid = {"Windows", "macOS", "Linux", "IOS", "Android"}
    choices = [x for x in choices if x in valid]
    if not choices:
        choices = ["Windows", "macOS"]
    return random.choice(choices)


def _random_roxy_profile_name() -> str:
    prefix = str(getattr(_cfg, "ROXY_PROFILE_NAME_PREFIX", "rb") or "rb").strip() or "rb"
    # Roxy 环境名每次创建都不同：前缀 + 毫秒时间戳 + 随机 4 位十六进制。
    return f"{prefix}-{int(time.time() * 1000)}-{random.randrange(0x10000):04x}"


def get_available_roxy_core_versions() -> list[str]:
    """扫描本地 Roxy chrome-bin 目录下可用的安全内核版本列表（自动过滤 155+ 官方暗桩版本）。"""
    import os
    import sys
    blacklisted = {"155"}
    is_mac = sys.platform == "darwin"
    pool_raw = str(getattr(_cfg, "ROXY_CORE_VERSION_POOL", "") or "143,149,150,151,152,153,154").strip()
    configured_pool = [
        v.strip()
        for part in pool_raw.replace("\n", ",").replace(";", ",").split(",")
        for v in [part.strip()]
        if v and v not in blacklisted
    ]
    if not configured_pool:
        configured_pool = ["143", "149", "150", "151", "152", "153", "154"]

    bin_dir = Path(os.path.expanduser("~/Library/Application Support/RoxyBrowser/chrome-bin"))
    if not bin_dir.exists() and os.name == "nt":
        app_data = os.getenv("APPDATA") or os.path.expanduser("~/AppData/Roaming")
        bin_dir = Path(app_data) / "RoxyBrowser" / "chrome-bin"

    if not bin_dir.exists():
        return configured_pool

    valid = []
    for ver in configured_pool:
        vdir = bin_dir / ver
        if vdir.is_dir():
            exe = vdir / "RoxyChrome.app" / "Contents" / "MacOS" / "RoxyChrome" if is_mac else vdir / "RoxyChrome.exe"
            drv = vdir / "chromedriver" if is_mac else vdir / "chromedriver.exe"
            if exe.exists() and drv.exists():
                valid.append(ver)

    return valid or configured_pool


def _random_roxy_core_version() -> str | None:
    if not bool(getattr(_cfg, "ROXY_RANDOM_CORE_VERSION", True)):
        return None
    available = get_available_roxy_core_versions()
    return random.choice(available) if available else None

class RoxyBrowserClient:
    def __init__(self, api_base: str | None = None, token: str | None = None):
        self.api_base = (api_base or _cfg.ROXY_API_BASE).strip()
        self.token = (token if token is not None else _cfg.ROXY_API_TOKEN).strip()
        self._proxy_pool_relay = None
        self._proxy_pool_target = ""
        self.http = requests.Session()
        if self.token:
            # 官方文档要求所有接口请求头必须加 token。这里同时兼容 token / Authorization。
            self.http.headers.update({
                "token": self.token,
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            })

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        text = str(exc or "").lower()
        return (
            "timeout" in text
            or "timed out" in text
            or "connection" in text
            or "temporarily" in text
            or "http 500" in text
            or "http 502" in text
            or "http 503" in text
            or "http 504" in text
            or "http 429" in text
        )

    def request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> dict:
        url = _join_url(self.api_base, path)
        method_u = method.upper()
        is_create = str(path or "").rstrip("/").endswith("/create") or "browser/create" in str(path or "")
        if is_create:
            max_attempts = max(1, int(getattr(_cfg, "ROXY_CREATE_RETRIES", 3) or 3))
            base_delay = max(0.5, float(getattr(_cfg, "ROXY_CREATE_RETRY_DELAY", 3) or 3))
        else:
            max_attempts = max(1, int(getattr(_cfg, "ROXY_API_RETRIES", 3) or 3))
            base_delay = max(0.5, float(getattr(_cfg, "ROXY_API_RETRY_DELAY", 2) or 2))
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                logger.debug(
                    "[Roxy] %s %s params=%s body=%s attempt=%s/%s",
                    method, url, params, json_body, attempt, max_attempts,
                )
                resp = self.http.request(
                    method_u,
                    url,
                    params=params or None,
                    json=json_body if json_body is not None else None,
                    timeout=max(5, int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)),
                )
                text = resp.text or ""
                try:
                    payload = resp.json()
                except Exception:
                    payload = {"raw": text}
                if not (200 <= resp.status_code < 300):
                    raise RoxyApiError(
                        f"Roxy API 请求失败 {method_u} {path} HTTP {resp.status_code}: {text[:500]}",
                        status_code=resp.status_code, code=payload.get("code") if isinstance(payload, dict) else None,
                    )
                if isinstance(payload, dict):
                    code = payload.get("code")
                    ok = payload.get("ok")
                    success = payload.get("success")
                    if code not in (None, 0, 200, "0", "200") and ok is not True and success is not True:
                        msg = payload.get("msg") or payload.get("message") or payload.get("error") or json.dumps(payload, ensure_ascii=False)[:500]
                        raise RoxyApiError(f"Roxy API 返回失败 {method_u} {path}: {msg}", status_code=resp.status_code, code=code)
                if attempt > 1:
                    logger.info("[Roxy] API 重试成功：%s %s attempt=%s/%s", method_u, path, attempt, max_attempts)
                return payload if isinstance(payload, dict) else {"data": payload}
            except Exception as exc:
                last_exc = exc
                retryable = self._is_retryable_error(exc)
                if attempt >= max_attempts or not retryable:
                    raise
                delay = base_delay * attempt
                logger.warning(
                    "[Roxy] API 请求失败，将在 %.1fs 后使用相同%s重试：%s %s attempt=%s/%s error=%s",
                    delay,
                    " create payload/name " if is_create else "请求参数",
                    method_u, path, attempt, max_attempts, exc,
                )
                time.sleep(delay)
        raise last_exc or RuntimeError(f"Roxy API 请求失败 {method_u} {path}")

    def try_request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> tuple[bool, dict | str]:
        """宽松请求：用于探测不同 Roxy 版本接口，失败不抛出。"""
        try:
            return True, self.request(method, path, params=params, json_body=json_body)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _extract_workspace_items(payload: dict) -> list[dict]:
        """解析 /browser/workspace：团队 rows + project_details 项目列表；兼容递归兜底。"""
        out = []

        # 官方结构：data.rows[].id/workspaceName/project_details[].projectId/projectName
        rows = None
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                rows = data.get("rows") or data.get("list") or data.get("records")
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                wid = row.get("id") or row.get("workspaceId") or row.get("workspace_id")
                wname = row.get("workspaceName") or row.get("workspace_name") or row.get("name") or str(wid or "")
                projects = row.get("project_details") or row.get("projectDetails") or row.get("projects") or []
                if isinstance(projects, list) and projects:
                    for proj in projects:
                        if not isinstance(proj, dict):
                            continue
                        pid = proj.get("projectId") or proj.get("project_id") or proj.get("id")
                        pname = proj.get("projectName") or proj.get("project_name") or proj.get("name") or str(pid or "")
                        if wid:
                            out.append({
                                "id": str(wid),
                                "name": str(wname),
                                "projectId": str(pid or ""),
                                "projectName": str(pname or ""),
                                "label": f"{wname} / {pname} ({wid}/{pid})" if pid else f"{wname} ({wid})",
                                "raw": {"workspace": row, "project": proj},
                            })
                elif wid:
                    out.append({
                        "id": str(wid),
                        "name": str(wname),
                        "projectId": "",
                        "projectName": "",
                        "label": f"{wname} ({wid})",
                        "raw": row,
                    })

        if out:
            return out

        # 兜底：递归抽 workspace/team/company 结构。
        def pick_id_name(item: dict) -> tuple[str, str]:
            wid = _first(item, [
                ("workspaceId",), ("workspace_id",), ("workspaceID",),
                ("teamId",), ("team_id",), ("teamID",),
                ("companyId",), ("company_id",), ("orgId",), ("org_id",),
                ("id",), ("value",), ("key",),
            ])
            name = _first(item, [
                ("workspaceName",), ("workspace_name",),
                ("teamName",), ("team_name",),
                ("companyName",), ("company_name",),
                ("orgName",), ("org_name",),
                ("name",), ("label",), ("title",), ("remark",),
            ])
            return wid, name

        def looks_like_workspace(item: dict) -> bool:
            keys = {str(k).lower() for k in item.keys()}
            joined = " ".join(keys)
            return any(x in joined for x in ("workspace", "team", "company", "org")) or ("id" in keys and "name" in keys)

        def walk(node):
            if isinstance(node, dict):
                wid, name = pick_id_name(node)
                if wid and looks_like_workspace(node):
                    out.append({"id": wid, "name": name or wid, "projectId": "", "projectName": "", "label": f"{name or wid} ({wid})", "raw": node})
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)
        dedup = {}
        for item in out:
            raw_keys = {str(k).lower() for k in (item.get("raw") or {}).keys()}
            if "dirid" in raw_keys and not any(k in raw_keys for k in ("workspaceid", "teamid", "companyid")):
                continue
            key = f"{item.get('id')}::{item.get('projectId','')}"
            dedup[key] = item
        return list(dedup.values())

    def list_workspaces(self) -> dict:
        """
        获取 Roxy 团队/工作区列表。
        Roxy 不同版本路径可能有差异，因此先试配置路径，再试常见路径。
        """
        configured = str(getattr(_cfg, "ROXY_WORKSPACE_LIST_PATH", "") or "").strip()
        method = str(getattr(_cfg, "ROXY_WORKSPACE_LIST_METHOD", "GET") or "GET").upper()
        candidates = []
        if configured:
            candidates.append((method, configured))
        candidates.extend([
            ("GET", "/browser/workspace"),
            ("POST", "/browser/workspace"),
            ("GET", "/workspace/list"),
            ("POST", "/workspace/list"),
            ("GET", "/workspace"),
            ("POST", "/workspace"),
            ("GET", "/team/list"),
            ("POST", "/team/list"),
            ("GET", "/team"),
            ("POST", "/team"),
            ("GET", "/workspaces"),
            ("GET", "/teams"),
            ("GET", "/user/workspace/list"),
            ("POST", "/user/workspace/list"),
            ("GET", "/user/team/list"),
            ("POST", "/user/team/list"),
            ("GET", "/api/workspace/list"),
            ("POST", "/api/workspace/list"),
            ("GET", "/api/team/list"),
            ("POST", "/api/team/list"),
            ("GET", "/browser/workspace/list"),
            ("POST", "/browser/workspace/list"),
            ("GET", "/browser/team/list"),
            ("POST", "/browser/team/list"),
        ])

        errors = []
        seen = set()
        for m, path in candidates:
            key = (m, path)
            if key in seen:
                continue
            seen.add(key)
            ok, payload = self.try_request(m, path)
            if not ok:
                errors.append({"method": m, "path": path, "error": payload})
                continue
            items = self._extract_workspace_items(payload if isinstance(payload, dict) else {})
            if items:
                return {"ok": True, "path": path, "method": m, "items": items, "raw": payload}
            errors.append({"method": m, "path": path, "error": "响应中未解析到团队/工作区列表", "payload": payload})

        return {"ok": False, "items": [], "errors": errors}

    def create_profile(self, payload: dict | None = None, *, proxy: str | None = None) -> str:
        body = dict(getattr(_cfg, "ROXY_PROFILE_CREATE_PAYLOAD", {}) or {})
        if payload:
            body.update(payload)
        random_name_enabled = bool(getattr(_cfg, "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", True))
        if random_name_enabled:
            # 覆盖 ROXY_PROFILE_CREATE_PAYLOAD 里的固定 name，避免所有 Roxy 窗口同名。
            body["name"] = _random_roxy_profile_name()
        random_os_enabled = bool(getattr(_cfg, "ROXY_RANDOM_OS_ON_CREATE", True))
        if random_os_enabled:
            # 每次创建环境随机 Windows / macOS；覆盖 ROXY_PROFILE_CREATE_PAYLOAD 里的固定 os。
            body["os"] = _random_roxy_os()
            # osVersion 跟 os 强绑定，随机 OS 时不沿用固定版本，避免 macOS 版本传给 Windows。
            body.pop("osVersion", None)
        else:
            default_os = str(getattr(_cfg, "ROXY_DEFAULT_OS", "macOS") or "macOS").strip()
            if default_os:
                # Roxy 官方枚举大小写敏感：Windows / macOS / Linux / IOS / Android。
                body.setdefault("os", default_os)
            default_os_version = str(getattr(_cfg, "ROXY_DEFAULT_OS_VERSION", "") or "").strip()
            if default_os_version:
                body.setdefault("osVersion", default_os_version)
        random_core_enabled = bool(getattr(_cfg, "ROXY_RANDOM_CORE_VERSION", True))
        if random_core_enabled and "coreVersion" not in body:
            chosen_core = _random_roxy_core_version()
            if chosen_core:
                body["coreVersion"] = chosen_core
        workspace_id = _workspace_id_value()
        if workspace_id:
            # Roxy 官方 /browser/create 要求 workspaceId。
            body.setdefault("workspaceId", workspace_id)
        project_id = _project_id_value()
        if project_id:
            body.setdefault("projectId", project_id)
        if proxy is not None:
            # 导入任务显式选定的出口优先，空字符串表示直连，不能再抽注册代理池。
            body.pop("proxyInfo", None)
            if proxy:
                body["proxyInfo"] = _proxy_url_to_roxy_info(proxy)
                body["proxy"] = proxy
            else:
                # 显式直连必须清空模板代理；省略字段会让本地 Roxy API 继承旧出口。
                body["proxyInfo"] = {"proxyMethod": "custom", "proxyCategory": "noproxy"}
                body["proxy"] = "direct"
            self._proxy_pool_target = proxy
        if proxy is None and bool(getattr(_cfg, "ROXY_CREATE_USE_PROXY_POOL", False)) and not body.get("proxyInfo"):
            from config import proxy as _proxy_cfg
            from core.proxy_chain import open_proxy_pool_proxy

            target_proxy = _proxy_cfg.pick_proxy()
            proxy_url, relay = open_proxy_pool_proxy(target_proxy, measure_transport=True)
            self._proxy_pool_relay = relay
            self._proxy_pool_target = str(target_proxy or "").strip()
            if proxy_url:
                proxy_info = _proxy_url_to_roxy_info(proxy_url)
                body["proxyInfo"] = proxy_info
                body["proxy"] = proxy_url
                logger.info(
                    "[Roxy] 创建环境启用代理池：target=%s transport=%s type=%s host=%s port=%s",
                    _mask_proxy(target_proxy), _mask_proxy(proxy_url),
                    proxy_info.get("protocol") or proxy_info.get("proxyCategory"),
                    proxy_info.get("host"),
                    proxy_info.get("port"),
                )
            else:
                body["proxyInfo"] = {"proxyMethod": "custom", "proxyCategory": "noproxy"}
                body["proxy"] = "direct"
                logger.warning("[Roxy] 已启用 ROXY_CREATE_USE_PROXY_POOL，但 PROXY_POOL 为空，本次创建环境不设置代理")
        if not body.get("proxyInfo") and not body.get("proxy"):
            # 未开启代理池且未指定代理时，显式指定直连，禁止继承任何旧模板中的代理
            body["proxyInfo"] = {"proxyMethod": "custom", "proxyCategory": "noproxy"}
            body["proxy"] = "direct"
        if not body.get("workspaceId"):
            raise RuntimeError(
                "Roxy 创建环境需要 workspaceId。请在 config/roxybrowser.py 或 WebUI 的 RoxyBrowser 配置中填写 ROXY_WORKSPACE_ID，"
                "或直接在 ROXY_PROFILE_CREATE_PAYLOAD 里加入 {'workspaceId': '你的工作区ID'}。"
            )
        logger.info(
            "[Roxy] 创建环境参数：workspaceId=%s projectId=%s name=%s random_name=%s os=%s osVersion=%s random_os=%s coreVersion=%s",
            body.get("workspaceId"),
            body.get("projectId") or "-",
            body.get("name") or "-",
            random_name_enabled,
            body.get("os") or "-",
            body.get("osVersion") or "-",
            random_os_enabled,
            body.get("coreVersion") or "-",
        )
        _wait_for_create_slot()
        result = self.request(_cfg.ROXY_CREATE_METHOD, _cfg.ROXY_CREATE_PATH, json_body=body)
        profile_id = _first(result, [
            ("id",), ("dirId",), ("dir_id",), ("profile_id",), ("profileId",), ("browser_id",),
            ("data", "id"), ("data", "dirId"), ("data", "dir_id"),
            ("data", "profile_id"), ("data", "profileId"), ("data", "browser_id"),
        ])
        if not profile_id:
            raise RuntimeError(f"Roxy 创建环境成功但未返回 dirId/profile_id: {result}")
        return profile_id

    @staticmethod
    def _normalize_profile_id(value: str | None) -> str:
        text = str(value or "").strip()
        # WebUI/人工配置里常用 - 表示“未配置”，这里统一按空处理。
        if text in ("-", "—", "无", "空", "none", "None", "null", "NULL"):
            return ""
        return text

    def open_profile(
        self, profile_id: str | None = None, *, fresh: bool = False, proxy: str | None = None,
        reuse: bool = False,
    ) -> RoxyOpenResult:
        if reuse and (fresh or not self._normalize_profile_id(profile_id)):
            raise ValueError("复用 Roxy 环境必须显式提供账号 Profile ID，且不能同时要求新建")
        with _ROXY_WINDOW_CREATE_LOCK:
            if fresh or proxy is not None or reuse:
                self._opening_profile = None
                try:
                    return self._open_profile_locked(profile_id, fresh=fresh, proxy=proxy, reuse=reuse)
                except Exception:
                    # 创建成功但打开失败时，也清理本次临时环境。
                    try:
                        self.cleanup_profile(self._opening_profile)
                    except Exception:
                        logger.debug("[Roxy] 打开失败后清理环境异常", exc_info=True)
                    raise
            return self._open_profile_locked(profile_id)

    def _open_profile_locked(
        self, profile_id: str | None = None, *, fresh: bool = False, proxy: str | None = None,
        reuse: bool = False,
    ) -> RoxyOpenResult:
        one_profile = bool(getattr(_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True))
        configured_pid = self._normalize_profile_id(profile_id if profile_id is not None else getattr(_cfg, "ROXY_PROFILE_ID", ""))
        if fresh:
            configured_pid = ""
        if one_profile and configured_pid and not reuse:
            raise RuntimeError(
                "已启用 ROXY_ONE_PROFILE_PER_ACCOUNT=True（一号一环境），"
                "不能配置/传入固定 ROXY_PROFILE_ID；请留空以便每个账号创建新环境。"
            )

        pid = configured_pid
        created_by_run = False
        if reuse:
            # 先确认环境存在；API 鉴权/网络故障不能当作环境删除而重复新建。
            self.get_profile_detail(pid)
            if proxy is not None:
                self.update_profile_proxy(pid, proxy)
            self._opening_profile = RoxyOpenResult(pid, {}, created_by_run=False)
        if not pid:
            pid = self.create_profile(proxy=proxy) if proxy is not None else self.create_profile()
            created_by_run = True
            self._opening_profile = RoxyOpenResult(pid, {}, created_by_run=True)
            logger.info("[Roxy] 已创建临时环境：%s", pid)

        path = str(_cfg.ROXY_OPEN_PATH).format(profile_id=pid)
        params = dict(getattr(_cfg, "ROXY_OPEN_EXTRA_PARAMS", {}) or {})
        # Roxy 官方 /browser/open body: {workspaceId, dirId, args, forceOpen, headless}
        params.setdefault("workspaceId", _workspace_id_value())
        params.setdefault("dirId", int(pid) if str(pid).isdigit() else pid)
        if fresh or reuse:
            # 新建和账号复用都必须打开指定环境，不能被全局 extra 覆盖。
            params["dirId"] = int(pid) if str(pid).isdigit() else pid
            params["workspaceId"] = _workspace_id_value()
        params.setdefault("args", [])
        params.setdefault("forceOpen", True)
        _apply_data_saver_open_args(params)
        # ROXY_OPEN_HEADLESS 是显式开关，优先级应高于 ROXY_OPEN_EXTRA_PARAMS，
        # 否则 extra 里残留 headless=False 会导致 WebUI 保存无头后仍弹窗口。
        params["headless"] = bool(getattr(_cfg, "ROXY_OPEN_HEADLESS", False))
        logger.info("[Roxy] open 参数：profile=%s headless=%s keep_open=%s", pid, params.get("headless"), getattr(_cfg, "ROXY_KEEP_BROWSER_OPEN", False))
        result = self.request(
            _cfg.ROXY_OPEN_METHOD,
            path,
            params=params if _cfg.ROXY_OPEN_METHOD.upper() == "GET" else None,
            json_body=params if _cfg.ROXY_OPEN_METHOD.upper() != "GET" else None,
        )
        debugger_address = self._extract_debugger_address(result)
        logger.info("[Roxy] open 返回摘要: debugger=%s raw=%s", debugger_address, json.dumps(result, ensure_ascii=False)[:800])
        webdriver_url = _first(result, [
            ("webdriver",), ("webDriver",), ("webdriver_url",), ("webdriverUrl",),
            ("selenium",), ("selenium_url",), ("seleniumUrl",),
            ("data", "webdriver"), ("data", "webDriver"), ("data", "webdriver_url"), ("data", "webdriverUrl"),
            ("data", "selenium"), ("data", "selenium_url"), ("data", "seleniumUrl"),
        ]) or None
        ws_endpoint = _first(result, [
            ("ws",), ("wsEndpoint",), ("ws_endpoint",), ("debuggerWsUrl",),
            ("data", "ws"), ("data", "wsEndpoint"), ("data", "ws_endpoint"), ("data", "debuggerWsUrl"),
        ]) or None
        if not debugger_address and not webdriver_url:
            raise RuntimeError(f"Roxy 已打开环境但未返回 Selenium/调试地址，请检查 ROXY_OPEN_PATH 或接口响应: {result}")
        if self._proxy_pool_target:
            result = dict(result)
            result["proxy_pool_target"] = self._proxy_pool_target
        return RoxyOpenResult(
            pid,
            result,
            debugger_address=debugger_address,
            webdriver_url=webdriver_url,
            ws_endpoint=ws_endpoint,
            created_by_run=created_by_run,
        )

    def get_profile_detail(self, profile_id: str) -> dict | None:
        """详情接口不可用时兼容新旧列表接口，不能把接口 404 直接当作环境丢失。"""
        try:
            result = self.request("GET", "/browser/detail", params={
                "workspaceId": _workspace_id_value(), "dirId": profile_id,
            })
        except RuntimeError as exc:
            if _endpoint_unavailable(exc):
                logger.info("[Roxy] 详情接口不可用，改用环境列表确认账号原环境")
                return self._find_profile_in_lists(profile_id)
            text = str(exc).lower()
            if any(hint in text for hint in ("profile not found", "profile does not exist", "窗口不存在", "环境不存在", "已删除")):
                raise RoxyProfileMissingError("账号原 Roxy 环境已删除") from exc
            raise
        data = result.get("data") or {}
        rows = data.get("rows") if isinstance(data, dict) else None
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and str(row.get("dirId") or "") == str(profile_id):
                    return row
            raise RoxyProfileMissingError("账号原 Roxy 环境不存在")
        if isinstance(data, dict) and str(data.get("dirId") or "") == str(profile_id):
            return data
        raise RuntimeError("Roxy 环境详情响应缺少环境记录，无法确认环境是否存在")

    def _find_profile_in_lists(self, profile_id: str) -> dict | None:
        for path in ("/browser/list_v3", "/browser/list"):
            page = 1
            seen_ids: set[str] = set()
            while True:
                try:
                    result = self.request("GET", path, params={
                        "workspaceId": _workspace_id_value(), "dirIds": profile_id,
                        "page_index": page, "page_size": 100,
                    })
                except RuntimeError as exc:
                    if _endpoint_unavailable(exc):
                        break
                    raise
                data = result.get("data") or {}
                rows = data.get("rows") if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    raise RuntimeError("Roxy 环境列表响应缺少 rows，无法确认环境是否存在")
                page_ids = {str(row.get("dirId") or "") for row in rows if isinstance(row, dict)} - {""}
                for row in rows:
                    if isinstance(row, dict) and str(row.get("dirId") or "") == str(profile_id):
                        return row
                total = data.get("total")
                if not rows or (total is not None and len(seen_ids | page_ids) >= int(total)) or (total is None and len(rows) < 100):
                    raise RoxyProfileMissingError("账号原 Roxy 环境不在环境列表中")
                if not page_ids - seen_ids or page >= 100:
                    raise RuntimeError("Roxy 环境列表分页未推进，无法确认环境是否存在")
                seen_ids.update(page_ids)
                page += 1
        logger.info("[Roxy] 当前版本不支持详情及列表查询，继续尝试直接复用账号原环境")
        return None

    def update_profile_proxy(self, profile_id: str, proxy: str) -> None:
        """仅更新代理，保留原环境的指纹、Cookie 和本地存储。"""
        proxy_info = _proxy_url_to_roxy_info(proxy) if proxy else {
            "proxyMethod": "custom", "proxyCategory": "noproxy",
        }
        try:
            self.request("POST", "/browser/mdf", json_body={
                "workspaceId": _workspace_id_value(), "dirId": profile_id, "proxyInfo": proxy_info,
            })
        except RuntimeError as exc:
            if _endpoint_unavailable(exc):
                raise RoxyProfileReuseUnavailableError("当前 Roxy 版本不支持更新旧环境代理，改为新建账号环境") from exc
            raise
        self._proxy_pool_target = proxy

    def close_profile(self, profile_id: str) -> None:
        if not profile_id:
            return
        path = str(_cfg.ROXY_CLOSE_PATH).format(profile_id=profile_id)
        try:
            body = {
                "workspaceId": _workspace_id_value(),
                "dirId": int(profile_id) if str(profile_id).isdigit() else profile_id,
            }
            self.request(
                _cfg.ROXY_CLOSE_METHOD,
                path,
                params=body if str(_cfg.ROXY_CLOSE_METHOD).upper() == "GET" else None,
                json_body=body if str(_cfg.ROXY_CLOSE_METHOD).upper() != "GET" else None,
            )
            logger.info("[Roxy] 已关闭环境：%s", profile_id)
        except Exception as exc:
            logger.warning("[Roxy] 关闭环境失败：%s", exc)

    def delete_profile(self, profile_id: str) -> None:
        if not profile_id:
            return
        path = str(getattr(_cfg, "ROXY_DELETE_PATH", "/browser/delete")).format(profile_id=profile_id)
        method = str(getattr(_cfg, "ROXY_DELETE_METHOD", "POST") or "POST")
        try:
            body = {
                "workspaceId": _workspace_id_value(),
                "dirId": int(profile_id) if str(profile_id).isdigit() else profile_id,
                "dirIds": [int(profile_id) if str(profile_id).isdigit() else profile_id],
            }
            self.request(
                method,
                path,
                params=body if method.upper() == "GET" else None,
                json_body=body if method.upper() != "GET" else None,
            )
            logger.info("[Roxy] 已删除环境：%s", profile_id)
        except Exception as exc:
            logger.warning("[Roxy] 删除环境失败：%s", exc)

    def cleanup_profile(self, opened: RoxyOpenResult | None, *, retain: bool = False) -> None:
        """结束时关闭窗口；查活绑定环境保留，其他临时环境按配置删除。"""
        keep_open = bool(getattr(_cfg, "ROXY_KEEP_BROWSER_OPEN", False))
        try:
            if not opened or not opened.profile_id:
                return
            if not keep_open:
                self.close_profile(opened.profile_id)

            delete_after_run = bool(getattr(_cfg, "ROXY_DELETE_PROFILE_AFTER_RUN", True))
            one_profile = bool(getattr(_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True))
            if one_profile and bool(opened.created_by_run):
                should_delete = delete_after_run if retain else True
            else:
                should_delete = False

            if should_delete:
                # 删除前尽量确保已关闭；若 keep_open=True 则不删除，便于调试保留现场。
                if keep_open:
                    logger.info("[Roxy] ROXY_KEEP_BROWSER_OPEN=True，跳过删除环境：%s", opened.profile_id)
                    return
                self.delete_profile(opened.profile_id)
            elif retain and opened.created_by_run:
                logger.info(
                    "[Roxy] 账号已成功保存/授权，环境已关闭并保留（ROXY_DELETE_PROFILE_AFTER_RUN=False）：%s",
                    opened.profile_id,
                )
        finally:
            if not keep_open:
                self.close_proxy_pool_relay()

    def close_proxy_pool_relay(self) -> None:
        relay, self._proxy_pool_relay = self._proxy_pool_relay, None
        if relay is not None:
            relay.close()
            # last_error 是本轮历史错误，不能在成功任务收尾时再次当作当前故障告警。
            # 未恢复的连接失败已在发生处带 destination 输出 WARNING。
            targets = relay.traffic_snapshot().get("destinations", [])
            failed = sum(item.get("failed_connection_count", 0) for item in targets)
            retries = sum(item.get("connect_retry_count", 0) for item in targets)
            recovered = sum(item.get("recovered_connection_count", 0) for item in targets)
            if failed or retries:
                logger.info("[Roxy] 代理链连接汇总：握手重试=%s，重试恢复=%s，失败连接=%s（失败详情见发生时的 destination 日志）",
                            retries, recovered, failed)

    def proxy_transport_snapshot(self) -> dict | None:
        """返回本轮 Roxy 经本地代理链传输的全浏览器流量。"""
        relay = self._proxy_pool_relay
        if relay is None:
            return None
        try:
            return relay.traffic_snapshot()
        except Exception:
            return None

    @staticmethod
    def _extract_debugger_address(payload: dict) -> str | None:
        value = _first(payload, [
            ("debuggerAddress",), ("debugger_address",), ("debugAddress",),
            ("debuggingPortUrl",), ("debugging_port_url",),
            ("remoteDebuggingAddress",), ("remote_debugging_address",),
            ("http",), ("debugHttp",), ("debug_http",),
            ("data", "debuggerAddress"), ("data", "debugger_address"), ("data", "debugAddress"),
            ("data", "debuggingPortUrl"), ("data", "debugging_port_url"),
            ("data", "remoteDebuggingAddress"), ("data", "remote_debugging_address"),
            ("data", "http"), ("data", "debugHttp"), ("data", "debug_http"),
        ])
        if value:
            value = value.strip()
            # 兼容 http://127.0.0.1:xxxx / 127.0.0.1:xxxx / :xxxx / 9222
            value = value.replace("http://", "").replace("https://", "").strip("/")
            if value.startswith(":") and value[1:].isdigit():
                return f"127.0.0.1{value}"
            if value.isdigit():
                return f"127.0.0.1:{value}"
            if ":" in value and not value.startswith(":"):
                return value
        port = _first(payload, [
            ("debuggingPort",), ("debugging_port",), ("debug_port",), ("port",),
            ("data", "debuggingPort"), ("data", "debugging_port"), ("data", "debug_port"), ("data", "port"),
        ])
        if port:
            port = str(port).strip()
            if port.startswith(":"):
                port = port[1:]
            if port.isdigit():
                return f"127.0.0.1:{port}"
        return None
