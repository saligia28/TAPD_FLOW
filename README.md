# TAPD ↔ Notion 需求管理集成方案与实施纲要

> 目标：打通 TAPD 与 Notion，建立一套可维护、可扩展的“需求管理数据库”，支持手动与定时同步，并在每条需求中自动/半自动生成“内容分析”与“功能点”。目前阶段仅做方案与准备工作，不落地实现。

---

## 快速上手（使用说明）
- 复制配置：`cp .env.example .env`，填入必需项
  - 必填：`TAPD_WORKSPACE_ID`、`TAPD_TOKEN`（或 `TAPD_API_USER`/`TAPD_API_PASSWORD`）、`NOTION_TOKEN`、`NOTION_DATABASE_ID`
  - 可选：`DEFAULT_OWNER`（默认负责人，默认为“saligia”）
- 安装依赖（二选一）
  - 一键：`make setup`
  - 手动：`python3 -m venv .venv && source .venv/bin/activate && python -m pip install -r requirements.txt`
- 先干跑验证（不写 Notion）：`python3 scripts/pull`
- 确认后执行写入：`python3 scripts/pull -e`（写入成功后会自动跟进一次 update；可用 `-P` 关闭）
- 常用操作
  - 仅更新已存在页面：`python3 scripts/update -e`
  - 指定 ID 更新并创建缺失：`python3 scripts/update -i 123,456 -e -C`
  - 当前迭代/负责人过滤：加 `-i` / `-o 张三`
  - 导出：`python3 scripts/export -o saligia -i -l 50 -O out.json`
  - 清库（危险）：`python3 scripts/wipe -e -d`
  - TestFlow（dry-run）：`python3 scripts/testflow --limit 5`
  - TestFlow 实际生成：`python3 scripts/testflow --limit 5 --execute --ack "我已知悉：拉取是严重危险操作"`
  - TestFlow 发送邮件：在上一步基础上追加 `--send-mail --ack-mail "邮件发送风险已知悉"`
- 定时任务（每小时）：见 `scripts/cron.sh`
- 命令执行环境说明：
  - `make pull`、`make update` 等 Make 目标默认调用 `.venv/bin/python`，因此需先运行 `make setup`（或手动创建 `.venv` 并安装依赖）。
  - 如果不使用虚拟环境，可直接执行 `python3 scripts/update -e`、`python3 scripts/pull` 等命令，但请自行保证当前 Python 解释器已经安装了 `requirements.txt` 中的依赖。
  - 打开新的终端后，若希望继续使用 `.venv`，记得 `source .venv/bin/activate` 再运行命令，或在命令前显式写 `./.venv/bin/python scripts/update ...`。

---

## 1. 可行性结论
- 可实现：TAPD 提供开放 API；Notion 提供官方 API 与集成（Integration）。二者通过中间同步服务即可打通。
- 首选单向主流程：TAPD → Notion（以 TAPD 作为权威源），避免双向冲突；后续可扩展双向。
- 运行方式：
  - 手动触发：命令行 `sync` 或工作流脚本。
  - 定时任务：本机 `cron`/`launchd`，或自托管服务/服务器定时器。
- 分析与功能点：
  - 初期：规则/模板化解析；
  - 进阶：接入 LLM（需外网/API Key），或半自动人工确认流程。

---


## 2. 整体架构
```
[TAPD Open API] --pull--> [同步服务/脚本] --upsert--> [Notion 数据库]
                             │
                             ├─ 分析器 Analyzer（规则/LLM/人工）
                             └─ 状态缓存 State（last_sync、ID 映射、重试队列）
```
- 同步策略：增量拉取（按更新时间/分页），在 Notion 端按 `TAPD_ID` 幂等更新（存在则更新，不存在则创建）。
- 可观测性：结构化日志 + 错误重试 + 速率限制（Notion 有速率限制，需退避）。

---

