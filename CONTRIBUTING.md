# Contributing / 参与开发

使用 Python 3.10+，创建虚拟环境并安装 `requirements.lock`。运行 `python -m pytest -q`。浏览器验收另安装 Playwright 并运行 `python -m playwright install chromium`，启动临时 DATA_DIR 的测试服务后运行 `tests/browser_smoke.py` 和 `tests/browser_recording_recovery.py`。

提交 PR 时说明行为变化、验证方法与数据迁移影响。涉及权限、授权范围、导出、任务重试/取消或原始素材处理的改动必须有边界测试。不要提交真人采访、个人照片、账户数据库、令牌或 `.env`。

默认演示模式必须明确标识；真实服务失败不得静默回退成演示输出。模型抽取的引文应可定位到源文字；不要用模型生成的“来源”证明模型自己生成的事实。

数据库迁移应兼容已有项目，备份恢复与测试使用临时目录。临时分支及PR可以采用中文或英文；修改功能时同步维护文档中的已实现能力与限制。

提交内容按项目 MIT 许可证提供；贡献者应确认有权提交相应代码、文档和资源。用户故事与录音属于用户内容，不因代码采用MIT而自动开放授权。
