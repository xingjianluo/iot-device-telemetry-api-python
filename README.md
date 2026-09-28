# IoT Device Telemetry API

基于 FastAPI 的物联网设备接入与遥测上报服务。**全部状态保存在本地内存中**，
不依赖数据库、消息队列或任何外部服务，也不需要真实凭据；服务重启后数据清空，
适合本地开发、演示与测试。

## 1. 能力概览

- 设备注册，注册即签发接入凭证（随机 256-bit secret）。
- 凭证轮换：旧凭证有明确的宽限窗口（grace window），窗口内仍可接入；
  超期 / 被撤销 / 错误 / 缺失等拒绝原因彼此可区分。
- 遥测批量上报：逐条返回判定结果，坏数据不会被静默丢弃。
- 按设备查询最新状态、按时间区间查询历史。
- 每设备一把锁，多客户端并发读写同一设备时不会出现半更新状态。

## 2. 本地运行

需要 Python ≥ 3.14，依赖通过 [uv](https://docs.astral.sh/uv/) 管理：

```bash
uv sync                       # 安装依赖
uv run python main.py         # 启动服务（默认 127.0.0.1:8000）
# 或：uv run uvicorn main:app --reload
```

启动后可访问：

- OpenAPI / Swagger：<http://127.0.0.1:8000/docs>
- 健康检查：`GET /health`

## 3. 设备接入流程

### 3.1 注册设备并拿到凭证

```bash
curl -s -X POST http://127.0.0.1:8000/devices \
  -H 'Content-Type: application/json' \
  -d '{"device_id": "sensor-001"}'
```

响应（`secret` 只在签发/轮换时完整返回一次，请妥善保存）：

```json
{
  "device_id": "sensor-001",
  "secret": "c21f27e9...（64 位十六进制）",
  "status": "active",
  "issued_at": 1790552365.0,
  "grace_expires_at": null,
  "grace_seconds": null
}
```

也可以不传 `device_id`，由服务端生成 `dev-xxxxxxxx` 形式的 ID。
设备 ID 重复返回 `409 device_already_exists`。

### 3.2 携带凭证访问

所有设备相关接口都通过请求头携带凭证：

```
X-Device-Secret: <注册或轮换时返回的 secret>
```

### 3.3 轮换凭证（含宽限窗口）

```bash
curl -s -X POST http://127.0.0.1:8000/devices/sensor-001/credentials/rotate \
  -H "X-Device-Secret: <当前有效 secret>" \
  -H 'Content-Type: application/json' \
  -d '{"grace_seconds": 600}'
```

返回新的 active secret，以及旧凭证的 `grace_expires_at`。

### 3.4 查询凭证状态

```bash
curl -s http://127.0.0.1:8000/devices/sensor-001/credentials \
  -H "X-Device-Secret: <secret>"
```

只返回 secret 的前 8 位、状态与时间，不返回完整 secret。

## 4. 凭证状态与兼容规则

一台设备的凭证有四种状态：

| 状态 | 含义 | 能否访问 |
| --- | --- | --- |
| `active` | 当前生效凭证 | 可以 |
| `grace` | 轮换后处于宽限窗口内的旧凭证 | 可以 |
| `expired` | 宽限窗口已结束 | 不可以，`401 credential_expired` |
| `revoked` | 已被后续轮换作废 | 不可以，`401 credential_revoked` |

规则：

1. **同一设备任何时刻最多只有一个 `active` 凭证**，至多一个 `grace` 凭证。
2. 轮换后旧凭证进入 `grace`，在 `grace_seconds`（默认 600 秒）内新旧凭证都能用，
   便于设备侧滚动升级。窗口结束后旧凭证变 `expired`。
3. 在宽限期未结束时再次轮换，上一个 `grace` 凭证立即变 `revoked`，立刻失效。
4. `grace_seconds=0` 表示旧凭证轮换后立即失效。
5. 认证失败原因严格可区分，HTTP 状态均为 401：

   | 错误码 `error.code` | 触发条件 |
   | --- | --- |
   | `missing_credential` | 没带 `X-Device-Secret` 请求头 |
   | `bad_credential` | 设备存在但 secret 不匹配（含其他设备的 secret） |
   | `credential_expired` | secret 正确，但宽限窗口已过 |
   | `credential_revoked` | secret 正确，但已被更新的轮换作废 |

   设备不存在返回 `404 device_not_found`。secret 比对使用常量时间比较。

## 5. 遥测数据上报

```bash
curl -s -X POST http://127.0.0.1:8000/devices/sensor-001/telemetry \
  -H "X-Device-Secret: <secret>" \
  -H 'Content-Type: application/json' \
  -d '{"items": [
    {"timestamp": 1700000000, "metric": "temperature", "value": 21.5},
    {"timestamp": 1700000010, "metric": "humidity", "value": 0.62}
  ]}'
```

- `timestamp`：数值型 epoch 秒（整数或浮点），必须是有限数；
  字符串、布尔值、`NaN`/`Infinity` 都不合法。
- `metric`：非空字符串。
- `value`：有限数值（布尔值不算数值）。

### 5.1 逐条判定结果

每条输入对应 `results[]` 中一项：

| outcome | reason | 含义 |
| --- | --- | --- |
| `accepted` | `accepted` | 通过校验并落库（或更新） |
| `duplicate` | `duplicate_same_value` | 与本批次/历史中 `(timestamp, metric)` 完全相同且值相同，幂等跳过，不重复落库 |
| `rejected` | `missing_field:<字段名>` | 缺少 `timestamp` / `metric` / `value` |
| `rejected` | `type_mismatch:<字段名>` | 字段类型不匹配 |
| `rejected` | `invalid_timestamp` | timestamp 是数值但非有限值（如 `NaN`） |
| `rejected` | `timestamp_regression` | timestamp 早于该设备此前已落库的最新时间戳（时间戳倒退） |
| `rejected` | `duplicate_conflict` | 同一 `(timestamp, metric)` 已存在但值不同（含同批次内冲突），**冲突双方都不落库/不覆盖** |

被拒绝的数据不会出现在最新状态和历史查询中。

### 5.2 乱序与重复的一致性语义

- 时序数据以 `(timestamp, metric)` 为唯一键。
- 同一批次内先按键分组判定：同键同值保留一条、其余记幂等重复；
  同键不同值则该键全部记 `duplicate_conflict`。
- 批次内剩余条目按 `(timestamp, metric)` 排序后统一应用，因此
  **同一批数据无论以什么顺序到达，最终落库状态完全一致、可预期**。
- 时间戳倒退只相对于"此前批次已落库的水位"判定；批内自身乱序不互判倒退。
- 历史重复上报同值是幂等的；对已有点上报不同值会被拒绝而不是静默覆盖。

## 6. 状态查询

最新状态（最新时间戳下每个指标的值）：

```bash
curl -s "http://127.0.0.1:8000/devices/sensor-001/state" \
  -H "X-Device-Secret: <secret>"
```

历史区间（`start`、`end` 均为 epoch 秒，**闭区间**，结果按 timestamp、metric 排序）：

```bash
curl -s "http://127.0.0.1:8000/devices/sensor-001/history?start=1700000000&end=1700000100" \
  -H "X-Device-Secret: <secret>"
```

## 7. 并发与一致性

- 全局注册表锁只保护"设备注册表"的创建/查找。
- 每台设备有独立的 `RLock`：凭证轮换、遥测落库、状态读取都在该设备锁内进行，
  读到的状态要么是更新前、要么是更新后，不存在半更新。
- 遥测的"校验 → 批内分组 → 锁内比较并一次性应用"流程保证并发写入下：
  - 同一唯一键最多只有一个值落库；
  - 最新水位与已存点永远自洽；
  - 被拒绝（冲突/倒退）的数据不污染任何查询结果。

## 8. 错误响应格式

所有业务错误统一为：

```json
{"error": {"code": "credential_expired", "message": "...", "details": {...}}}
```

## 9. 本地验证（测试）

```bash
uv run pytest                 # 全量测试（默认实时打印请求输入与判定依据日志）
uv run pytest -q              # 安静模式
```

测试覆盖：

- `tests/test_credentials.py`：签发、轮换、宽限期生效/到期、二次轮换撤销、
  四类可区分的 401 拒绝原因。
- `tests/test_telemetry.py`：各类坏数据拒绝原因、批内/跨批次重复、
  同键冲突、时间戳倒退、乱序到达的最终状态一致性、区间查询。
- `tests/test_concurrency.py`：多线程并发写同一设备、并发读写下的不变量、
  同键并发写不同值时只有一个值落库。

测试日志会输出每条上报请求的原始输入（`ingest start ... items=...`）
以及逐条判定依据（`ingest done ... results=...`）和认证判定（`auth allow/deny ...`）。

## 10. 目录结构

```
iot/
  clock.py        # 可注入时钟（测试用 FakeClock 控制宽限时间）
  errors.py       # 错误码与异常类型
  models.py       # 领域模型与请求/响应 schema
  storage.py      # 内存存储 + 全局/每设备锁
  security.py     # 凭证签发、轮换、状态判定
  service.py      # 设备/遥测业务逻辑与逐条规则
  api/            # FastAPI 路由与依赖
  app.py          # 应用工厂与统一错误处理
tests/            # pytest 测试
main.py           # 入口
```
