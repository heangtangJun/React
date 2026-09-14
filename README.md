# 从零构建生产级Agent服务

## 1、系统功能

- 使用FastAPI框架实现对外提供Agent智能体API后端接口服务

- 使用LangGraph中预置的ReAct架构的Agent

- 支持Short-term（短期记忆）并使用PostgreSQL进行持久化存储

- 支持Function Calling，包含自定义工具和MCP Server提供的工具

- 支持Human in the loop（HITL 人工审查）对工具调用提供人工审查功能，支持四种审查类型

- 支持多厂家大模型接口调用，OpenAI、阿里通义千问、本地开源大模型（Ollama）等

- 支持Redis存储用户会话状态，支持客户端的故障恢复和服务端的故障恢复

- 使用功能强大的rich库实现前端demo应用，与后端API接口服务联调

  

- 支持动态调整会话的过期时间

- 支持用户登录到系统后自动打开最近一次使用的会话，若无则新建会话

- 支持历史会话管理和历史会话恢复

- 支持修剪短期记忆中的聊天历史以满足上下文对token数量或消息数量的限制

- 支持读取和写入长期记忆（如用户偏好设置等）

  

- 支持异步模式调用Agent运行，支持并行(Celery是一个强大的异步任务队列/作业队列库)，接口立即返回task_id

- 支持客户端随时通过task_id来查询服务端任务的状态与响应内容



## 2、核心业务流程

### 2.1 后端业务核心流程                             

- docs/01_后端业务核心流程.pdf

- docs/02_API接口和数据模型描述.pdf                                             

### 2.2 前端业务核心流程                                  
- docs/03_前端业务核心流程.pdf                                 

​                      


## 3、前期准备工作
### 3.1 集成开发环境搭建
- anaconda提供python虚拟环境（Conda）

- pycharm提供集成开发环境

### 3.2 大模型LLM服务接口调用方案
- OpenAI等国外大模型使用方案

  - 国内无法直接访问，可以使用代理的方式，具体代理方案自己选择 

- 国内大模型采用厂商原生接口

- 本地开源大模型方案（Ollama方式）

  ​       


## 4、项目初始化                                                                                  
### 4.1 安装项目依赖

```bash
# 创建项目虚拟环境
conda create -n ReActAgents python=3.11
```

```bash
# 安装项目依赖
pip install langgraph==0.4.5
pip install langchain==0.3.25
pip install langchain-openai==0.3.17
pip install langgraph-checkpoint-postgres==2.0.21
pip install rich==14.0.0
pip install fastapi==0.115.12
pip install redis==6.2.0
pip install concurrent-log-handler==0.9.28
pip install celery==5.5.3
```

> [!CAUTION]
>
> 建议先使用要求的对应版本进行本项目测试，避免因版本升级造成的代码不兼容。测试通过后，可进行升级测试。



### 4.2 构建项目 

使用PyCharm构建一个项目，为项目配置虚拟python环境
项目名称：ReActAgents

### 4.3 将课件代码拷贝到项目工程中
将下载的代码文件夹中的文件全部拷贝到新建的项目根目录下




## 5、功能测试
### 5.1 使用Docker方式运行PostgreSQL数据库和Redis数据库                                      
1. 进入官网 https://www.docker.com/ 下载安装Docker Desktop软件并安装，安装完成后打开软件
2. 打开命令行终端，进入到docker/postgresql下执行 docker-compose up -d 运行PostgreSQL服务
3. 进入到docker/redis下执行 docker-compose up -d 运行Redis服务
4. 运行成功后可在Docker Desktop软件中进行管理操作或使用命令行操作或使用指令
5. 使用数据库客户端软件远程登陆进行可视化操作，这里使用Navicat客户端软件和Redis-Insight客户端软件



### 5.2 测试 HITL 对工具请求进行人类反馈

1. 进入 04_ReActAgentHITLApi 文件夹下运行脚本进行测试
2. 运行后端服务 `python 01_backendServer.py`
3. 运行前端服务 `python 02_frontendServer.py`



使用python实现的一个模拟酒店预订的工具 `book_hotel`

- 其需传入的参数为：{hotel_name}

使用python实现的一个计算两个数的乘积的工具 `multiply`

- 其需传入的参数为：{a:float, b:float}

**测试流程：**

1. 输入 'yes' 接受工具调用

   调用工具预定如家酒店

2. 输入 'no' 拒绝工具调用

   调用工具预定桔子酒店

3. 输入 'edit' 修改工具参数后调用工具

   调用工具预定全季酒店

   {"hotel_name": "全季酒店(软件园店)"}

4. 输入 'response' 不调用工具直接反馈信息

   调用工具预定汉庭酒店

   把酒店名称换为：汉庭酒店(软件园店)，再调用工具预定



### 5.3 测试 status、new、exit指令

1. **status** 查看会话状态，用于客户端故障恢复
2. **new** 新建会话
3. **exit** 退出当前会话



### 5.4 测试客户端和服务端故障恢复

客户端故障恢复：会话管理

1. 用户ID（test2）：上海天气如何？
2. 强制关闭客户端（意外退出）
3. 再次启动客户端，输入用户ID（test2），会话自动恢复到中断的状态

服务端故障恢复：LangGraph节点的状态恢复（checkpointer）



### 5.5 测试动态调整会话的过期时间

### 5.6 测试历史会话管理和历史会话恢复



### 5.7 测试异步模式调用Agent服务

1. 进入 `06_ReActAgentHILApiMultiSessionTask` 文件夹下运行脚本进行测试，支持多用户访问
2. 首先运行 `celery -A 01_backendServer.celery_app worker --loglevel=info` 启动 celery 服务
3. 再运行后端服务 `python 01_backendServer.py`
4. 最后运行前端服务 `python 02_frontendServer.py`



## 6、扩展学习

NirDiamant：https://github.com/NirDiamant
