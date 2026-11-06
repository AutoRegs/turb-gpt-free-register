# Repository Guidelines

## 项目结构与模块职责

- `main.py` 是 CLI 入口；`web.py` 启动 Flask WebUI，`webui.sh` 管理后台进程。
- `core/` 包含注册、邮箱提供商、OAuth、账号管理和 SQLite 数据访问；配置默认值及环境变量加载位于 `config/`。
- `webui/` 负责路由、认证和配置编辑，页面模板与内嵌前端资源位于 `webui/templates/`。
- `sentinel/` 保存 JavaScript SDK 与运行器；`tests/` 存放自动化测试，`tools/` 存放诊断脚本，`docs/` 存放设计与协议分析文档。

## 环境与开发命令

要求 Python 3.10+、Node.js 18+。在仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

仅在 `.env` 不存在时复制模板，再填写本地配置。

- `python web.py`：启动本地 WebUI，默认监听 `127.0.0.1:5000`。
- `./webui.sh start`：后台启动；使用 `stop`、`restart`、`status`、`logs` 管理进程。
- `python main.py -n 1 --verbose`：执行一次真实注册流程，不作为无副作用的验证命令。
- 项目无独立编译构建步骤；浏览器驱动所需环境参见 `README.md`。

## 编码风格与命名

Python 使用四空格缩进；模块、函数和变量采用 `snake_case`，类采用 `PascalCase`，配置常量采用 `UPPER_SNAKE_CASE`。保持现有类型标注和中文文档风格。业务逻辑放入 `core/`，避免堆积到路由或模板中。仓库未配置统一格式化或 lint 工具，不要为局部修改引入整库格式化。

## 测试规范

测试使用标准库 `unittest`，文件命名为 `tests/test_<功能>.py`，测试方法以 `test_` 开头。

```bash
python -m unittest discover -s tests -p 'test_email_provider_gptmail.py'
python -m unittest discover -s tests
```

先运行相关测试，再运行完整测试集。使用 `unittest.mock.patch` 替换外部服务，数据测试使用临时目录并隔离数据库路径；运行前确认不会修改真实账号数据。修改功能时补充成功、失败及重试边界测试。当前未配置覆盖率门槛；`tools/test_*.py` 属于手动诊断脚本，可能访问真实服务，不等同于离线测试。

## 提交与 Pull Request

近期提交使用简短中文动作描述，例如“增加2fa并发数设置”“查活403优化”，没有统一的 Conventional Commits 前缀。提交应聚焦单一目的，说明具体模块与行为。PR 应交代变更原因、影响范围、配置变化及验证命令和结果；有关联 issue 时附上编号，界面变更附脱敏截图。

## 安全与配置

密钥仅保存在本地 `.env`，新增配置同步更新无敏感信息的 `.env.example`。禁止提交真实邮箱池、账号密码、Token、`turb.sqlite3`、日志或浏览器会话数据；分享 HAR 和截图前先脱敏。WebUI 默认保持本地监听，配置 `WEBUI_AUTH_CODE`，不要在未评估访问控制时暴露到公网。保留工作区已有修改，不覆盖他人的配置。
