"""认证、授权、上传、回跳与日志边界的隔离回归验证，不连接外部数据库。"""

from __future__ import annotations

import asyncio
import importlib
import json
import secrets
import socket
import threading
import time
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import fakeredis.aioredis
import httpx
import pytest
import uvicorn
from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.common.enums import EnvironmentEnum
from app.config.setting import Settings, settings
from app.core import dependencies
from app.core.auth.schema import AuthSchema, JWTPayloadSchema
from app.core.base_model import MappedBase
from app.core.dependencies import db_getter
from app.core.exceptions import CustomException
from app.core.router_class import OperationLogRoute, sanitize_log_data
from app.core.security import CustomOAuth2PasswordRequestForm, create_access_token
from app.plugin.module_common.file.service import FileService
from app.plugin.module_system.auth.controller import AuthRouter
from app.plugin.module_system.auth.oauth_service import (
    complete_oauth_login,
    exchange_oauth_code,
    oauth_service_frontend_redirect_from_token,
    save_oauth_state,
    validate_frontend_redirect,
)
from app.plugin.module_system.auth.schema import JWTOutSchema
from app.plugin.module_system.auth.service import AutoLoginService, LoginService
from app.plugin.module_system.user.controller import UserRouter
from app.plugin.module_system.user.model import UserModel
from app.plugin.module_system.user.schema import (
    ResetPasswordSchema,
    UserForgetPasswordSchema,
    UserRegisterSchema,
)
from app.plugin.module_system.user.service import UserService
from app.utils.sm_crypto_util import PwdUtil
from app.utils.upload_util import UploadUtil


def _request(headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers or []})


async def _exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, CustomException)
    return JSONResponse({"msg": exc.msg}, status_code=exc.status_code)


@pytest.fixture
def security_client(monkeypatch: pytest.MonkeyPatch):
    """挂载真实控制器，禁用日志落库，并使用不绑定数据库的会话。"""
    monkeypatch.setattr(settings, "OPERATION_LOG_RECORD", False)
    app = FastAPI()
    app.add_exception_handler(CustomException, _exception_handler)
    app.include_router(AuthRouter, prefix="/system")
    app.include_router(UserRouter, prefix="/system")

    async def db_override():
        async with AsyncSession() as db:
            yield db

    app.dependency_overrides[db_getter] = db_override
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/system/auth/auto-login/users"),
        ("POST", "/system/auth/auto-login/token?user_id=1"),
        ("POST", "/system/auth/auto-login?token=legacy-token"),
        ("POST", "/system/user/forget/password"),
    ],
)
def test_unsafe_public_routes_fail_closed(security_client, method: str, path: str) -> None:
    response = security_client.request(
        method, path, json={"username": "victim", "new_password": "changed123"}
    )
    assert response.status_code == 403
    assert "access_token" not in response.text


def test_unsafe_services_cannot_be_called_internally() -> None:
    async def run() -> None:
        async with AsyncSession() as db:
            redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
            calls = [
                AutoLoginService.get_auto_login_users_service(db),
                AutoLoginService.create_auto_login_token_service(redis, db, 1),
                AutoLoginService.auto_login_service(_request(), redis, db, "old-token"),
                UserService.forget_password_service(
                    AuthSchema(db=db),
                    UserForgetPasswordSchema(username="victim", new_password="changed123"),
                ),
            ]
            for call in calls:
                with pytest.raises(CustomException) as caught:
                    await call
                assert caught.value.status_code == 403
            assert await redis.dbsize() == 0
            await redis.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("extra", [{"role_ids": [1]}, {"created_id": 1}, {"is_superuser": True}])
def test_registration_rejects_privilege_fields(extra: dict) -> None:
    with pytest.raises(ValidationError):
        UserRegisterSchema.model_validate({"username": "newuser", "password": "secret123", **extra})


