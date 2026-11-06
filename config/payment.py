# -*- coding: utf-8 -*-
"""Plus 代付与支付渠道配置。"""
from config.env_loader import apply_env_overrides

# ---------- 支付通道通用配置 ----------
# 是否在提链成功后自动发起代付
ENABLE_AUTO_PAY_AFTER_EXTRACT: bool = False

# 当前激活的支付渠道：foarge (默认)
PAYMENT_CHANNEL: str = "foarge"

# 后台代付并发与队列
PAYMENT_WORKERS: int = 3
PAYMENT_QUEUE_LIMIT: int = 500
PAYMENT_POLL_INTERVAL: int = 8
PAYMENT_MAX_WAIT_SECONDS: int = 600

# ---------- Foarge (Crowd Publisher 众包代付平台) 配置 ----------
# Foarge API 基础网关地址
FOARGE_API_BASE: str = "https://foarge.com/api/publisher/v1"

# 认证模式：pbk (预付卡密按次扣减，推荐) 或 api_key (钱包余额扣款)
FOARGE_AUTH_MODE: str = "pbk"

# PBK 卡密（格式 PBK-XXXX-XXXX-XXXX，保存在 .env）
FOARGE_PBK_KEY: str = ""

# 钱包 API Key（格式 pk_live_xxxx，保存在 .env）
FOARGE_API_KEY: str = ""

# 默认优先代付通道：upi (印度) / pix (巴西) / blik (波兰)
FOARGE_PAYMENT_METHOD: str = "upi"

# 自愿加价赏金(元)：在平台底价基础上额外增加赏金以提高工人抢单优先级
FOARGE_MARKUP_CNY: float = 0.0

# 专属工人圈 ID（可选，逗号分隔，如 w_4fe73ac5ad0a）
FOARGE_PREFERRED_WORKER_IDS: str = ""

# 专属工人圈优先抢单窗口（秒，默认 45 秒；0 为严格独占）
FOARGE_FALLBACK_SEC: int = 45

# 请求超时（秒）
FOARGE_TIMEOUT: int = 30

# 是否在支付码即将到期时自动重新提交新码刷新
FOARGE_AUTO_REFRESH_QR: bool = True

apply_env_overrides(globals(), {
    'ENABLE_AUTO_PAY_AFTER_EXTRACT': 'bool',
    'PAYMENT_CHANNEL': 'str',
    'PAYMENT_WORKERS': 'int',
    'PAYMENT_QUEUE_LIMIT': 'int',
    'PAYMENT_POLL_INTERVAL': 'int',
    'PAYMENT_MAX_WAIT_SECONDS': 'int',
    'FOARGE_API_BASE': 'str',
    'FOARGE_AUTH_MODE': 'str',
    'FOARGE_PBK_KEY': 'str',
    'FOARGE_API_KEY': 'str',
    'FOARGE_PAYMENT_METHOD': 'str',
    'FOARGE_MARKUP_CNY': 'float',
    'FOARGE_PREFERRED_WORKER_IDS': 'str',
    'FOARGE_FALLBACK_SEC': 'int',
    'FOARGE_TIMEOUT': 'int',
    'FOARGE_AUTO_REFRESH_QR': 'bool',
})
