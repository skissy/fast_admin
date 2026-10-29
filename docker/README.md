# Docker 部署说明

支持 A 全套服务和 B 仅后端两种模式。后端可本地构建、从 GitHub GHCR 拉取或离线加载。
GitHub Actions 配置、镜像标签及部署命令见 [CI/CD 说明](../docs/ci-cd.md)。

## 容器名称

| 编排 | Compose 服务 | 容器名 |
| --- | --- | --- |
| 全套 | `mysql` | `fast-mysql` |
| 全套 | `redis` | `fast-redis` |
| 两种模式 | `backend` | `fast-backend` |
| 全套 | `nginx` | `fast-nginx` |
| 全套，manual profile | `certbot` | `fast-certbot` |
| B 可选依赖 | `mysql` | `fast-backend-mysql` |
| B 可选依赖 | `redis` | `fast-backend-redis` |

服务名保持不变，命令仍使用 `docker compose logs backend`，容器内 DNS 地址仍为
`mysql`、`redis`、`backend`。直接操作容器时使用 `docker logs fast-backend` 等新名称。
两种模式共用后端名称，不要同时启动。

升级旧栈时，在原目录、使用原来的 Compose project name 执行 `up -d`，Compose 会按新配置
重建容器；数据卷名称保持原样，不需要删除数据卷。

## 配置文件与端口

- `docker-compose.yaml`：A 全套 MySQL、Redis、后端、Nginx，Certbot 按需运行。
- `docker-compose.dev.yaml`：A 开发覆盖，挂载 `../backend:/home`。
- `docker-compose.backend.yaml`：B 仅后端，连接已有数据库和 Redis。
- `docker-compose.local-deps.yaml`：B 可选 MySQL/Redis。
- `.env.example`：复制成编排目录的 `.env`，设置镜像、标签、端口和依赖密码。

应用配置通过 `env_file` 注入：A 使用 `backend/env/.env.prod`；B 使用编排目录的 `env.prod`。
文件缺失会直接报错，不使用单文件 bind mount。优先级是 Compose `environment` > `env_file`
> 应用 `.env` > 默认值。生产必须填写随机 `SECRET_KEY` 并保持验证码开启，见
[安全升级说明](../backend/docs/安全升级说明.md)。

后端容器内端口固定 `6100`，宿主机映射由 `BACKEND_PORT` 决定，且只绑定 `127.0.0.1`。
MySQL/Redis 全套编排的端口也只绑定回环；公网通过 Nginx 的 80/443 提供服务。

## A：全套服务

### 本地构建

```bash
cd docker
cp .env.example .env
cp ../backend/env/.env.prod.example ../backend/env/.env.prod
# 填写数据库、Redis 密码和随机 SECRET_KEY
# BACKEND_IMAGE=fast_admin-backend，IMAGE_TAG 保持本地版本标签
bash ../deploy.sh
bash ../deploy.sh status
bash ../deploy.sh logs backend
```

脚本构建镜像并管理证书，实际域名和邮箱在 `deploy.sh` 配置。
依赖构建默认使用官方 PyPI；需要切换公共镜像源时可执行
`docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple backend`。

### 从 GitHub GHCR 拉取

先配置 [镜像发布](../docs/ci-cd.md)，在 `.env` 设置：

```dotenv
BACKEND_IMAGE=ghcr.io/skissy/fast_admin-backend
IMAGE_TAG=latest
```

不需要 Docker Hub 账号。首次发布后将 GitHub Packages 中的镜像包设为 Public，可免登录拉取；
保留私有时先执行 `docker login ghcr.io`，见 CI/CD 说明。

首次运行先启动依赖、拉取镜像并迁移数据库：

```bash
docker compose up -d --wait mysql redis
docker compose pull backend
docker compose run --rm --no-deps backend alembic upgrade head
docker compose up -d --no-build --wait backend
bash ../deploy.sh cert:init
docker compose up -d nginx
```

Nginx 配置和证书必须对应实际域名。已有服务更新步骤见 CI/CD 文档。
`restart` 只重启现有容器，更新镜像需要 `pull` 后执行 `up -d`。

### 开发覆盖

```bash
docker compose -f docker-compose.yaml -f docker-compose.dev.yaml up -d --build
```

后续管理也带上同样的两个 `-f` 参数，避免重建时丢失源码挂载。应用配置文件仍需存在。

## B：只运行后端

复制 `deploy.sh`、`docker-compose.backend.yaml`、`.env.example` 到服务器同一目录，
再将 `backend/env/.env.prod.example` 复制并命名为 `env.prod.example`。
需要本地依赖时再复制 `docker-compose.local-deps.yaml`。

```bash
cp .env.example .env
cp env.prod.example env.prod
# .env 使用默认 GHCR 镜像，按需固定 IMAGE_TAG
# env.prod 填写随机 SECRET_KEY 和已有数据库、Redis 连接配置
docker compose -f docker-compose.backend.yaml pull backend
bash deploy.sh db:migrate
bash deploy.sh start
```

可选依赖使用 `bash deploy.sh start --local-deps`；拉取、迁移等手工命令也须同时带上
`-f docker-compose.backend.yaml -f docker-compose.local-deps.yaml`。先启动依赖再迁移。
主文件不声明 `depends_on`，可独立解析。

服务器已有 Nginx 的反代示例：

```nginx
location /api/v1 {
    proxy_pass http://127.0.0.1:6100;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

## 离线交付

```bash
# 开发机，在仓库根目录执行；先准备 docker/.env、backend/env/.env.prod
bash deploy.sh image:export 3.1.0
# 复制 docker/dist/backend-3.1.0.tar.gz 和 .sha256 到服务器

# 服务器 .env 的 BACKEND_IMAGE/IMAGE_TAG 必须和导出的镜像一致
bash deploy.sh image:load
bash deploy.sh db:migrate
bash deploy.sh start
```

离线导出固定使用本地 `fast_admin-backend:3.1.0`；服务器 `.env` 应设置
`BACKEND_IMAGE=fast_admin-backend`、`IMAGE_TAG=3.1.0`，不依赖 GHCR 访问。
加载镜像只更新本地镜像缓存，不会自动更新现有容器。

## 日常管理与配置验证

```bash
bash deploy.sh status
bash deploy.sh logs backend
bash deploy.sh restart
bash deploy.sh cert:renew

cd docker
docker compose -f docker-compose.yaml config --quiet
docker compose -f docker-compose.yaml -f docker-compose.dev.yaml config --quiet
docker compose -f docker-compose.backend.yaml config --quiet
docker compose -f docker-compose.backend.yaml -f docker-compose.local-deps.yaml config --quiet
```

管理后台静态文件放在 `docker/nginx/web/`。CI 的前端产物需另行部署到此目录，后端镜像发布
不包含这些静态文件，也不会自动修改线上容器。
