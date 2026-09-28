# IoT Device Telemetry API（本地、无外部依赖）

基于 FastAPI 的物联网设备接入与遥测上报服务。**所有状态保存在进程内存中**：
不依赖数据库、消息队列或任何云服务，也不需要真实凭据；重启进程即清空，
适合本地开发与验证。

## 能力总览

- 设备注册，并在注册时签发第一张接入凭证（Bearer Token，仅展示一次明文）。
- 凭证轮换：旧凭证进入一个**明确的宽限（兼容）窗口**；窗口内新旧凭证都可用，
  超期或被后续轮换提前作废时，访问会被拒绝，且**拒绝原因可区分**。
- 遥测批量上报：逐行校验，坏数据不会被静默丢弃；同一批次内重复 / 乱序到达，
  最终落库时序一致且可预期。
- 按设备查询最新状态与历史区间；多客户端并发读写同一设备时不会出现半更新，
  被拒绝的数据绝不会计入结果。

## 快速开始

```bash
# 安装依赖（需要 Python 3.14+，使用 uv）
uv sync

# 启动服务
uv run uvicorn app.main:app --reload
# 交互式文档： http://127.0.0.1:8000/docs
```

健康检查：`GET /health` → `{"status":"ok","storage":"local-memory"}`

## 设备接入流程

### 1. 注册设备，获取凭证

```bash
curl -s -X POST http://127.0.0.1:8000/devices \
  -H 'Content-Type: application/json' \
  -d '{"name":"sensor-1","metadata":{"zone":"lab"}}'
```

响应（`token` 只在签发/轮换时返回一次，服务端只存 SHA-256 哈希）：

```json
{
  "device": {"device_id": "dev_xxx", "name": "sensor-1", "metadata": {"zone":"lab"}, "created_at": "..."},
  "credential": {
    "credential_id": "cred_xxx", "device_id": "dev_xxx",
    "token": "iotdev_...", "status": "active", "expires_at": null
  }
}
```

### 2. 使用凭证上报 / 查询

所有遥测接口都需要请求头：`Authorization: Bearer <token>`。
凭证只能访问它所属的设备；访问其他设备返回 `403 invalid_credential`。

## 凭证与兼容（轮换）规则

调用轮换接口签发新凭证，并让旧凭证进入宽限窗口：

```bash
curl -s -X POST http://127.0.0.1:8000/devices/dev_xxx/credentials/rotate \
  -H 'Authorization: Bearer <当前token>' \
  -H 'Content-Type: application/json' \
  -d '{"grace_seconds": 3600}'
```

- `new`：新生效凭证（返回明文 token）。
- `retired`：被替换的旧凭证，`status=grace`，`expires_at` 为宽限窗口结束时刻；
  其明文 token 不再返回（设备本地已持有）。
- `grace_seconds` 可设 `0`（无兼容窗口，旧凭证立即过期）到 86400。

凭证在任一时刻的状态：`active` → `grace` → `expired` / `revoked`。

**关键不变量**：同一设备**最多只有一个 `active` 凭证**，并且最多只有一个仍在
窗口内的 `grace` 凭证——绝不会有两个同时生效的凭证。

- 宽限窗口结束后仍用旧凭证 → 标记为 `expired`。
- 在窗口结束前再次轮换 → 上一个 `grace` 凭证被**立即作废为 `revoked`**
  （若它其实已过窗口则记为 `expired`），然后当前活动凭证进入新的 `grace`。

### 可区分的拒绝原因（HTTP 401，错误体 `error.code`）

| code | 含义 |
| --- | --- |
| `missing_credential` | 未提供 `Authorization` 头 / token |
| `invalid_credential` | token 无法识别（或用 A 设备凭证访问 B 设备，返回 403 同 code） |
| `credential_expired` | token 对应的旧凭证已超过轮换后的宽限窗口 |
| `credential_revoked` | token 对应的凭证在宽限窗口内被一次更新的轮换提前作废 |

## 遥测上报与坏数据规则

