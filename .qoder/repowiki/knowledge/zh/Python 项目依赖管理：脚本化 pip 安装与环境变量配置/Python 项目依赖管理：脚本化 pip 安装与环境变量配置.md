---
kind: dependency_management
name: Python 项目依赖管理：脚本化 pip 安装与环境变量配置
category: dependency_management
scope:
    - '**'
source_files:
    - pip.sh
    - README.md
    - .env
    - utils/config.py
    - utils/llms.py
---

## 1. 使用的系统/方法

该项目是一个 Python 工程，采用**最简化的依赖管理方式**：
- 没有使用 `requirements.txt`、`pyproject.toml`、`setup.py`、`Pipfile`、`poetry.lock` 等任何标准依赖清单文件。
- 没有使用虚拟环境隔离（README 中建议用 Conda 创建 `ReActAgents` 环境，但仓库内未包含 `.condarc` 或 `environment.yml`）。
- 没有使用私有 PyPI 源或镜像配置。
- 没有使用 vendoring（无 `vendor/` 目录）。
- 没有使用 `pip freeze > requirements.txt` 生成锁定文件。

依赖安装通过两种方式完成：
1. **`pip.sh` 脚本**：集中安装 LangChain/LangGraph 生态核心包（`langgraph`、`langchain`、`langchain-deepseek`、`langchain-mcp-adapters`），但未指定版本。
2. **`README.md` 中的安装命令**：以逐条 `pip install <包>=<固定版本>` 的形式列出所有运行时依赖，包括 `langgraph==0.4.5`、`langchain==0.3.25`、`fastapi==0.115.12`、`celery==5.5.3`、`redis==6.2.0`、`rich==14.0.0`、`concurrent-log-handler==0.9.28`、`langgraph-checkpoint-postgres==2.0.21`、`psycopg_pool` 等。

## 2. 关键文件

- `pip.sh`：LangChain 生态包的快捷安装脚本（无版本锁定）。
- `README.md` 第 73–96 行：完整依赖列表及固定版本号，是实际约束依赖版本的权威来源。
- `.env`：存放运行时密钥与数据库连接串（`DASHSCOPE_API_KEY`、`DB_URI` 等），属于运行时配置而非依赖声明。
- `utils/config.py`：集中管理应用级常量（PostgreSQL 连接池大小、Redis 地址、Celery broker URL、LLM 类型、服务端口等），通过 `os.getenv` 读取环境变量覆盖默认值。
- `utils/llms.py`：通过 `MODEL_CONFIGS` 字典统一管理多厂商 LLM 后端（openai / oneapi / qwen / ollama），支持通过环境变量注入 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`DASHSCOPE_API_KEY` 等。

## 3. 架构与约定

- **依赖来源**：全部来自官方 PyPI 仓库，未配置自定义 index URL。
- **版本策略**：依赖版本仅记录在 README 文档中，未固化到可被 CI 自动读取的清单文件中；`pip.sh` 中的包名不指定版本，存在漂移风险。
- **配置与依赖分离**：第三方库本身通过 pip 安装，其运行时参数（模型 base_url、API key、数据库 URI、Redis broker URL）通过 `.env` + `os.getenv` 注入，由 `utils/config.py` 和 `utils/llms.py` 统一读取。
- **容器化辅助**：`docker/postgresql/docker-compose.yml` 与 `docker/redis/docker-compose.yaml` 仅用于启动外部依赖服务（PostgreSQL、Redis），不包含 Python 依赖安装逻辑。

## 4. 约定与约束

- README 明确提示“建议先使用要求的对应版本进行本项目测试，避免因版本升级造成的代码不兼容”，表明**固定版本是强制要求**，但约束仅存在于文档层面。
- 运行前需先执行 `conda create -n ReActAgents python=3.11` 创建 Python 3.11 环境，再按 README 顺序逐个 `pip install` 安装依赖。
- 所有敏感信息（API Key、数据库连接串）必须放入 `.env`，禁止硬编码进源码（`utils/config.py` 通过 `os.getenv` 读取）。
- 由于缺少 `requirements.txt`/`pyproject.toml` 等清单文件，该仓库**不具备可复现的依赖锁定能力**，不同机器上 `pip.sh` 可能安装到最新兼容版本，导致行为差异。