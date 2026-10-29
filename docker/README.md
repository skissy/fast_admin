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

首次启动会创建数据库表并导入种子数据，可能持续数分钟。Nginx 等后端健康检查通过后启动，
健康探针直接访问 `/common/health/`。后端启动宽限期默认为 `180s`，较慢机器可在 Compose
`.env` 中增大 `BACKEND_HEALTH_START_PERIOD`；探针提前成功时会立即标记健康，无需等满宽限期。
若一直未就绪，先查看 `docker compose logs --tail=200 backend`，检查数据库和插件初始化日志。

## A：全套服务

### Nginx HTTP / HTTPS

Compose `.env` 中的 `NGINX_MODE` 默认为 `auto`：证书目录
`nginx/ssl/live/service.xxcdjl.xyz/` 下的 `fullchain.pem`、`privkey.pem` 和 `chain.pem`
均存在且非空时启用 HTTPS，否则启动 HTTP。也可用 `http` 固定 HTTP；`https` 固定 HTTPS，
证书缺失时明确报错退出。两个模式共用静态文件和 API 路由。

局域网部署可设置：

```dotenv
NGINX_MODE=http
HTTP_PORT=8081
HTTPS_PORT=8443
```

然后执行 `docker compose up -d --no-deps --force-recreate nginx`，访问
`http://<服务器IP>:8081/web/`。无证书时不监听 HTTPS，也不会把 IP 访问重定向到示例域名。

需要 HTTPS 时，先把 `nginx.conf` 的域名、证书路径与 `deploy.sh` 的 `CERT_DOMAIN` /
`CERT_EMAIL` 改为自己的配置，再执行 `bash ../deploy.sh cert:init`。签发流程先强制启动 HTTP
提供 ACME webroot，成功后重建 Nginx，按 `.env` 中的模式重新选取配置；使用 `auto` 即切换
至 HTTPS。公网 ACME 验证需要域名的 80 端口能够到达此 Nginx 的 HTTP 端口。
证书签发失败时保留 HTTP，排查日志后可重试；生产需要强制 HTTPS 时设置 `NGINX_MODE=https`。

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
docker compose up -d nginx
# 需要 HTTPS 时按上文配置自己的域名，再执行 bash ../deploy.sh cert:init
```

HTTPS 配置和证书必须对应实际域名。已有服务更新步骤见 CI/CD 文档。
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
