# TAPD ↔ Notion 同步服务

TAPD 与 Notion 之间的需求数据库同步与分析工具，支持在 TAPD 中获取需求信息并同步到指定的 Notion 数据库，同时针对需求内容生成结构化的分析结果、功能点和测试流附加产物。

---

## 核心能力
- **需求拉取**：基于 TAPD Open API 增量获取需求（Story），支持负责人、创建人、迭代及 ID 精确过滤。
- **数据扩展**：自动补充需求的标签、附件、评论等信息并写入 Notion 属性。
- **内容同步**：使用幂等 upsert 写入 Notion 数据库，维护页面正文、属性、多选字段等；支持“仅新增”模式。
- **分析与测试**：
  - `analyzer` 模块对需求描述进行规则/LLM 分析，输出“内容分析”“需求点”等区块。
  - `testflow` 集成可根据需求生成测试附件、发送邮件（需显式执行与确认）。
- **状态记录**：`data/state.json` 维护上次同步时间与已跟踪的 TAPD_ID，支持增量与冲突恢复。
- **命令行工具链**：`scripts/`、`src/cli.py` 提供 sync/update/export/testflow 等命令，Makefile 封装常用流程。
- **可观测性**：控制台输出结构化日志；`logs/` 保存执行记录，失败场景会打印详细警告。

---

## 快速上手
1. **加载配置**
   ```bash
   cp .env.example .env
   # 填写 TAPD 与 Notion 凭证，详见下方配置说明
   ```
2. **准备运行环境**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
   或直接执行 `make setup`（自动创建虚拟环境并安装依赖）。
3. **本地验证（dry-run，不写入 Notion）**
   ```bash
   python3 scripts/pull
   ```
4. **实际写入 Notion（需显式确认）**
   ```bash
   python3 scripts/pull -e --ack "我已知悉：拉取是严重危险操作" --owner 江林 --current-iteration
   ```
   默认范围为 `--owner 江林 --current-iteration`，如需覆盖请显式传参。成功写入后会自动触发一次 `update-all`；可通过 `-P` 禁用自动更新。
5. **常用命令**
   ```bash
   # 更新既有页面（按 TAPD_ID 匹配，不创建新页面）
   python3 scripts/update -e --ack "我已知悉：更新是中度危险操作"

   # 按需求 ID 明确更新（缺失时可创建）：-C 表示允许创建
   python3 scripts/update -i 123456,123457 -e -C --ack "我已知悉：更新是中度危险操作"

   # 仅新增（已存在的页面不更新）
   python3 scripts/pull -n -e --ack "我已知悉：拉取是严重危险操作"

   # 导出当前 Notion 数据库到 JSON
   python3 scripts/export -l 100 -O data/export.json

   # 按模块逐个同步（适合大规模初始化）
   python3 scripts/pull -m -e --ack "我已知悉：拉取是严重危险操作"

   # 生成测试流附件（需按提示确认 ack）
   python3 scripts/testflow --limit 5 --execute --ack "我已知悉：拉取是严重危险操作"
   ```
   所有命令均支持 `-h/--help` 查看完整参数。
   亦可通过 `make pull ARGS="-e --ack \"我已知悉：拉取是严重危险操作\""` 触发带确认的拉取。

---

## 安全约束与默认参数
- **默认范围**：同步命令默认使用 `--owner 江林 --current-iteration`，避免误同步其他迭代数据；若需修改必须明确传参并在变更前确认风险。
- **危险操作确认**：所有写入操作必须追加 `--ack`。
  - 拉取/创建：`--ack "我已知悉：拉取是严重危险操作"`
  - 更新：`--ack "我已知悉：更新是中度危险操作"`
- **写入后的跟进**：成功执行带写入的 `pull` 后，应立即对同一范围运行 `python3 scripts/update -e --ack "我已知悉：更新是中度危险操作"`，确保 Notion 状态与 TAPD 保持一致。
- **Makefile 包装**：可使用 `make pull ARGS="-e --ack '我已知悉：拉取是严重危险操作'"` 或 `make update ARGS="-e --ack '我已知悉：更新是中度危险操作'"`，Makefile 会沿用默认范围；自定义范围时请同步更新命令参数。

---

## 命令执行流程（scripts/）

