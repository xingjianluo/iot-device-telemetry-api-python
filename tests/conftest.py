"""pytest 公共夹具与日志配置。

单测日志中会打印请求输入与判定依据（iot.service 的 INFO 日志，
以及每个测试用例记录的 request/response 摘要）。
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from iot.app import create_app
from iot.clock import FakeClock


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(start=1_000_000.0)


@pytest.fixture
def client(fake_clock: FakeClock) -> TestClient:
    app = create_app(clock=fake_clock)
    return TestClient(app)


@pytest.fixture
def registered(client: TestClient):
    """注册一台设备，返回 (device_id, secret)。"""
    resp = client.post("/devices", json={"device_id": "dev-1"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["device_id"], body["secret"]


def auth(secret: str) -> dict[str, str]:
    return {"X-Device-Secret": secret}


def log_exchange(caplog, name: str, request: object, response: object) -> None:
    """在测试日志中打印请求输入与响应判定，便于排查。"""
    logging.getLogger("tests").info(
        "case=%s request=%s -> status=%s body=%s",
        name, request, getattr(response, "status_code", None),
        getattr(response, "json", lambda: None)(),
    )


# 让服务层 INFO 日志在 pytest 中可见
@pytest.fixture(autouse=True)
def _service_logs(caplog):
    caplog.set_level(logging.INFO, logger="iot.service")
    caplog.set_level(logging.INFO, logger="tests")
    yield