## 3. 数据模型与字段映射（建议）
- 核心对象：TAPD 需求（User Story/Story）、任务（Task），可从“需求”起步。
- Notion 数据库建议字段：
  - `Name`（Title）：TAPD 标题
  - `TAPD_ID`（Text/Number）：TAPD 唯一 ID（用于幂等）
  - `TAPD_链接`（URL）：跳转回 TAPD 的卡片链接
  - `状态`（Select）：映射 TAPD 状态（示例：新建/进行中/已完成/已关闭）
  - `优先级`（Select）：映射 TAPD 优先级
  - `负责人`（Multi-select 或 People）：若无需与 Notion 用户绑定，先用 Multi-select
  - `迭代`（Select/Text）：迭代/里程碑信息
  - `模块/标签`（Multi-select）：模块路径或标签
  - `计划开始`/`截止`（Date）：起止时间
  - `故事点 StoryPoints`（Number）：如有
  - `更新时间`（Date）：TAPD 最后更新时间
  - `分析版本`（Number）：分析策略用，判断是否需重算
  - `同步方式`（Select）：手动/定时（可选）
- 页面正文（Page Content）：
  - 段落 1：原始描述（可裁剪/清洗）
  - 段落 2：内容分析（生成/半自动）
  - 段落 3：功能点（项目符号清单；或分解为子任务）

---

## 4. TAPD 接入要点（概述）
- 申请企业开放 API（通常需 `AppKey`/`AppSecret` 或类似 Token），按官方签名规则与时间戳调用。
- 典型能力：按项目/工作区拉取需求列表与详情、分页与筛选（状态、更新时间段等）。
- 关键准备：
  - 获取可用的测试工作区与 API 凭证；
  - 明确需求对象的字段（含自定义字段）与状态流转；
  - 确认是否有 Webhook（若有，可后续做实时推送；初期先采用轮询）。

---

## 5. Notion 接入要点
- 创建 Notion Integration，拿到 `NOTION_TOKEN`；
- 在 Notion 中创建一个数据库（或使用现有 DB），并将其“分享”给该 Integration；
- 记录数据库 `NOTION_DATABASE_ID`；
- 字段类型尽量与 TAPD 对齐（状态、优先级等建议用 Select，人员初期用 Multi-select 文本，避免用户绑定问题）。

---

## 6. 同步与幂等策略
- 增量拉取：以 TAPD `updated_at` 与本地 `state.last_sync_at` 为界；
- 幂等 Upsert：在 Notion 以 `TAPD_ID` 查找，命中则更新，否则创建；
- 冲突策略：默认以 TAPD 为准覆盖 Notion 同步字段；Notion 中“分析/功能点”可视为派生字段（保留）；
- 速率限制：
  - Notion API 约 3 req/s（官方限制会调整），遇 `429` 需指数退避；
  - TAPD 端也需遵守分页与频控；
- 失败重试：记录失败项与原因，后续批量重试；
- 可追溯性：在 Notion 页加入 `TAPD_ID` 与回跳链接，日志中输出变更摘要。

---

## 7. 内容分析与功能点提取（方案）
- V1：规则/模板化
  - 基于需求描述关键词与结构，抽取“目标/范围/验收标准/风险”；
  - 功能点按句号/分号/关键动词拆分 + 去重归并；
- V2：LLM 辅助（可选）
  - 接入第三方 LLM（需外网/API Key），对描述进行结构化提炼；
  - 增加“人工确认”标记（在 Notion 中打勾后再写回“确认版本”）；
- 产出落位：
  - Notion 页面正文中追加“内容分析”与“功能点”分区；
  - 或将“功能点”映射为子任务数据库（进阶）。

---

## 8. 安全与配置
- 使用本地 `.env` 管理敏感变量，不入库：
```
# .env.example
TAPD_API_KEY=xxxx
TAPD_API_SECRET=xxxx
TAPD_WORKSPACE_ID=xxxx
NOTION_TOKEN=secret_xxx
NOTION_DATABASE_ID=xxxxxxx
TZ=Asia/Shanghai
```
- 配置项集中化：API 基地址、分页大小、默认迭代、字段映射表、分析开关；
- 日志中避免输出完整 Token/Sign；
- 如需部署到服务器，使用密钥管理（环境变量/密钥管理服务）。

---

## 9. 技术栈与项目结构（建议）
- 语言：
  - Python（推荐）：生态成熟（`requests`、`notion-client`、`tenacity` 重试、`pydantic` 校验）。
  - 或 Node.js：`@notionhq/client`、`axios`/`got`、`p-limit`；