def test_registration_and_reset_obey_server_roles_and_data_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """使用真实 ORM：注册不获角色，专门权限与行过滤均须生效。"""
    from app.core import permission
    from app.plugin.module_system.dept.model import DeptModel
    from app.plugin.module_system.plugin import MODEL_PATHS

    for module in MODEL_PATHS:
        importlib.import_module(f"app.plugin.module_system.{module}")
    monkeypatch.setattr(settings, "REGISTER_DEFAULT_ROLE_IDS", [])
    monkeypatch.setattr(
        permission, "get", lambda _slot: {"user_model": UserModel, "dept_model": DeptModel}
    )

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(MappedBase.metadata.create_all)
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                created = await UserService.register_user_service(
                    AuthSchema(db=db), UserRegisterSchema(username="newuser", password="secret123")
                )
                fresh = await db.get(UserModel, created["id"])
                assert fresh is not None and not fresh.is_superuser and fresh.created_id is None
                assert not fresh.roles
                db.add_all([
                    UserModel(
                        id=20, username="in_scope", name="inside", password="old", created_id=10
                    ),
                    UserModel(
                        id=21, username="out_scope", name="outside", password="old", created_id=99
                    ),
                ])
                await db.flush()
                role = SimpleNamespace(status="0", menus=[], data_scope=1)
                operator = SimpleNamespace(id=10, is_superuser=False, roles=[role], dept_id=None)
                auth = AuthSchema(db=db, user=operator, check_data_scope=False)
                data = ResetPasswordSchema(id=20, password="new-secret123")
                with pytest.raises(CustomException) as caught:
                    await UserService.reset_user_password_service(auth, data)
                assert caught.value.status_code == 403
                role.menus = [SimpleNamespace(permission="module_system:user:reset", status="0")]
                with pytest.raises(CustomException):
                    await UserService.reset_user_password_service(
                        auth, ResetPasswordSchema(id=21, password="hacked123")
                    )
                result = await UserService.reset_user_password_service(auth, data)
                assert result["id"] == 20 and auth.check_data_scope is True
                outside = await db.get(UserModel, 21)
                assert outside is not None and outside.password == "old"
                inside = await db.get(UserModel, 20)
                assert inside is not None and PwdUtil.verify_password(
                    "new-secret123", inside.password
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["http", "websocket"])
def test_session_rejects_forged_identity_and_revoked_tokens(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    resolver = SimpleNamespace(
        resolve=AsyncMock(
            return_value=SimpleNamespace(
                id=7, username="ordinary", mobile=None, status="0", roles=[], positions=[]
            )
        )
    )
    monkeypatch.setattr(dependencies, "get", lambda _slot: resolver)

    def token(user_id: int = 7, username: str = "ordinary", seconds: int = 1800) -> str:
        return create_access_token(
            JWTPayloadSchema(
                sub=json.dumps({
                    "session_id": "own-session",
                    "user_id": user_id,
                    "user_name": username,
                }),
                exp=datetime.now() + timedelta(seconds=seconds),
            )
        )

    async def run() -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        async with AsyncSession() as db:

            async def authenticate(submitted: str):
                if mode == "http":
                    return await dependencies.get_current_user(_request(), db, redis, submitted)
                return await dependencies.get_current_user_ws(submitted, db, redis)

            valid = token()
            await redis.set("access_token:own-session", valid)
            authenticated = await authenticate(valid)
            assert authenticated.user is not None and authenticated.user.id == 7
            for forged in (token(1, "admin"), token(seconds=900)):
                with pytest.raises(CustomException) as caught:
                    await authenticate(forged)
                assert caught.value.status_code == 401
            await redis.delete("access_token:own-session")
            with pytest.raises(CustomException):
                await authenticate(valid)
        await redis.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "secret",
    ["change-me-to-a-secure-random-key", "a" * 64, "short", "your-prod-secret-key-change-me"],
)
def test_production_refuses_weak_signing_keys(secret: str) -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(ENVIRONMENT=EnvironmentEnum.PROD, SECRET_KEY=secret)


def test_production_accepts_random_key() -> None:
    assert (
        Settings(ENVIRONMENT=EnvironmentEnum.PROD, SECRET_KEY=secrets.token_hex(32)).ENVIRONMENT
        == "prod"
    )


@pytest.mark.parametrize(
    "redirect",
    [
        "https://evil.example/login",
        "https://app.example/login.evil",
        "https://app.example.evil/login",
        "https://app.example@evil.example/login",
        "//evil.example/login",
        "javascript:alert(1)",
        "https://app.example/login?next=https://evil.example",
        "https://app.example/login#evil",
    ],
)
def test_oauth_redirect_requires_exact_allowlist(
    monkeypatch: pytest.MonkeyPatch, redirect: str
) -> None:
    monkeypatch.setattr(settings, "OAUTH_REDIRECT_ALLOWLIST", ["https://app.example/login"])
    with pytest.raises(CustomException):
        validate_frontend_redirect(redirect)