### `scripts/pull`
1. 解析命令参数并加载配置，按需初始化企业微信通知调度器（`send_wecom_markdown`）。
2. 根据 `--full/--since/--owner/--story-ids` 等旗标确定同步范围，自动补充“当前迭代”安全限制。
3. 选择执行 `run_sync` 或 `run_sync_by_modules`：后者会为每个模块拼装过滤条件并逐批处理。
4. 对匹配的需求执行标准流水线：经由 TAPD API 拉取详情 → `enrich_story_with_extras` 补齐标签/附件/评论 → `analyze` 产出分析 → 构建 Notion 属性和内容块。
5. 按 `--execute/--insert-only/--wipe-first` 等选项决定创建、更新或跳过页面；dry-run 模式仅输出计划操作，执行模式需附加 `--ack "我已知悉：拉取是严重危险操作"` 完成安全确认。
6. 实际写入时同步跟踪状态（`data/state.json`）并可将成功/失败摘要投递到企业微信。
7. 默认在成功写入后追加一次 `run_update_all` 作为兜底刷新；可通过 `-P/--no-post-update` 禁用。

### `scripts/update`
1. 解析参数后在三种模式间切换：指定 ID（`-i/-I/-f`）、从 Notion 反查（`--from-notion`）或默认的全量 `update-all`。
2. `update-by-ids`：按 ID 调用 TAPD `get_story`，补齐扩展字段，更新 Notion 页面；`--create-missing` 可在缺页时创建。
3. `update-from-notion`：先读取 Notion 索引，再回源 TAPD 完整刷新，完全不创建新页面。
4. `update-all`：遍历 TAPD 列表，受负责人/迭代过滤，只对已存在的 Notion 页面执行更新。
5. Dry-run 输出计划动作；执行模式会记录摘要并通过通知调度器推送成功或失败告警，执行时请加上 `--ack "我已知悉：更新是中度危险操作"`。

### `scripts/export`
1. 加载配置后构建 TAPD 与 Notion 客户端。
2. 查询 Notion 数据库，应用负责人、模块及游标过滤；必要时回查 TAPD 校验当前迭代。
3. 输出包含 `schema_version/items/next_cursor` 的 JSON，可写入文件或打印到标准输出。

### `scripts/testflow`
1. 校验危险操作确认（`--ack`、`--ack-mail`），加载测试人员表与配置。
2. 初始化 TAPD 客户端，并按负责人/创建人/迭代限制抓取需求样本。
3. 调用 `generate_testflow_for_stories`：优先尝试 LLM 生成测试用例，失败时回退规则模板。
4. 当 `--execute` 生效时，将测试用例导出至 `TESTFLOW_OUTPUT_DIR` 生成附件，并打印生成结果。
5. 如开启 `--send-mail`，构建邮件任务并调用 SMTP 发送，同时汇总发送状态。

### `scripts/status`
1. 初始化 TAPD 与 Notion 客户端，支持 `--current-iteration` 限制样本。
2. 抽样拉取 TAPD 需求并聚合状态值。
3. 调用 `NotionWrapper.sync_status_options_from_tapd` 更新 Notion 数据库中的状态选项。

### `scripts/modules`
1. 构建 TAPD 客户端。
2. 遍历 `list_modules()` 并打印模块名称与 ID，便于配置或 `--by-modules` 排错。

### `scripts/auth`
1. 初始化 TAPD 客户端后执行 `test_auth()`。
2. 默认输出状态码；`--verbose` 时打印完整响应 JSON 用于排查鉴权。

### `scripts/analyze`
1. 接收文本输入（缺省为示例），调用 `analyzer.rule_based.analyze`。
2. 打印结构化 JSON，快速验证规则命中的标签与段落拆分。

### `scripts/wipe`
1. 未显式传入 `--execute` 时仅提示危险性并退出。
2. 正式执行时初始化 Notion 客户端，调用 `clear_database(deep=...)` 归档数据库页面，可递归处理子页面。

### `scripts/sync`
1. 兼容旧入口：直接 `execv` 调用同目录下的 `pull`，参数保持不变。

### `scripts/cron.sh`
1. 切换到仓库根目录，按需加载 `.env`。
2. 以干跑方式执行 `python3 scripts/pull` 并将日志追加到 `logs/sync.log`，常用于定时任务。

---

## 环境配置
下表列出最常用的环境变量，完整列表可参考 `src/config.py`：

