# 提链服务接口契约

本文档描述账号管理客户端调用的“提链服务”接口。提链服务方按本文档实现后，客户端只需要把 `EXTRACT_LINK_API_BASE` 配置为服务根地址即可。

## 1. 术语和边界

- **客户端**：本项目 WebUI 后台服务。
- **提链服务**：负责校验 CDK、使用 ChatGPT `access_token` 创建提链任务、执行任务并返回支付/兑换链接的外部服务。
- **服务根地址**：客户端配置项 `EXTRACT_LINK_API_BASE`。客户端会在该地址后拼接本文档中的路径，因此根地址不要再包含 `/api`。
- **CDK**：提链服务方分配的兑换/调用码。客户端会将 CDK 通过 HTTPS 查询参数或 JSON 请求体发送。
- **access_token**：账号登录后保存的 ChatGPT 访问令牌。该值属于敏感凭据，服务端不得写入普通日志、错误响应或 SSE 日志。

示例：

```text
EXTRACT_LINK_API_BASE=https://example.com
```

客户端实际请求：

```text
GET  https://example.com/api/cdk?code=...
POST https://example.com/api/extract
GET  https://example.com/api/jobs/{job_id}/events?cdk=...
```

## 2. 通用约定

- 所有接口必须使用 HTTPS。
- JSON 请求使用 `Content-Type: application/json`，字符集为 UTF-8。
- JSON 响应使用 `Content-Type: application/json; charset=utf-8`。
- 成功响应使用 2xx 状态码；参数、CDK、账号或业务失败使用 4xx；服务内部故障使用 5xx。
- 错误响应至少包含一个可读的 `error` 字段：

```json
{
  "error": "错误说明"
}
```

`error` 也可以是对象，客户端会依次尝试读取 `message`、`detail`、`reason`、`error`、`msg`、`description` 字段：

```json
{
  "error": {
    "code": "CDK_INVALID",
    "message": "CDK 无效或已过期"
  }
}
```

错误响应不得包含 `access_token`、完整 CDK、账号密码或 2FA 信息。

支持的提链类型：

```text
pix
upi
kakao_pay
ideal
```

类型值必须使用小写字符串。客户端默认值由 `EXTRACT_LINK_TYPE` 配置决定，当前配置可传入 `upi` 等类型覆盖默认值。

## 3. 查询 CDK

### 请求

```http
GET /api/cdk?code={CDK}
Accept: application/json
```

参数：

| 位置 | 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| Query | `code` | string | 是 | CDK 原文，需 URL 编码 |

### 成功响应

状态码：`200`

```json
{
  "valid": true,
  "cdk_remaining": 98,
  "expires_at": "2026-12-31T23:59:59Z"
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `valid` | boolean | 建议 | CDK 是否可用 |
| `cdk_remaining` | integer | 建议 | 剩余可用次数；未知时可为 `null` |
| `expires_at` | string/null | 可选 | CDK 过期时间，建议 ISO 8601 |
| `message` | string | 可选 | 面向用户的补充说明 |

客户端对该响应保留原始 JSON；建议服务方始终返回 `valid` 和 `cdk_remaining`，便于 WebUI 展示。

### 失败响应

状态码建议：

- `400`：缺少 `code` 或参数格式错误；
- `401`：服务认证失败（如果服务方额外启用了服务级认证）；
- `404`：CDK 不存在；
- `409`：CDK 已用尽；
- `410`：CDK 已过期。

示例：

```json
{
  "error": "CDK 已用尽",
  "code": "CDK_EXHAUSTED"
}
```

## 4. 创建提链任务

### 请求

```http
POST /api/extract
Accept: application/json
Content-Type: application/json