- 目录结构（建议）：
```
.
├─ README.md
├─ .env.example
├─ src/
│  ├─ cli.py                # 命令行入口：fetch/sync/analyze/run
│  ├─ tapd_client.py        # TAPD API 封装（签名、分页、模型）
│  ├─ notion_client.py      # Notion API 封装（速率限制、重试）
│  ├─ mapper.py             # 字段映射与清洗
│  ├─ sync.py               # 增量同步与幂等逻辑
│  ├─ content.py            # 页面正文写入（分析/功能点）
│  ├─ analyzer/
│  │  ├─ __init__.py
│  │  ├─ rule_based.py      # 规则提取实现
│  │  └─ llm.py             # LLM 适配（可选）
│  └─ state/
│     └─ store.py           # last_sync、失败队列等
├─ data/
│  ├─ state.json            # 运行时状态（不提交）
│  └─ cache/                # 临时缓存（不提交）
└─ scripts/
   └─ cron.sh               # 定时任务脚本（读取 .env 后执行 cli）
```

---

## 10. 同步命令与定时运行（示例）
- 命令行（示意）：
```
# 验证 TAPD 基础认证是否可用
python3 scripts/auth

# 全量初始化（仅一次；默认 dry-run，加入 -e 执行写入）
python3 scripts/pull -f
# 实际写入：
# python3 scripts/pull -f -e

# 增量同步（常用；默认 owner=saligia，当前迭代）
python3 scripts/pull

# 按模块遍历并分别同步（dry-run）；列出模块
python3 scripts/pull -m
python3 scripts/modules

# 实际写入 Notion（谨慎执行，需已配置 NOTION_TOKEN/NOTION_DATABASE_ID）
python3 scripts/pull -m -e

# 覆盖式同步（先清空 Notion 数据库再全量重建；会自动忽略 since）
python3 scripts/pull -w -f -e

# 仅新增（按 TAPD_ID 判断，不更新已存在的 Notion 页面）
python3 scripts/pull -n -e

# 同步状态枚举到 Notion 数据库选项
python3 scripts/status -i -p 10 -l 50

# 只同步“saligia”负责或创建的需求（可用 CLI 覆盖 .env）
python3 scripts/pull -o saligia
python3 scripts/pull -c saligia

---

## 11. TestFlow 自动化（需求 → 测试用例）
- 目的：利用本地 Ollama 模型 `gpt-oss:20b` 自动生成测试用例，按测试人员拆分成 `.xmind` 并可选发送邮件。
- 配置：
  - `.env` 中补充 `OLLAMA_HOST`、`OLLAMA_MODEL`、`TESTFLOW_TESTERS_PATH` 等；
  - 在 `config/testers.json` 中维护测试人员 → 邮箱映射（示例已预填 `saligia28@126.com`）；
  - 若需真实邮件，请提供 `SMTP_HOST/PORT/USER/PASSWORD`，并设置 `TESTFLOW_MAIL_DRY_RUN=0`。
- 命令：
  - Dry-run：`python3 scripts/testflow --limit 5`
  - 生成 XMind：`python3 scripts/testflow --execute --ack "我已知悉：拉取是严重危险操作"`
  - 发送邮件：在上一步基础上追加 `--send-mail --ack-mail "邮件发送风险已知悉"`
- 输出：附件保存在 `data/testflow/`，文件名形如 `测试人员_YYYYMMDD_HHMMSS.xmind`；目录已加入 `.gitignore`。
- 更多待办与需求信息见 `TestFlow.md` 与 `feature.md`。

# 仅分析/重算功能点
python3 scripts/analyze -t "作为用户..."
```
- 定时（本机 cron）：
```
# 覆盖式清库（危险）
python3 scripts/wipe -e -d

# 导出 Notion 已处理数据（MCP 契约 JSON）
python3 scripts/export -o saligia -i -l 50 -O out.json

# 每小时增量同步一次
0 * * * * cd /path/to/TAPD && /usr/bin/env -S bash -lc 'source .env && python3 scripts/pull >> logs/sync.log 2>&1'
```
- macOS 也可用 launchd；或部署到服务器以获得更稳定的定时环境。

---

## 11. 测试与验收
- 单元测试：
  - 映射函数（TAPD→Notion）
  - 幂等 upsert、字段清洗、速率限制与退避
- 集成测试：
  - 沙箱/测试项目下跑一次全量与增量；
  - 人工抽样核对字段与链接可用；
- 验收标准：
  - 指定范围内的 TAPD 需求都能在 Notion 中找到对应记录；
  - 再次运行同步不产生重复记录；
  - 分析/功能点生成位置正确，且可人工覆盖。