def test_oauth_codes_are_bound_expiring_and_single_use(monkeypatch: pytest.MonkeyPatch) -> None:
    frontend = "https://app.example/login"
    monkeypatch.setattr(settings, "OAUTH_REDIRECT_ALLOWLIST", [frontend])

    async def run() -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        token = JWTOutSchema(
            access_token="secret-access", refresh_token="secret-refresh", expires_in=1800
        )
        url = await oauth_service_frontend_redirect_from_token(
            frontend, token, redis=redis, browser_nonce="browser"
        )
        assert "secret-access" not in url and "secret-refresh" not in url
        code = parse_qs(urlsplit(url).query)["oauth_code"][0]
        assert 0 < await redis.ttl(f"oauth_code:{code}") <= 60
        assert await exchange_oauth_code(redis=redis, code=code, browser_nonce="browser") == token
        with pytest.raises(CustomException):
            await exchange_oauth_code(redis=redis, code=code, browser_nonce="browser")
        url = await oauth_service_frontend_redirect_from_token(
            frontend, token, redis=redis, browser_nonce="browser"
        )
        code = parse_qs(urlsplit(url).query)["oauth_code"][0]
        with pytest.raises(CustomException):
            await exchange_oauth_code(redis=redis, code=code, browser_nonce="other-browser")
        url = await oauth_service_frontend_redirect_from_token(
            frontend, token, redis=redis, browser_nonce="browser"
        )
        code = parse_qs(urlsplit(url).query)["oauth_code"][0]
        await redis.delete(f"oauth_code:{code}")
        with pytest.raises(CustomException):
            await exchange_oauth_code(redis=redis, code=code, browser_nonce="browser")
        await save_oauth_state(
            redis=redis,
            state="state",
            provider="github",
            frontend_redirect=frontend,
            browser_nonce="browser",
        )
        async with AsyncSession() as db:
            with pytest.raises(CustomException, match="浏览器"):
                await complete_oauth_login(
                    request=_request(),
                    redis=redis,
                    db=db,
                    provider="github",
                    code="code",
                    state="state",
                )
        await redis.aclose()

    asyncio.run(run())


def test_avatar_rejects_svg_and_reencodes_bitmap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "UPLOAD_FILE_PATH", tmp_path)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    with pytest.raises(CustomException):
        UploadUtil.validate_file_content_type(svg, ".svg")
    with pytest.raises(CustomException):
        UploadUtil.validate_file_content_type(svg, ".png")
    image = BytesIO()
    Image.new("RGB", (10, 10), "red").save(image, format="PNG")
    original = image.getvalue() + b"<script>secret-payload</script>"

    async def run() -> None:
        file = UploadFile(filename="avatar.png", file=BytesIO(original))
        _, path, _ = await UploadUtil.upload_avatar(file, "https://app.example/")
        assert b"secret-payload" not in path.read_bytes()
        with Image.open(path) as saved:
            saved.verify()
        await file.close()

    asyncio.run(run())