{
  "link_type": "upi",
  "cdk": "客户端传入的 CDK",
  "token": "ChatGPT access_token"
}
```

参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `link_type` | string | 是 | `pix`、`upi`、`kakao_pay`、`ideal` 之一 |
| `cdk` | string | 是 | 用于创建任务并扣减次数的 CDK |
| `token` | string | 是 | ChatGPT 账号 `access_token` |

服务方应在创建任务时完成以下校验：

1. 校验 `link_type` 是否支持；
2. 校验 CDK 是否有效、未过期且仍有剩余次数；
3. 校验 `token` 是否能访问 ChatGPT 目标账号；
4. 校验目标账号是否符合提链资格；
5. 成功创建任务后再扣减一次 CDK，或使用事务保证失败不会误扣。

### 成功响应

状态码：`200` 或 `202`。

```json
{
  "job_id": "job_20261019_000001",
  "status": "queued",
  "message": "提链任务已创建",
  "cdk_remaining": 97
}
```

`job_id` 是客户端继续监听任务的唯一依据，必须返回非空字符串。

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `job_id` | string | **是** | 任务唯一 ID，建议全局唯一且稳定 |
| `status` | string | 建议 | `queued` 或 `running` |
| `message` | string | 可选 | 创建结果说明 |
| `cdk_remaining` | integer/null | 建议 | 扣减后的剩余次数 |

### 创建失败响应

状态码建议：

- `400`：缺少字段、提链类型不支持；
- `401`：Token 无效或已过期；
- `403`：账号无提链权限或 CDK 无权使用该渠道；
- `404`：接口路径不存在时不应发生；CDK/账号不存在时可使用；
- `409`：重复任务、CDK 并发冲突或账号当前不可提链；
- `429`：服务并发或频率限制；
- `500`：服务内部错误。

示例：

```json
{
  "error": "access_token 已失效",
  "code": "TOKEN_INVALID"
}
```

## 5. 监听任务事件（SSE）

创建任务成功后，客户端立即监听：

```http
GET /api/jobs/{job_id}/events?cdk={CDK}
Accept: text/event-stream
Cache-Control: no-cache
```

其中 `{job_id}` 必须 URL 编码。服务端响应：

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
Connection: keep-alive
```

SSE 每个事件以空行结束。`data` 内容必须是合法 JSON；客户端按标准 SSE 解析 `event:` 和 `data:` 行。

### 5.1 日志事件

用于向 WebUI 更新进度：

```text
event: log
data: {"message":"正在校验账号资格","percent":20}

```

字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `message` | string | 是 | 当前进度文字，建议不超过 300 字符 |
| `percent` | number | 可选 | 0 到 100 的进度百分比 |
| `status` | string | 可选 | 服务方内部阶段 |

不要在日志事件中返回 Token、CDK、密码、2FA 或完整账号资料。

### 5.2 成功结果事件

必须先发送 `result`，再发送 `done`：

```text
event: result
data: {"result":{"long_url":"https://pay.example/link/abc","copy_paste":"https://pay.example/link/abc","image_url_png":"https://pay.example/qr/abc.png","image_url_svg":"https://pay.example/qr/abc.svg","payment_method":"upi","payment_link_type":"upi","expires_at":"2026-10-19T15:00:00Z","cdk_remaining":96}}

event: done
data: {"status":"success"}

```

`result` 对象字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `long_url` | string/null | 建议 | 完整提链 URL |
| `copy_paste` | string/null | 建议 | 可复制的链接文本；没有时可与 `long_url` 相同 |
| `image_url_png` | string/null | 可选 | PNG 二维码 URL |
| `image_url_svg` | string/null | 可选 | SVG 二维码 URL |
| `payment_method` | string/null | 可选 | 支付方式，如 `upi` |
| `payment_link_type` | string/null | 可选 | 实际生成的链接类型 |
| `expires_at` | string/null | 可选 | 链接过期时间，建议 ISO 8601 |
| `cdk_remaining` | integer/null | 建议 | 任务完成后的剩余 CDK 次数 |

服务方可以在 `result` 中返回额外字段，客户端会保留原始结果 JSON，但 WebUI 只直接展示上表字段。

### 5.3 失败事件

