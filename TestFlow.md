# TestFlow 概念设计与可行性分析

## 目标概述
- 在现有 TAPD → Notion 同步链路基础上新增“测试流”能力：
  1. 从 TAPD 拉取需求后补充测试人员信息；
  2. 调用本地 Ollama 模型 `gpt-oss:20b` 为每个需求生成测试用例结构；
  3. 将用例按测试人员分组并导出为 XMind 文件；
  4. 根据测试人员邮箱枚举自动投递邮件附件，减轻手工编写与分发成本。

## 当前进展（2024-XX-XX）
- 新增 `testflow` CLI 命令（`python3 scripts/testflow` 同等入口），支持 dry-run、`--execute` 落盘、`--send-mail` 发送邮件，均带风险确认。
- 创建 `src/testflow/` 模块簇：
  - `models.py` 定义用例、联系人、流程选项与结果；
  - `testers.py` 负责读取 `config/testers.json`，支持别名匹配、默认联系人回退；
  - `llm.py` 对 Ollama `gpt-oss:20b` 发起请求，解析 JSON 结果并提供失败兜底；
  - `exporter.py` 将用例按需求 → 用例分层写入 `.xmind`（Zip + `content.json` 等最小集）；
  - `mailer.py` 基于 `smtplib`，默认 dry-run 打印收件人与附件路径；
  - `service.py` 串联 TAPD 拉取、测试人员识别、LLM 生成、XMind 导出、邮件下发。
- `.env.example`、`.gitignore`、`config/testers.json` 等配置位已补齐；默认将生成文件写入 `data/testflow/` 并忽略版本控制。
- 输出补充文档 `feature.md` 记录用户需提供的映射、SMTP 与 prompt 细节。

## 后续扩展点
- Notion `mapper` 暂未写入「测试人员」属性，如需同步至数据库需扩展映射层。
- 需确认 TAPD 自定义字段中“测试人员”命名/结构，并在 `extract_story_testers` 中补充特定键。
- LLM prompt 模板可外置（`config/testflow_prompt.md`）并由 QA 团队校准。
- SMTP 配置仍为 dry-run，需要运维提供可用账号后再关闭 `TESTFLOW_MAIL_DRY_RUN`。
- 待评审导出的 XMind 层级/备注格式是否满足协同需求。

## 模块说明
1. **测试人员管理（`testflow/testers.py`）**
   - 读取 `config/testers.json` 并构造 `TesterRegistry`，支持别名、模块标签。
   - 暂无映射时回退至默认联系人（已临时指向 `saligia28@126.com`），以保证流程可跑通。

2. **LLM 用例生成（`testflow/llm.py`）**
   - 以需求描述、验收标准和候选测试人员拼接 prompt，调用本地 Ollama `gpt-oss:20b`。
   - 解析 JSON `test_cases` 列表，不合规时自动降级为手工模板。
   - 支持外置 prompt（`TESTFLOW_PROMPT_PATH`）。

3. **导出能力（`testflow/exporter.py`）**
   - 将测试人员的全部用例整理为 `content.json` + `metadata.json` + `manifest.json` 压缩成 `.xmind`。
   - 结构：根节点 = 测试人员；次级 = 需求；叶子 = 用例，备注中写入步骤/期望。

4. **邮件投递（`testflow/mailer.py`）**
   - 基于标准库 `smtplib`，默认 dry-run 打印发送计划。
   - 支持 TLS/SSL，收件人、主题、附件统一由 `MailJob` 承载。

5. **服务编排（`testflow/service.py` & `cli.py`）**
   - 负责 TAPD 拉取、owner 过滤、测试人员解析、调用 LLM、生成附件、（可选）发送邮件。
   - CLI 支持 `--execute`、`--send-mail`、`--ack`、`--ack-mail` 与 `--limit` 等参数便于分批调试。

