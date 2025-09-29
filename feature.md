# TestFlow 后续补充清单

- [ ] 提供测试人员名称 ↔ 邮箱映射完整表，更新 `config/testers.json`。
- [ ] 确认 TAPD 中“测试人员”字段命名，必要时补充自定义字段解析规则。
- [ ] 确认/调整 LLM Prompt 模板，可覆盖默认提示并存放在 `config/testflow_prompt.md`。
- [ ] 确认邮件发送 SMTP 配置（`SMTP_HOST` 等）；如需真实发送请在 `.env` 中填写并将 `TESTFLOW_MAIL_DRY_RUN=0`。
- [ ] 评审自动生成的 XMind 结构是否满足团队习惯（节点层级、备注格式等）。
- [ ] 约定运行排程（手动 or cron），以及与主同步流程的衔接策略。

---

## 2024-XX-XX LLM 测试记录
- 环境：本地运行 `ollama serve`，模型 `gpt-oss:20b`，`OLLAMA_HOST` 可自定义端口，更新 `.env` 后需在终端重新加载。
- 在沙箱环境触发 `src/cli.py testflow` 时，由于无法解析 `api.tapd.cn`，确认需要在拥有外网访问权的本机执行真实流程。
- 邮件通道：需向企业邮箱管理员获取 `SMTP_HOST/PORT/USER/PASSWORD` 与授权码，`TESTFLOW_MAIL_DRY_RUN=0` 才会实际发送；若暂不可行，保持 dry-run。
- 替代方案备选：企业微信机器人、企业微信应用消息或共享网盘链接广播，视后续风控要求选择。
- 下一步：在本地环境配置好 SMTP/测试人员映射后，用真实需求跑 `testflow --execute` 生成 XMind，手动验证用例质量，再决定是否回写 Notion 或拓展推送方式。