任务业务失败时发送 `error`，随后发送 `done`：

```text
event: error
data: {"error":"账号不符合当前渠道提链条件","code":"NOT_ELIGIBLE"}

event: done
data: {"status":"failed"}

```

`error` 可以是字符串，也可以是对象：

```text
event: error
data: {"error":{"message":"CDK 已用尽","code":"CDK_EXHAUSTED"}}

```

客户端会把错误转换为 `RuntimeError: <错误说明>` 并显示在账号列表。

### 5.4 心跳和断开

长任务期间建议每 15 到 30 秒发送 SSE 注释心跳，防止中间代理断开：

```text
: keep-alive

```

任务结束必须发送 `result` 或 `error`。如果连接直接结束且没有结果，客户端会将任务标记为失败，并提示“提链事件流结束但未返回 result”。

## 6. 客户端本地接口（供前端调用）

以下接口是本项目 WebUI 的接口，不是提链服务方需要实现的外部接口。它们最终由后台转发到第 3 至第 5 节的外部接口。

### 单账号入队

```http
POST /api/accounts/extract-link
Content-Type: application/json

{"account_id":123,"link_type":"upi","cdk":"可选，省略时使用服务端配置"}
```

成功返回 `202`：

```json
{
  "ok": true,
  "started": true,
  "link_type": "upi"
}
```

### 批量入队

```http
POST /api/accounts/extract-link-bulk
Content-Type: application/json

{"account_ids":[123,124],"link_type":"upi","cdk":"可选"}
```

成功返回 `202`，响应包含：

```json
{
  "ok": true,
  "started": [],
  "started_count": 0,
  "busy": [],
  "busy_count": 0,
  "failed": [],
  "failed_count": 0,
  "skipped": [],
  "skipped_count": 0
}
```

### 查询 CDK

```http
GET /api/extract-link/cdk?code=可选
```

后台会调用外部 `GET /api/cdk?code=...` 并透传其 JSON 字段。

## 7. 推荐时序

```text
客户端                  提链服务
  |                         |
  | GET /api/cdk            |
  |------------------------>|  校验 CDK
  |<------------------------|  valid / remaining
  |                         |
  | POST /api/extract       |
  |------------------------>|  校验 token、资格，创建任务并扣 CDK
  |<------------------------|  job_id
  |                         |
  | GET /api/jobs/{id}/events?cdk=...
  |------------------------>|  SSE
  |<------------------------|  log*
  |<------------------------|  result 或 error
  |<------------------------|  done
```

## 8. 实现和验收清单

服务方上线前应使用测试 CDK 和测试 Token 验证：

- `GET /api/cdk` 返回 200 JSON，并能区分有效、过期、用尽 CDK；
- `POST /api/extract` 返回 200/202，且一定包含非空 `job_id`；
- 无效 Token 返回 401/403 JSON，而不是 HTML 404 页面；
- 无效提链类型返回 400 JSON；
- 创建成功后 SSE 返回 `Content-Type: text/event-stream`；
- SSE 至少发送一个 `log`，最终发送 `result + done` 或 `error + done`；
- `result` 中至少返回 `long_url` 或 `copy_paste` 之一；
- 长任务期间支持 SSE 注释心跳；
- 接口错误消息不泄露 Token、CDK、密码或 2FA；
- API 路径必须与本文档完全一致，不能只提供 `/api/checkout`、`/api/checkout-progress` 等不同协议的路径；
- 使用反向代理时，必须关闭 SSE 缓冲并允许长连接，例如 Nginx 需正确转发 `text/event-stream`。

## 9. 当前错误的对应关系

客户端当前调用 `POST {EXTRACT_LINK_API_BASE}/api/extract`。如果服务方返回：

```json
{"error":"not found"}
```

通常表示 API 路径未实现或服务根地址配置错误，而不是 Token、CDK 或账号资格错误。服务方完成本文档中的三个外部接口后，客户端现有提链流程即可继续使用，无需客户端改动。