## 关键流程拆解
1. **需求拉取阶段**
   - 输入：TAPD 过滤条件（默认当前迭代+负责人）。
   - 输出：`Story` 对象列表（含 `id`, `name`, `description`, `owner`, `module` 等）。
   - 扩展：为每条需求附加 `testers`（列表）。

2. **语义提炼阶段**
   - Prompt: 需求描述 + 验收标准 + 风险点。强调输出 JSON 结构 {tester: [...cases...]}，每个 case 含标题/前置/步骤/期望。
   - 添加守护：
     - 超时重试（3 次指数退避）。
     - 结果校验（JSON Schema）。
     - 失败 fallback：产出固定模板。

3. **分发与文件生成**
   - 聚合：对同一个测试人员合并所有需求的测试用例，按模块或需求标题建立第一层节点。
   - XMind 结构：
     - 根节点：`测试人员 - 日期`。
     - 二级：需求标题。
     - 三级：测试用例（含步骤、期望）。
   - 文件存储：默认 `out/xmind/YYYYMMDD/张三.xmind`。

4. **邮件发送**
   - 校验：仅当配置中存在有效邮箱且启用 `--send`。
   - 内容：
     - 邮件标题：`[测试用例] {日期} {测试人员}`。
     - 正文：概览用例数量、需求列表。
     - 附件：对应 `.xmind`。
   - 失败：记录日志并返回状态供上层判断是否重试。

## 新需求 vs. 现有需求评估
- 直接嵌入现有 `sync`：
  - 优点：数据来源一致，可在 Notion 页内记录生成的测试用例链接。
  - 风险：同步流程更重，Notion 写入与邮件发送耦合，失败影响主流程。
- 新增独立 TestFlow：
  - 推荐方案。命令/脚本独立，复用 `tapd_client` 与 `mapper`；
  - 可在生成后 optional 调用现有 `sync` 写回测试人员/附件链接；
  - 与 Notion 写入解耦，便于单独跑批或调度。

## 配置与安全注意事项
- `.env` 新增：
  - `OLLAMA_HOST=http://127.0.0.1:11434`
  - `OLLAMA_MODEL=gpt-oss:20b`
  - SMTP 相关凭证（建议使用应用专用密码）。
- 生成的 `.xmind`/`zip` 属于敏感数据，默认写入 `data/testflow/`，并在 `.gitignore` 中忽略。
- 邮件发送前应提供风险确认（与现有 `--ack` 风格一致）。

## 下一步建议
1. 补全测试人员 ↔ 邮箱映射并确认 TAPD 中字段命名，必要时在 `extract_story_testers` 增加自定义键。
2. 与 QA 团队核对 prompt 模板内容、XMind 节点展示形式，必要时在 `config/testflow_prompt.md` 中自定义。
3. 配置 SMTP（或保持 dry-run），提供可用账号后再关闭 `TESTFLOW_MAIL_DRY_RUN` 并试发。
4. 选取真实需求跑通流程，验证 LLM 输出质量，视结果决定是否增加后处理规则。
5. 是否需要将测试人员信息同步回 Notion（`mapper.map_story_to_notion_properties`）需与产品确认。


# TestFlow 后续补充清单

- [ ] 提供测试人员名称 ↔ 邮箱映射完整表，更新 `config/testers.json`。
- [ ] 确认 TAPD 中“测试人员”字段命名，必要时补充自定义字段解析规则。
- [ ] 确认/调整 LLM Prompt 模板，可覆盖默认提示并存放在 `config/testflow_prompt.md`。
- [ ] 确认邮件发送 SMTP 配置（`SMTP_HOST` 等）；如需真实发送请在 `.env` 中填写并将 `TESTFLOW_MAIL_DRY_RUN=0`。
- [ ] 评审自动生成的 XMind 结构是否满足团队习惯（节点层级、备注格式等）。
- [ ] 约定运行排程（手动 or cron），以及与主同步流程的衔接策略。