```bash
curl -s -X POST http://127.0.0.1:8000/devices/dev_xxx/telemetry \
  -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' \
  -d '{"entries":[
    {"ts":"2026-09-28T10:00:00Z","metrics":{"temp":20.5,"hum":55}},
    {"ts":"2026-09-28T10:01:00Z","metrics":{"temp":21.0}}
  ]}'
```

- `ts`：ISO-8601 且**必须带时区**（推荐 `Z`）。
- `metrics`：非空对象，值必须是数字（布尔值视为非法）。

逐行校验，**整批部分成功**：响应给出 `accepted` / `rejected` 计数，以及每行的
`index / reason / message`。坏数据绝不静默丢弃，也绝不落库。拒绝原因：

| reason | 触发条件 |
| --- | --- |
| `missing_field` | 条目不是对象、缺 `ts` / `metrics`、或 `metrics` 为空 |
| `type_mismatch` | `ts` 无法解析 / 不带时区 / 类型错误，或指标值非数字（含布尔） |
| `duplicate` | 时间戳已在库中，或在同一批次中重复（首条生效，后续拒绝） |
| `timestamp_regression` | 时间戳早于该设备**已落库**的最新时间戳（设备时钟倒退） |

**去重与乱序的一致性**：判定只针对已落库状态，与批次内的行顺序无关。时间戳按
`ts` 存储、读取时排序，因此打乱顺序的同一批数据（`[t2,t1]` 与 `[t1,t2]`）
最终得到完全一致的时序；同一时间戳只有第一次上报生效。

响应示例：

```json
{"device_id":"dev_xxx","accepted":1,"rejected":1,
 "rejected_items":[{"index":1,"reason":"duplicate","message":"timestamp ... already stored ..."}],
 "latest_ts":"2026-09-28T10:00:00Z"}
```

## 状态查询（均需设备凭证）

- 最新状态：`GET /devices/{id}/state/latest`
  → `latest_ts`、`metrics`、已落库点数 `count`。
- 历史区间（闭区间，按时间升序）：
  `GET /devices/{id}/state/history?start=2026-09-28T10:00:00Z&end=2026-09-28T11:00:00Z`

## 并发一致性

- 每个设备有一把可重入锁；一批数据的“校验 + 提交”在同一临界区内完成，读取
  （含 `latest` 与 `history` 的原子快照）也持同一把锁，因此：
  - 不会读到半更新状态；
  - 每个时间戳最多落库一次，无丢失 / 重复（见并发测试）；
  - 被拒绝的行在提交阶段前就被剔除，绝不会出现在查询结果里。
- 不同设备使用不同锁，互不阻塞。

## 本地验证（测试）

```bash
uv run pytest                      # 运行全部测试
uv run pytest -s                   # 同时打印每次请求的“输入 + 判定依据”日志
uv run pytest -q -k credential     # 只跑凭证轮换相关
uv run pytest -q -k concurrency    # 只跑并发读写相关
```

测试覆盖：

- **凭证轮换**：宽限窗口内可用、窗口后 `credential_expired`、窗口内再次轮换
  `credential_revoked`、四种拒绝原因可区分、任意时刻只有一个 `active`。
- **重复 / 乱序上报**：批内重复首条生效、打乱顺序收敛一致、时钟倒退被拒且不落库。
- **并发读写**：多线程写不丢不重、读始终拿到自洽快照、拒绝行不计入结果。

测试日志由 `tests/conftest.py` 配置，会把每次服务调用的请求输入（含完整
payload）与判定依据（接受/拒绝计数、逐行原因、凭证状态迁移）打印到标准输出。

## 代码结构

```
app/
  main.py          FastAPI 应用与统一错误响应
  errors.py        领域错误与稳定的机器可读 code
  models.py        请求/响应模型
  store.py         线程安全的本地内存存储（设备/凭证/时序点）
  security.py      凭证签发、轮换宽限窗口、Bearer 认证
  telemetry.py     逐条校验分类、去重、时钟回退判定、原子提交
  services.py      用例编排（注册/轮换/入库/查询）与决策日志
  dependencies.py  认证依赖
  routers/         devices、telemetry 路由
tests/             凭证、遥测、并发、边界测试
```
