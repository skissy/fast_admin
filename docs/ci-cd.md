# GitHub Actions 与 GHCR

工作流：[`ci-cd.yml`](../.github/workflows/ci-cd.yml)。Pull Request、`main` 推送、`v*` 标签
及 Actions 页面手动运行都会执行检查。只有全部检查通过，才构建后端 Docker 镜像。

## 检查与交付

| 阶段 | 内容 |
| --- | --- |
| 后端 | 按 `.python-version` 选择 Python，`uv sync --frozen`，Ruff、Pyright、完整 pytest（含真实 HTTP 回归） |
| 管理后台 | Node.js 24、pnpm 11.25.0，锁定依赖、生产构建、TypeScript、Vitest |
| 部署配置 | 四种 Compose 组合解析，`deploy.sh` Bash 语法检查 |
| 镜像 | 基于官方 Python 镜像构建 `linux/amd64` 后端镜像，发布到 GitHub Container Registry（GHCR） |
| 构建产物 | 保留 14 天的 JUnit 报告及 `web-dist-<commit SHA>` 管理后台静态文件 |

测试使用隔离的 SQLite 和内存缓存设施，不使用生产数据库或应用密钥。
检查任务只授予仓库内容读取权限，镜像任务另授予 `packages: write`；Actions 固定到完整提交 SHA。

## 无需 Docker Hub 账号

MySQL、Redis、Nginx、Certbot 使用官方镜像。后端包含本项目代码，由工作流基于官方
`python:3.14-slim` 构建，使用 GitHub 自动提供的 `GITHUB_TOKEN` 发布到 GHCR。
不需要注册 Docker Hub，也不需要配置 `DOCKERHUB_TOKEN`、SSH 密钥或额外发布令牌。
镜像构建默认从官方 PyPI 安装 Python 依赖，避免将某一地区的公共镜像源固定到 CI 中。

镜像地址按 GitHub 仓库自动生成：`ghcr.io/<owner>/<repository>-backend`，统一转成小写。
当前仓库是 **`ghcr.io/skissy/fast_admin-backend`**。
推送 `main` 或在 Actions 页面运行 **CI / GHCR** 即可触发。Pull Request 只构建，不登录或发布。

GitHub 新发布的包默认私有。首次成功发布后，打开 GitHub 个人主页的 **Packages**，选择
`fast_admin-backend` → **Package settings** → **Change visibility** → **Public**，即可让
服务器免登录拉取；包的可见性与源码仓库可见性分别管理。
若保留私有，在服务器使用具有 `read:packages` 权限的 GitHub PAT（classic）执行
`docker login ghcr.io`。工作流本身仍只使用自动令牌。

Fork 仓库时，工作流会自动向 Fork 所有者的 GHCR 发布，Compose 的 `BACKEND_IMAGE` 也应
改成对应地址。首次发布时若同名包已存在，应在包的 Actions access 中授权该代码仓库。

| 触发来源 | 发布标签 |
| --- | --- |
| `main` | `latest`、`sha-<完整 commit SHA>` |
| 例如 `v3.1.1` 的版本标签 | `3.1.1`、`sha-<完整 commit SHA>` |

生产建议使用固定版本或 SHA 标签；回滚时将 `IMAGE_TAG` 改为上一版本再拉取并重建容器。
数据库迁移需单独评估，镜像回滚不会撤销数据库变更。

## 服务器拉取与更新

Compose 会按 `image` 下载镜像，服务器不需要 GitHub SSH 部署凭据。已运行的容器不会
仅因镜像仓库出现新镜像而更新；更新时需要 `pull` 和 `up`。这里配置的是持续交付镜像，
生产部署由操作者执行，不自动连接或修改服务器。

在服务器 Compose 所在目录的 `.env` 中填写：

```dotenv
BACKEND_IMAGE=ghcr.io/skissy/fast_admin-backend
IMAGE_TAG=latest
BACKEND_PORT=6100
```

公开的 GHCR 镜像可直接拉取；私有镜像需先在服务器执行 `docker login ghcr.io`。
随机 `SECRET_KEY`、数据库地址与密码继续保留在服务器应用配置中，不放进镜像或工作流。
首次安装见 [Docker 部署说明](../docker/README.md)。

### A：已有全套 Compose 服务

在仓库的 `docker/` 目录执行：

```bash
docker compose pull backend
docker compose run --rm --no-deps backend alembic upgrade head
docker compose up -d --no-build --wait backend
docker compose ps
```

迁移失败时先处理迁移问题，再更新服务。首次部署先启动 `mysql redis`，并准备实际域名的
Nginx 配置和证书。

### B：服务器只有后端编排文件

```bash
docker compose -f docker-compose.backend.yaml pull backend
docker compose -f docker-compose.backend.yaml run --rm --no-deps backend alembic upgrade head
docker compose -f docker-compose.backend.yaml up -d --no-build --wait
```

使用可选本地依赖时，每个命令都需同时带 `-f docker-compose.local-deps.yaml`；首次运行
先启动这份编排中的 `mysql redis`。

## 管理后台静态文件

`web-dist-<commit SHA>` 是独立产物，不包含在后端镜像中。构建使用
`frontend/web/env/` 的公开示例配置。自定义部署子路径、WebSocket 域名时，应先修改相应
公开构建配置，再发布；不要把服务器密钥放入 `VITE_*` 变量。

下载并解压产物，将内容部署到 `docker/nginx/web/` 或服务器 Nginx 对应的静态目录。
发布前检查 `/web/` 页面和 `/api/v1` 接口。

## 参考

- [GitHub：发布 Docker 镜像](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)
- [GitHub Container Registry：认证、发布与拉取](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
- [GitHub Packages：包的访问权限与可见性](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)
- [Compose 镜像拉取策略](https://docs.docker.com/reference/compose-file/services/#pull_policy)
