import json
from collections.abc import AsyncGenerator

from fastapi import Depends, Query, Request
from redis.asyncio.client import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import RedisInitKeyConfig
from app.config.setting import settings
from app.core.auth.schema import AuthSchema
from app.core.database import async_db_session
from app.core.exceptions import CustomException
from app.core.logger import log
from app.core.plugin.slots import SLOT_AUTH_USER_RESOLVER, get
from app.core.redis_crud import RedisCURD
from app.core.security import OAuth2Schema, decode_access_token


async def db_getter() -> AsyncGenerator[AsyncSession]:
    """获取数据库会话连接

    返回:
    - AsyncSession: 数据库会话连接
    """
    async with async_db_session() as session:
        async with session.begin():
            yield session


async def redis_getter(request: Request) -> Redis:
    """获取Redis连接

    参数:
    - request (Request): 请求对象

    返回:
    - Redis: Redis连接
    """
    return request.app.state.redis


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(db_getter),
    redis: Redis = Depends(redis_getter),
    token: str = Depends(OAuth2Schema),
) -> AuthSchema:
    """获取当前用户

    参数:
    - request (Request): 请求对象
    - db (AsyncSession): 数据库会话
    - redis (Redis): Redis连接
    - token (str): 访问令牌

    返回:
    - AuthSchema: 认证信息模型
    """
    return await _verify_token(token, db, redis, request=request)


async def get_current_user_ws(
    token: str = Query(..., description="认证token"),
    db: AsyncSession = Depends(db_getter),
    redis: Redis = Depends(redis_getter),
) -> AuthSchema:
    """获取当前用户（WebSocket专用，从查询参数获取token）

    参数:
    - token (str): 认证token
    - db (AsyncSession): 数据库会话
    - redis (Redis): Redis连接

    返回:
    - AuthSchema: 认证信息模型
    """
    return await _verify_token(token, db, redis)


async def _verify_token(
    token: str,
    db: AsyncSession,
    redis: Redis,
    request: Request | None = None,
) -> AuthSchema:
    """校验服务端留存凭据及其身份绑定，供 HTTP 和 WebSocket 共用。"""
    if not token:
        raise CustomException(msg="认证已失效", code=10401, status_code=401)
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    payload = decode_access_token(token)
    if payload.is_refresh:
        raise CustomException(msg="非法凭证", code=10401, status_code=401)
    try:
        user_info = json.loads(payload.sub)
        if not isinstance(user_info, dict):
            raise ValueError("invalid session")
        session_id = user_info.get("session_id")
        username = user_info.get("user_name")
        user_id = user_info.get("user_id")
        if (
            not isinstance(session_id, str)
            or not session_id
            or not isinstance(username, str)
            or not username
            or type(user_id) is not int
            or user_id <= 0
        ):
            raise ValueError("invalid identity")
    except ValueError, TypeError:
        raise CustomException(msg="非法凭证", code=10401, status_code=401) from None

    # 键存在不代表凭据合法：禁止使用自己的 session_id 冒用其他身份。
    access_key = f"{RedisInitKeyConfig.ACCESS_TOKEN.key}:{session_id}"
    stored = await RedisCURD(redis).get(access_key)
    if isinstance(stored, bytes):
        stored = stored.decode("utf-8")
    if not stored or stored != token:
        raise CustomException(msg="认证已失效", code=10401, status_code=401)

    resolver = get(SLOT_AUTH_USER_RESOLVER)
    if resolver is None:
        log.warning("用户解析槽位 auth.user_resolver 未提供，无法认证")
        raise CustomException(msg="认证已失效", code=10401, status_code=401)
    user = await resolver.resolve(db, username)
    if not user or user.id != user_id or user.username != username:
        raise CustomException(msg="用户不存在", code=10401, status_code=401)
    if user.status != "0":
        raise CustomException(msg="用户已被停用", code=10401, status_code=401)

    if settings.TOKEN_SLIDING_EXPIRE:
        await RedisCURD(redis).expire(access_key, expire=settings.ACCESS_TOKEN_EXPIRE_SECONDS)
        await RedisCURD(redis).expire(
            f"{RedisInitKeyConfig.REFRESH_TOKEN.key}:{session_id}",
            expire=settings.REFRESH_TOKEN_EXPIRE_SECONDS,
        )
    if request is not None:
        request.scope["user_id"] = user.id
        request.scope["user_username"] = user.username
        request.scope["user_mobile"] = user.mobile
        request.scope["login_type"] = user_info.get("login_type")
    if hasattr(user, "roles"):
        user.roles = [role for role in user.roles if role and role.status == "0"]
    if hasattr(user, "positions"):
        user.positions = [pos for pos in user.positions if pos and pos.status == "0"]
    return AuthSchema(db=db, user=user, check_data_scope=False)
