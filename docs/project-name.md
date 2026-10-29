# 项目名称与升级兼容

项目名称统一为 **fast_admin**。后端文档标题、日志、AI 助手、站点默认参数、Web/UniApp
界面、Flutter 显示名称和说明文档使用该名称。

| 项目 | 当前名称 |
| --- | --- |
| GitHub 仓库 | `skissy/fast_admin` |
| GHCR 后端镜像 | `ghcr.io/skissy/fast_admin-backend` |
| 离线后端镜像 | `fast_admin-backend:<tag>` |
| 新安装的数据库 / MySQL 用户默认值 | `fast_admin` |
| Web npm 工程 | `fast_admin` |
| Flutter Dart 包 / 桌面可执行文件 | `fast_admin_mobile` |
| 初始化 SQL | `backend/sql/mysql/fast_admin_ddl.sql`、`backend/sql/postgres/fast_admin_ddl.sql` |

Docker 容器继续使用已有的 `fast-` 前缀，例如 `fast-backend`。GHCR 路径与此前配置一致，
CI/CD 无需创建新的镜像仓库。

## 已有部署

此次改名只更新代码、模板和默认值，不迁移或重命名真实数据库、数据库用户、数据卷。
已有部署应继续在私有环境配置中显式填写原来的 `DATABASE_NAME`、`DATABASE_USER`，以及
Compose 的 `MYSQL_DATABASE`、`MYSQL_USER`。例如原库仍叫 `fastapiadmin`，就继续填写该值；
不要仅为显示名称变化创建新库或更换连接账号。

已有数据库中的系统参数不会被种子初始化覆盖。需要同步界面名称时，在后台系统参数中
更新 `sys_web_title`、`sys_web_description` 和项目链接；公开构建标题也可在前端 `.env` 中设置。
重新获取 Flutter 依赖以刷新 Dart 包名映射，再重新构建客户端。

## 刻意保留的历史标识

- Android `applicationId` / Kotlin namespace、iOS/macOS Bundle ID 和 Linux application ID
  保持不变，以维持既有安装包和签名升级兼容。它们不作为界面项目名展示。
- 原作者署名、上游历史版本链接与外部示例图片地址保留原始信息，避免虚构资源地址。
- 国密测试中的固定消息及签名向量保持原样；安全升级文档中的历史 Redis 凭据前缀仍用于
  清理旧凭据，不应替换成新前缀。

本机私有 `.env` 和已生成的本地产物不由此次批量改名覆盖；如其中显式设置了旧的显示标题，
按实际部署需要修改对应标题字段即可。