| 变量名 | 说明 |
| --- | --- |
| `TAPD_WORKSPACE_ID` | TAPD 工作空间 ID，必填 |
| `TAPD_API_USER` / `TAPD_API_PASSWORD` | TAPD Basic Auth 凭证（二选一，可使用 `TAPD_TOKEN`） |
| `TAPD_TOKEN` | 若使用 Token 鉴权，可填此项 |
| `NOTION_TOKEN` | Notion Integration Token，必填 |
| `NOTION_REQUIREMENT_DB_ID` | 需求数据同步目标 Notion 数据库 ID，必填 |
| `NOTION_DEFECT_DB_ID` | 缺陷数据目标 Notion 数据库 ID，选填 |
| `DEFAULT_OWNER` | 默认过滤的负责人，命令行可覆盖 |
| `TAPD_FETCH_TAGS` / `TAPD_FETCH_ATTACHMENTS` / `TAPD_FETCH_COMMENTS` | 是否在同步时拉取标签/附件/评论（默认开启） |
| `TAPD_STORY_TAGS_PATH` / `TAPD_STORY_ATTACHMENTS_PATH` / `TAPD_STORY_COMMENTS_PATH` | 自定义 API 路径，兼容不同租户 |
| `TAPD_USE_CURRENT_ITERATION` | 默认限制为当前迭代 |
| `CREATION_OWNER_SUBSTR` / `CREATION_REQUIRE_CURRENT_ITERATION` | 新建页面的安全限制，避免误创建 |
| `TESTFLOW_*` 系列 | TestFlow 功能所需配置（输出目录、是否发送邮件等） |

> `.env` 仅在本地使用，不会被纳入版本控制；上线或部署时可通过环境变量注入。若遗留脚本仍使用 `NOTION_DATABASE_ID`，会自动回退到 `NOTION_REQUIREMENT_DB_ID`。

---

## 项目结构
```
.
├── README.md                     # 当前文档
├── src/
│   ├── cli.py                   # CLI 入口（脚本统一调度）
│   ├── server.py                # FastAPI 入口（保留旧路径兼容）
│   ├── analyzer/                # 内容分析器（规则 / LLM / 图像）
│   ├── app/
│   │   ├── cli.py               # Typer CLI，封装 sync/update/testflow 子命令
│   │   └── server/              # FastAPI 应用与后台任务
│   │       ├── app.py
│   │       ├── actions.py
│   │       ├── jobs.py
│   │       └── schemas.py
│   ├── core/                    # 配置加载与状态持久化
│   │   ├── config.py
│   │   └── state/
│   │       └── store.py         # data/state.json 读写与增量游标
│   ├── integrations/            # 外部系统适配层
│   │   ├── tapd/
│   │   │   ├── client.py        # TAPD OpenAPI 封装与请求调度
│   │   │   ├── extras.py        # 附件 / 评论 / 标签补齐
│   │   │   └── story_utils.py   # 需求字段清洗与派生属性
│   │   └── notion/
│   │       ├── client.py        # Notion SDK 包装（重试、批量操作）
│   │       ├── content.py       # 页面正文块构建
│   │       └── mapper.py        # TAPD → Notion 属性映射
│   ├── services/
│   │   ├── notifications.py     # 企业微信 / 邮件通知封装
│   │   └── sync/
│   │       ├── service.py       # TAPD ↔ Notion 主同步管道
│   │       ├── frontend.py      # CLI/服务器复用的高层入口
│   │       ├── results.py       # 同步统计与报告模型
│   │       └── utils.py
│   └── testflow/                # 测试流生成、导出与邮件发送
├── scripts/                     # 命令封装（pull/update/export/testflow 等）
├── tests/                       # Pytest 单元 / 集成测试
├── data/                        # 运行期数据与缓存（state.json、export 等）
├── logs/                        # 同步 / 执行日志
└── docs/                        # TAPD 接口文档与设计说明
```

---

