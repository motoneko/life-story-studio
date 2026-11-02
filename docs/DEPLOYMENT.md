# 部署与运维

## 本地

按 README 启动即可。首次运行创建 `data/studio.sqlite3` 与 `data/uploads/`。不要把 `data/`、`.env` 放进 Git 或网站静态目录。

Linux 如缺少虚拟环境或 FFmpeg，可通过发行版包管理器安装 `python3-venv` 和 `ffmpeg`。Windows 需安装 Python 3.10+ 和 FFmpeg，并让 `ffmpeg -version`、`ffprobe -version` 可在命令行执行。

## Docker 单机

```bash
cp .env.example .env
# 修改 .env
# 本机访问无需启用 COOKIE_SECURE
docker compose up --build -d
```

访问 http://127.0.0.1:8000。数据库和录音在 `studio-data` 命名卷中，重建容器不会丢失。**不要执行 `docker compose down -v`，除非确定删除全部数据。**

## 域名与 HTTPS

1. 准备域名及服务器，将域名解析到服务器。
2. 在 `.env` 加入 `DOMAIN=story.example.com`，并设置 `COOKIE_SECURE=true`、真实 AI 配置。
3. 放行 80、443 端口，应用的 8000 端口保持仅绑定本机。
4. 执行：

```bash
docker compose --profile production up --build -d
```

Caddy 自动申请 TLS 证书。打开 `https://你的域名`。正式域名证书签发取决于 DNS 和服务器公网可达性。浏览器麦克风访问需要 HTTPS。开启 Secure Cookie 后，不要用普通 HTTP 登录。

`Caddyfile` 默认限制请求体 210 MB，与应用默认 200 MB 文件限制配套；调整上传限制时同步修改。应用流式读取上传，但框架解析 multipart 可能先落临时盘，因此公网入口限制不能省略。

## 单进程约束

只运行 **1 个 Uvicorn Web进程、1 个任务worker**，或一个内嵌worker的Web进程。队列虽存于 SQLite，但启动恢复由当前进程负责，任务进程用数据库租约拒绝重复启动；仍不支持跨主机横向扩容。服务重启时，未开始任务继续排队，运行中任务改为失败供用户重试。失败任务不会自动反复计费。

音频识别失败重试会复用同一音频和模型配置下已成功的分片缓存。单项目串行处理任务，任务期间禁止素材修改与项目删除，避免覆盖。其他项目仍可浏览、编辑和排队；在线追问使用独立通道，不再等待其他项目的长音频批处理。需要大并发时迁移到独立队列/数据库后再扩容。

## 备份与恢复

推荐在维护窗口停止应用后同时备份整个 `data` 目录/命名卷和 `.env`（密钥应单独加密存放）。数据库和上传目录必须作为同一批次恢复。备份访问权限与原始数据相同，运营方自行规定保留期限并演练恢复。

在线备份 SQLite 时应使用 Python sqlite3 的 backup API，不能只复制主数据库而遗漏 WAL。恢复前停止应用，恢复文件所有权到容器用户 UID 10001。升级前备份；当前使用幂等增量迁移添加 included、edit_revision 与整书确认表；复杂结构仍需正式迁移框架。

## 运维检查

- `/health` 用于数据库连通性健康检查，不暴露配置和密钥。
- `docker compose logs --tail=100 app` 查看错误类别和任务 ID；不要将包含个人资料的调试信息发到公开日志系统。
- 定期检查磁盘空间、队列积压、失败率、AI 账单和备份可恢复性。
- 环境变量和依赖变更后重建/重启。`requirements.lock` 固定本次验证版本；版本升级后运行测试。
- 登录接口有单进程、按来源 IP 的基础限流；公网需补共享网关限流、注册滥用保护、邮箱验证、密码找回、账户配额和安全审查。不要把本地验收当作已完成商用公网验收。

## 验证状态

自动化测试验证功能与权限，不等同真实服务联调。没有供应商密钥时只能验证协议适配和模拟响应；上线前必须用选定服务商实际验证中文/方言录音、成本、延迟、错误恢复与书稿质量。当前 Docker 配置需在实际部署主机完成构建和证书验收。

本次环境的 Docker 构建尝试被 WSL / Docker Desktop 集成状态阻止，未完成镜像构建验收；Python 服务与测试不依赖 Docker。

## 1.0：独立任务进程与账户恢复

新版 compose 默认运行一个Web进程和一个独立worker，共享命名卷；Web设置 `RUN_WORKER=false`。本机默认仍可单进程启动内嵌worker。请勿在同一数据库上同时运行多个worker；租约会拒绝第二个消费者。跨主机部署需更换数据库/存储与队列，不能直接扩容当前compose。

```bash
RUN_WORKER=false uvicorn app.main:app --host 127.0.0.1 --no-access-log
# 另一终端，在相同DATA_DIR与环境配置下
python -m app.worker
```

原声分享URL包含访问凭据，生产访问日志不得收集完整分享URL。compose已关闭应用access log。

配置 `PUBLIC_BASE_URL`、`SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASSWORD`、`SMTP_FROM` 可启用密码恢复，SMTP使用STARTTLS。恢复链接30分钟有效且只能用一次，重设会撤销旧会话。没有SMTP的自部署管理员可执行：

```bash
python -m app.admin reset-password person@example.com
```

密码交互输入，不放入shell参数或环境变量。

停止Web/worker后执行一致性备份（压缩包含账户哈希和私人资料，应加密保存）：

```bash
python -m app.admin backup /safe/location/backup.zip
python -m app.admin restore /safe/location/backup.zip /new/empty/data
```

恢复仅允许空目标目录，之后设置DATA_DIR并恢复文件权限。`.env` 不在备份中，密钥应由独立秘密管理流程恢复。项目删除不删除历史备份，运营方必须明确保留期限与到期清理。

MAX_PROJECT_JOBS、MAX_PROJECT_AUDIO_MB、MAX_JOB_ATTEMPTS 是部署上限；项目创建者可在上限内下调/调整。成功ASR分片缓存存数据库，录音/模型配置变更使缓存失效。新版本schema为幂等增量迁移，升级前备份，不能用旧代码写入新schema。