def test_download_rejects_sibling_and_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "upload"
    root.mkdir()
    sibling = tmp_path / "upload_backup"
    sibling.mkdir()
    outside = sibling / "private.txt"
    outside.write_text("private", encoding="utf-8")
    monkeypatch.setattr(settings, "UPLOAD_FILE_PATH", root)
    with pytest.raises(CustomException):
        FileService._validate_download_path(str(outside))
    inside = root / "allowed.txt"
    inside.write_text("allowed", encoding="utf-8")
    assert FileService._validate_download_path(str(inside)) == str(inside.resolve())
    try:
        (root / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("当前 Windows 权限不允许创建符号链接")
    with pytest.raises(CustomException):
        FileService._validate_download_path(str(root / "link.txt"))


def test_log_redaction_covers_nested_and_scalar_secrets() -> None:
    value = {
        "body": {"username": "user", "password": "plain-secret", "code": "auth-code"},
        "data": {
            "items": [{"refresh_token": "refresh-secret", "id": 1}],
            "access_token": "access-secret",
        },
    }
    rendered = json.dumps(sanitize_log_data(value))
    assert all(
        secret not in rendered
        for secret in ("plain-secret", "auth-code", "refresh-secret", "access-secret")
    )
    assert "user" in rendered
    assert sanitize_log_data({"data": "scalar-secret"})["data"] == "[REDACTED]"


@pytest.mark.parametrize("referer", [b"https://evil.example/docs", b"https://evil.example/redoc"])
def test_captcha_cannot_be_skipped_by_referer(
    monkeypatch: pytest.MonkeyPatch, referer: bytes
) -> None:
    monkeypatch.setattr(settings, "CAPTCHA_ENABLE", True)

    async def run() -> None:
        async with AsyncSession() as db:
            redis = fakeredis.aioredis.FakeRedis()
            with pytest.raises(CustomException, match="验证码"):
                await LoginService.authenticate_user_service(
                    _request([(b"referer", referer)]),
                    redis,
                    CustomOAuth2PasswordRequestForm(
                        username="admin",
                        password="unused",
                        scope="",
                        captcha_key=None,
                        captcha=None,
                    ),
                    db,
                )
            await redis.aclose()

    asyncio.run(run())


def test_operation_log_redacts_form_and_response_even_with_docs_referer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager

    from app.core import router_class

    entries = []

    async def write(entry, *_args):
        entries.append(entry)

    @asynccontextmanager
    async def session():
        async with AsyncSession() as db:
            yield db

    monkeypatch.setattr(settings, "OPERATION_LOG_RECORD", True)
    monkeypatch.setattr(settings, "OPERATION_LOG_SIGN_ENABLE", False)
    monkeypatch.setattr(router_class, "get", lambda _slot: SimpleNamespace(write=write))
    monkeypatch.setattr(router_class, "async_db_session", session)
    monkeypatch.setattr(
        router_class.IpLocalUtil, "resolve_location_for_log", AsyncMock(return_value="local")
    )
    app = FastAPI()
    app.router.route_class = OperationLogRoute

    @app.post("/login")
    async def login(request: Request):
        await request.form()
        return {"data": {"access_token": "access-secret", "refresh_token": "refresh-secret"}}

    with TestClient(app) as client:
        response = client.post(
            "/login",
            data={"username": "user", "password": "plain-secret"},
            headers={"referer": "https://evil.example/docs"},
        )
        assert response.status_code == 200
    assert len(entries) == 1
    persisted = entries[0].request_payload + entries[0].response_json
    assert all(
        secret not in persisted for secret in ("plain-secret", "access-secret", "refresh-secret")
    )


def test_real_http_server_security_and_file_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """启动生产应用工厂的真实 TCP 服务；隔离外部设施，验证真实路由与文件响应。"""
    from app.core.dependencies import get_current_user, redis_getter
    from main import create_app

    monkeypatch.setattr(settings, "OPERATION_LOG_RECORD", False)
    monkeypatch.setattr(settings, "CAPTCHA_ENABLE", True)
    monkeypatch.setattr(settings, "OAUTH_REDIRECT_ALLOWLIST", ["https://app.example/login"])
    upload = tmp_path / "upload"
    upload.mkdir()
    monkeypatch.setattr(settings, "UPLOAD_FILE_PATH", upload)
    allowed = upload / "report.txt"
    allowed.write_text("http-download-ok", encoding="utf-8")
    outside = tmp_path / "upload_backup"
    outside.mkdir()
    (outside / "private.txt").write_text("private", encoding="utf-8")
    app = create_app()

    async def db_override():
        async with AsyncSession() as db:
            yield db

    async def local_user(request: Request):
        if request.headers.get("authorization") != "Bearer isolated-http-test":
            raise CustomException(msg="请先登录", status_code=401)
        return AuthSchema(db=AsyncSession(), user=SimpleNamespace(is_superuser=True, id=10))

    async def redis_override():
        return fakeredis.aioredis.FakeRedis(decode_responses=True)

    app.dependency_overrides[db_getter] = db_override
    app.dependency_overrides[get_current_user] = local_user
    app.dependency_overrides[redis_getter] = redis_override
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, lifespan="off", log_level="error", access_log=False)
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}/api/v1", timeout=10, trust_env=False
            ) as client:
                for path in (
                    "/system/auth/auto-login/token?user_id=1",
                    "/system/auth/auto-login?token=old",
                    "/system/user/forget/password",
                ):
                    assert client.post(path).status_code == 403
                assert client.get("/system/auth/auto-login/users").status_code == 403
                assert (
                    client.get(
                        "/system/auth/oauth/github/login",
                        params={"redirect_uri": "https://evil.example/login"},
                    ).status_code
                    == 403
                )
                rejected = client.post(
                    "/system/user/register",
                    json={"username": "intruder", "password": "private-password", "role_ids": [1]},
                )
                assert rejected.status_code == 422 and "private-password" not in rejected.text
                login = client.post(
                    "/system/auth/login",
                    data={"username": "admin", "password": "unused"},
                    headers={"referer": "https://evil.example/docs"},
                )
                assert login.status_code != 200 and "验证码" in login.text
                denied = client.put(
                    "/system/user/reset/password", json={"id": 20, "password": "new-secret"}
                )
                assert denied.status_code == 401
                headers = {"authorization": "Bearer isolated-http-test"}
                downloaded = client.post(
                    "/common/file/download", json={"file_path": str(allowed)}, headers=headers
                )
                assert downloaded.status_code == 200 and downloaded.text == "http-download-ok"
                assert "attachment" in downloaded.headers["content-disposition"]
                denied = client.post(
                    "/common/file/download",
                    json={"file_path": str(outside / "private.txt")},
                    headers=headers,
                )
                assert denied.status_code != 200 and "private" not in denied.text
                avatar = client.post(
                    "/system/user/current/avatar/upload",
                    files={
                        "file": (
                            "avatar.svg",
                            b"<svg><script>alert(1)</script></svg>",
                            "image/svg+xml",
                        )
                    },
                    headers=headers,
                )
                assert avatar.status_code != 200
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