---

## 12. 风险与应对
- API 访问限制/频控：实现指数退避与队列批处理；
- 自定义字段变更：将字段映射表外置配置，可热更新；
- 人员映射：不做强绑定，先以文本呈现；
- 长文本与富文本：Notion 页面正文分块写入，避免单请求超长；
- 时区与时间精度：统一 `Asia/Shanghai`，存原始时间戳便于追溯；
- 双向编辑冲突：先单向，以 TAPD 为准；如需回写，必须引入“锁/版本戳”。

---

## 附：开发快捷命令与依赖锁定

- 快速启动（Makefile）：
```
make setup              # 创建 .venv 并安装 requirements.txt
make pull ARGS="-e"     # 执行写入的拉取，同步作用域由 .env/参数决定
make update             # 更新（dry-run）；传入 ARGS 覆盖，如 ARGS="-e -N -l 100"
```

- 依赖锁定（pip-tools）：
```
# 顶层依赖维护在 requirements.in
make lock               # 生成锁定的 requirements.txt（含哈希）
```
说明：运行 `make lock` 需要网络并会安装 pip-tools 到本地 venv。开发依赖可在 requirements-dev.in 中维护（包含 pip-tools/pytest 等）。

## 13. 实施里程碑（建议）
- M0 准备（本阶段）
  - 申请 TAPD API 凭证，确定工作区与测试项目
  - 创建 Notion Integration 与数据库，建立字段
  - 整理字段映射表与示例数据
- M1 基础同步（TAPD → Notion）
  - 拉取需求列表/详情，完成 upsert 与幂等
  - 打通分页、速率限制与错误处理
- M2 增量与稳定性
  - last_sync 状态与失败重试；完善日志	on
  - 完成字段清洗与自定义字段对接
- M3 分析与功能点
  - 规则提取 + 人工确认流程；
  - 可选接入 LLM 与“确认版本”机制
- M4 交付与运维
  - 定时任务/部署脚本；运行手册；权限与密钥管理

---

## 14. 立即待办（行动清单）
- [ ] 你方提供/确认：
  - [ ] TAPD API 凭证（AppKey/Secret 或等效）与测试工作区 ID
  - [ ] 目标需求对象与字段（含自定义字段）
  - [ ] Notion Integration Token 与目标数据库（若未建，按第 3 节字段创建）
- [ ] 我方准备：
  - [ ] `.env.example` 与配置模板
  - [ ] 字段映射表草案与约定
  - [ ] CLI 与目录骨架（不含实现）

---

## 15. 参考与备注
- TAPD Open API：需登录企业开放平台查看官方文档（鉴权、签名、分页、对象模型）。
- Notion API：官方 SDK 与速率限制说明，需将数据库共享给 Integration。
- 后续可扩展：

---

## 16. TODO（图片分析与自动化引用）
- 图片分析（V1 已实现基础版）
  - [x] 提取 HTML 描述中的图片 URL，并在 Notion 页面中原样展示（公网 http/https）
  - [x] 轻量分析：格式、尺寸、近似平均色；结果写入“图片分析”区块
  - [ ] 语义描述（Image Captioning）：接入视觉大模型（可选 OpenAI/Azure/本地模型），生成一句话摘要与关键标签
  - [ ] OCR：识别图片内文字，用于搜索与引用
  - [ ] 敏感信息检测：标注水印/隐私字段
  - [ ] 缓存与重试：按图片 URL 做本地缓存（data/cache），避免重复下载
  - [ ] 相对地址补全：支持 TAPD 私有域地址（需要确认 TAPD 静态资源基址）
  - [ ] 配置化开关/限额：最大图片数、下载上限、超时、允许来源域

- MCP 自动化引用（规划）
  - [ ] 定义 Notion → MCP 的数据契约（字段、块结构、图片元数据 schema）
  - [ ] 提供只读 API/脚本，按过滤条件导出结构化 JSON（负责人、迭代、模块等）
  - [ ] 提供增量游标与快照导出（便于其他终端增量拉取）
  - [ ] 错误/空值处理策略（无图片/无描述时的占位与提示）
  - Webhook/实时同步；
  - 双向同步（Notion 修改回写 TAPD，需谨慎）；
  - 关联 Bug/任务/迭代看板，或输出周报/日报。