## 同步流程概览
```
┌──────────────┐    stories + extras    ┌──────────────────────────┐
│ TAPD OpenAPI │ ─────────────────────► │ services/sync/service.py │
└──────────────┘                        │                          │
        ▲                               │  map/merge               │
        │        increment / id cache   │                          │
        │  ┌─────────────────────────── │                          │
        │  │                            └────┬─────────────────────┘
        │  │                                 │
        │  │                           Notion SDK
        │  │                                 │
┌───────┴──┴───────────┐              ┌──────▼───────┐
│ core/state/store.py  │◄─ track ids ─│ Notion DB    │
└──────────────────────┘              └──────────────┘
```
- 按更新时间或指定 ID 拉取需求；
- 补齐标签、附件、评论等扩展字段；
- 映射并 upsert 到 Notion 属性与页面正文；
- 更新本地状态，供下一次增量同步与快速刷新使用。

---

## 测试与开发
- 运行全量测试：
  ```bash
  python3 -m pytest -q
  ```
  当前项目包含 30+ 项测试，覆盖同步逻辑、内容生成、TAPD 客户端参数组合等。
- 新增功能时建议同步补充测试用例；单元测试位于 `tests/`，命名遵循 `test_*.py`。
- 代码风格遵循 PEP 8，并尽量保持函数纯度与类型标注。
- 对外部服务（TAPD、Notion、邮件等）进行调用时，请在测试中使用 `monkeypatch` 或 mock。

---

## 参考资料
- 项目内附带 TAPD 官方接口文档镜像：`docs/tapd_api/`，使用前请参阅对应模块说明（如 `collaboration/attachment/get_attachments.md`、`comment/get_comments.md` 等）。
- `AGENTS.md` 收录运行与协作约定，操作前建议阅读。
- 若遇到接口返回异常或字段含义不明，可优先在 TAPD 官方文档中搜索（`doc` 目录几乎涵盖所有需求相关接口）。

---

## 常见问题 & 排查
- **API 权限不足**：确认 TAPD 应用已拥有需求读取及附件/评论访问权限。
- **Notion 429 / 速率限制**：系统会自动退避重试，若仍频繁触发，可降低批量写入速率或分批执行。
- **字段缺失**：检查 Notion 数据库是否存在同名属性（`状态`、`附件` 等），并确认类型与预期一致。
- **新建页面受限**：默认仅同步属于指定负责人的需求并且在当前迭代内；可通过环境变量或命令参数调整。
- **TestFlow 未生成附件**：确保配置了 `TESTFLOW_OUTPUT_DIR`、`TESTFLOW_TESTERS_PATH` 等依赖文件，并在执行时加上必需的 ack。

---

## TODO / Roadmap
- **图片分析与自动化引用**
  - HTML 描述中提取图片 URL（公网/私网）并在 Notion 页面原样插入，提供 TAPD 静态资源地址补全。
  - 图片轻量分析（尺寸、格式、主色）、语义摘要（视觉大模型）、OCR 与敏感信息检测，结果写入 “图片分析” 区块。
  - 按 URL 缓存与重试下载，限制最大图片数/来源域/超时时间，支持配置化开关。
- **Notion 属性补全优化**：`NotionWrapper._apply_extended_story_properties` 当前在部分路径被重复调用 3 次，应调整为单次调用并补充回归测试，避免潜在性能与幂等问题。
- **多类型实体支持**：规划扩展到 TAPD 任务（Task）、缺陷（Bug）等对象，需新增 mapper、同步策略及数据库字段映射。
- **Webhook / 实时同步**：调研 TAPD WebHook（或企业消息）以减少轮询延迟，结合现有状态缓存设计冲突解决策略。
- **双向同步评估**：在确保安全的前提下，调研 Notion 修改回写 TAPD 的可行性及幂等策略。
- **附件持久化与下载**：增加可选流程将附件下载落地到 `data/` 并在 Notion 中保留外链/上传，需评估体积与权限。
- **MCP 自动化引用**
  - 定义 Notion → MCP 的结构化契约（字段、块、图片元数据），提供只读导出脚本。
  - 支持负责人/迭代/模块等过滤以及增量游标，便于第三方终端消费。
  - 设计空值与错误占位策略（无图片/无描述）。
- **配置可视化文档**：为 `.env` 中的高级选项（如 `tapd_filter_*`、TestFlow、ACK 提示等）补充文档或示例，降低新成员接入门槛。
- **关联数据输出**：拉通需求关联的 Bug、任务、迭代看板，并提供周报/日报自动输出模板。
- **监控与告警**：集成失败重试、WeCom 通知与指标上报（成功率、限流命中），提升运维可视化。

---

## 许可证

项目内部使用，默认遵循企业内规范；若需外部发布，请先确认授权策略。
