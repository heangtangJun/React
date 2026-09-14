---
kind: build_system
name: 构建与部署：基于脚本与 Docker Compose 的轻量级工程化
category: build_system
scope:
    - '**'
source_files:
    - pip.sh
    - README.md
    - docker/postgresql/docker-compose.yml
    - docker/redis/docker-compose.yaml
    - 01_backendServer.py
    - 02_frontendServer.py
    - .env
---

## 1. 使用的系统/方法

本项目采用**极简、脚本驱动**的构建与部署方式，没有引入 Makefile、Dockerfile、CI/CD 流水线或包管理清单（requirements.txt / pyproject.toml）。核心依赖安装通过根目录的 `pip.sh` 脚本完成，服务编排通过 `docker/` 下的独立 `docker-compose.yml` 文件启动 PostgreSQL 与 Redis。

- **依赖安装**：`pip.sh` 使用 `set -e` 确保任一安装失败即停止，并通过多行 `pip install` 一次性安装 LangGraph、LangChain、langchain-deepseek、langchain-mcp-adapters 等核心库。
- **环境搭建**：README 明确给出 Conda 虚拟环境创建命令（`conda create -n ReActAgents python=3.11`）以及精确到小版本的 pip 安装语句（如 `langgraph==0.4.5`、`fastapi==0.115.12`、`celery==5.5.3` 等），强调“先使用要求的对应版本进行本项目测试”。
- **容器化**：仅对数据库层做容器化封装，应用本身以 Python 脚本直接运行（`python 01_backendServer.py`、`python 02_frontendServer.py`），Celery worker 通过命令行 `celery -A 01_backendServer.celery_app worker --loglevel=info` 启动。
- **无 CI/CD**：仓库中未发现 `.github/workflows`、Jenkinsfile、Makefile、Dockerfile、setup.py、pyproject.toml、requirements.txt 等任何自动化构建或发布配置。

## 2. 关键文件

| 文件 | 作用 |
|---|---|
| `pip.sh` | 依赖安装入口脚本，锁定 LangChain/LangGraph 生态 |
| `README.md` | 项目初始化、依赖安装、服务启停、功能测试的权威说明 |
| `docker/postgresql/docker-compose.yml` | 启动 PostgreSQL 15，含健康检查、持久卷、时区与连接数配置 |
| `docker/redis/docker-compose.yaml` | 启动 Redis，含健康检查、数据卷、bridge 网络定义 |
| `01_backendServer.py` | FastAPI 后端 + Celery worker 入口（通过 `-A` 指定 celery_app） |
| `02_frontendServer.py` | rich 终端前端演示服务 |
| `.env` | 运行时环境变量（数据库、Redis、LLM 密钥等） |

## 3. 架构与约定

- **分层容器化**：仅基础设施（PostgreSQL、Redis）用 Docker Compose 管理，业务代码不打包镜像，开发/测试直接在宿主机 Python 环境中运行。两个 compose 文件各自独立，需分别进入子目录执行 `docker-compose up -d`。
- **版本锁定策略**：README 中以固定版本号列出所有 pip 依赖，并在警告框中要求“先使用要求的对应版本进行测试”，体现**显式版本锁定**的约束；`pip.sh` 则只安装最新可用版本，两者存在差异，实际应以 README 为准。
- **启动顺序约定**：必须先启动 PostgreSQL 与 Redis（Docker Compose），再启动 Celery worker，最后启动 FastAPI 后端与前端，顺序在 README 的“异步模式调用 Agent 服务”一节中明确规定。
- **配置来源**：数据库、Redis、LLM 等外部服务地址与凭据通过 `.env` 注入，应用代码读取该文件，避免硬编码。
- **日志与锁**：`logfile/app.log` 为应用日志输出位置，`.__app.lock` 用于进程级互斥（由 concurrent-log-handler 等机制产生）。

## 4. 约定与约束

- **必须**：运行前先用 Conda 创建 `ReActAgents` 虚拟环境并安装 README 所列全部依赖；否则无法保证兼容性。
- **必须**：通过 `docker-compose up -d` 分别启动 `docker/postgresql` 与 `docker/redis`，且需等待健康检查通过后再启动应用。
- **必须**：按 `celery worker → FastAPI 后端 → 前端` 的顺序启动服务。
- **禁止**：随意升级依赖版本——README 明确要求先使用指定版本测试通过后再考虑升级。
- **约束**：当前仓库不存在统一的构建脚本（Makefile）、应用镜像构建（Dockerfile）、自动化测试/发布流水线（CI），因此本项目的“构建”仅限于本地依赖安装与服务编排。

## 5. 缺失项（现状说明）

- 无 `Makefile` / `build*.sh` / `Dockerfile` / `pyproject.toml` / `requirements.txt` / `Pipfile` / `setup.py`。
- 无 GitHub Actions、GitLab CI、Jenkins 等 CI/CD 配置。
- 无制品打包、版本标签、发布流程。
- 依赖安装脚本 `pip.sh` 未包含 README 中列出的全部依赖（缺少 fastapi、celery、redis、rich 等），与文档存在不一致。

总体而言，该项目将“构建”理解为**本地环境准备 + 容器化基础设施编排**，适用于教学/原型场景，尚未达到生产级工程的自动化构建与发布标准。