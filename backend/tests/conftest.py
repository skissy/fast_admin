"""
测试配置与共享 fixtures。

注意：本文件在 import 阶段设置 TESTING 环境变量与 SQLite，
必须在其他应用模块导入之前执行。pytest 会自动优先加载 conftest.py。
"""

import os

# ── 在导入任何应用模块前设置测试环境 ────────────────────
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("ENVIRONMENT", "dev")
os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLE", "False")
os.environ.setdefault("CAPTCHA_ENABLE", "False")
os.environ.setdefault("SQL_DB_ENABLE", "True")
os.environ.setdefault("CORS_ORIGIN_ENABLE", "False")
os.environ.setdefault("LOGGER_LEVEL", "ERROR")

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from apscheduler.jobstores.memory import MemoryJobStore
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def test_client() -> Iterator[TestClient]:
    """
    创建 FastAPI TestClient 实例。

    ``REDIS_ENABLE=False`` 时应用启动不依赖 Redis：限流器降级为 InMemoryBucket，
    中间件系统配置读取静默跳过（演示模式/IP 黑白名单按默认值执行）。

    返回:
    - Iterator[TestClient]: 用于发送测试请求的客户端。
    """
    from app.core.ap_scheduler import scheduler
    from main import create_app

    app = create_app()
    # APScheduler 仍注册了 redis jobstore；仅在隔离测试中替换，防止启动 fixture
    # 在 REDIS_ENABLE=False 时意外连接真实 Redis，并阻塞整个事件循环。
    with patch.dict(scheduler._jobstores, {"redis": MemoryJobStore()}):
        with TestClient(app) as client:
            yield client
