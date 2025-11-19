# Story-ID 拉取终端无输出的原因与改进方案

## 现状复盘
- `scripts/pull` 通过 `_pipeline.run_step` 只输出“开始/结束”两行；`run_step` 内部不会转发子步骤日志，导致长时间没有进度回显。
- `run_sync(..., story_ids=[...])` 会开启 `restrict_to_ids`，`iterate_source_stories()` 逐条调用 `tapd.get_story`，但这一阶段完全没有 `print`，外部也只能等全部请求结束。
- 把全部 story 放入 `notion_candidates` 后才开始 Notion upsert，日志只在这里出现（如 `would create` / `created`），因此终端表现成“长时间沉默→一次性打印结束”。
- 这个行为是实现细节造成的，并非 TAPD 接口一次性返回所有结果。

## 改进方向
1. **增加实时进度日志**
   - 在 `iterate_source_stories()` 中，成功拿到单条故事后立即打印一行，例如：`print(f"[sync] fetched id={sid} ({idx}/{total})")`。若未知总数，可只打印已完成数量。
   - 在 Notion 更新循环前，再输出 `print(f"[sync] enqueue story id={sid}")`，帮助确认队列长度。
2. **保持 run_step 包装但补充 flush**
   - `run_step` 内加 `sys.stdout.flush()`（或 logging）以确保缓冲及时刷新，避免输出被缓冲延迟。
3. **可选：进度条/速率统计**
   - 若 story 数量多，可在循环中每处理 N 条打印耗时与平均速率，便于定位慢请求。

## 实施要点
- 所有新增日志使用 `[sync]` 前缀，与现有输出风格一致；避免在 dry-run/execute 行为上产生副作用。
- 日志打印应位于 try/except 之后，保证失败请求也能输出 `fetch by id failed id=...`，便于排障。
- 修改后需在本地通过 `python3 scripts/pull --story-ids <几个ID> --ack "..."` 验证：终端应看到逐条 ID 日志 + 最终汇总。

## 更细粒度进度输出方案
### 可行性分析
- 拉取、分析、写入的关键路径都在 `services/sync/service.py` 内部，且调用链可控，无需跨线程或监听异步任务；因此可以在同一函数内插入带计数的进度日志。
- 现有 `run_sync` 在 story-ID 模式下能提前获知总数（`len(focus_ids)`），迭代模式虽然不知道上限，但仍可输出“已处理 N 条”。
- “分析需求”主要对应 `generate_testflow_for_stories` 与 `enrich_story_with_extras` 等 CPU 密集步骤，函数调用点单一，插入前置/后置日志即可满足需求。
- 为避免默认模式过于噪声，可新增 `--progress`（或 `--verbose-progress`）开关，只有显式开启时才打印阶段日志；实现成本低，阻力小。

### 方案设计
1. **CLI/服务层参数**
   - 在 `scripts/pull` 添加 `--progress` flag，默认 False；传递给 `run_sync` / `run_sync_by_modules` / `run_update_all`（可选）。
   - `run_sync` 新增形参 `progress: bool = False`，并在需要时构造 `ProgressReporter`。
2. **ProgressReporter 辅助类**
   - 在 `services/sync/utils.py` 或新文件中实现一个轻量工具，提供 `stage(name, idx=None, total=None, tapd_id=None)` 方法，统一格式化输出，例如：`[sync] progress stage=fetch idx=3/15 tapd_id=12345`。
   - 内部跟踪计数，未知 total 时仅打印 `idx`。
3. **日志插桩点**
   - **Fetch 阶段**：`iterate_source_stories()` 在成功 `yield` 前调用 `progress.stage("fetch", idx, total, sid)`；失败时沿用现有 `fetch by id failed` 日志。
   - **分析阶段**：
     - 在调用 `generate_testflow_for_stories` 前打印 `stage=analyze start stories=len(all_stories)`，结束后打印完成信息及耗时。
     - 在 `enrich_story_with_extras` 前后分别输出 `stage=enrich` 与 `stage=extras-ready`，以便判断卡顿在 TAPD 附加接口还是本地分析。
   - **Notion upsert 阶段**：进入 `for story in notion_candidates` 时，先输出 `stage=notion idx=... tapd_id=...`，然后在 `create/upsert/skip` 分支的现有日志前补充 `stage=notion-write result=create/upsert/skip`。
4. **可选扩展**
   - 在 `_pipeline.run_step` 里调用 `sys.stdout.flush()`，确保阶段日志实时显示。
   - 为 `run_update_all` 复用相同 `ProgressReporter`，以覆盖拉取后立即更新的场景。

### 验证步骤
- 本地执行 `python3 scripts/pull --story-ids 1,2,3 --progress -e --ack "..."`，观察输出是否出现 fetch→analyze→notion 的阶段提示。
- 模拟普通增量拉取（不传 `--story-ids`），确认未知总数时也能看到 `idx` 累加并且日志不会淹没核心信息。
- 在未传 `--progress` 时只保留原有日志，确保默认体验不变。

## 推荐实施顺序（建议）
1. 在 `scripts/pull` / `run_sync` 增加 `--progress` 参数链路，并实现 `ProgressReporter`，先确保 fetch → analyze → notion 的阶段日志可控输出。
2. 依照“验证步骤”逐项执行：先跑 `--story-ids` 的 execute 流程，再跑一次常规增量；确认日志粒度和默认噪声都符合预期。
3. 评估是否需要为 `run_update_all` 引入同样的进度日志（若拉取后自动更新耗时长，建议顺手加上），最后再考虑扩展到其它 CLI（如 `scripts/update`）。
