# AGENTS.md — Codex 视频工作流规则

## 1. 读取范围

- 修改状态机、阶段合同或生产资产前，先完整阅读 `PROJECT_STATE.md`。
- 处理首次安装或 Provider 接入时，先阅读 `docs/INTEGRATION_GUIDE.md`。
- 只修改文档或前端展示时，读取与任务直接相关的文件即可。
- 不新增 MiniMax、mock fallback、Supabase、runner 选择器或旧 12-stage 结构。

## 2. 付费与外部生成

- 文字与图片生成由用户的“继续制作”或“重新生成”动作直接授权，不再额外询问；重建与验证过程中可直接调用 Codex 和图片 provider。
- 只有视频生成需要单独人工确认，并且必须由当前 Codex 对话明确确认当前 revision fingerprint。
- 代码检查、测试、dry-run、整理文件和文档无需确认。

## 3. 状态和资产

- `status.json` 是唯一业务状态来源，事件写入 JSONL。
- 流程代码决定推进；模型不得修改状态或跳过依赖。
- 工作台只读取状态、事件和资产；它唯一允许的写操作是新增标注，不能创建剧集或推进流程。
- Codex 使用 `python3 scripts/workflow.py` 创建、检查和推进剧集，不直接编辑 `status.json`。
- 每轮处理某个剧集时先运行 `scripts/workflow.py status`。只有 `annotations.pending_count > 0` 时才运行 `scripts/workflow.py annotations` 读取增量标注；没有增量时不得读取标注目录。
- 只有标注对应的修改已成功落盘，或已明确判定无需修改后，才能运行 `scripts/workflow.py handle-annotations` 推进游标。
- `suggestion` 可以在明确判断后处理；`redo` 不得直接用 `handle-annotations` 静默关闭，必须绑定 Repair Plan，并在新资产和审查通过后由流程关闭。
- 第一阶段是唯一内容权威：一次性锁定剧本、详细分镜、故事板执行 Prompt 和最终视频 Prompt。后续阶段不得根据故事板、定妆图或审查结果改写这些内容。
- 用户已经提供完整剧本与分镜时，第一阶段只按视频生成单元机械拆分；不得做语义审查、扩写、重排或时长再分配。一个生成单元可以包含多个内部短镜头。
- 第二阶段只规划定妆资产数量、required/optional 和逐单元绑定关系，不生成定妆 Prompt 或图片。必需资产失败必须阻塞；可选资产失败可以省略，但不得使用占位图，且后续不得绑定。
- 故障只影响部分故事板时，必须用 `regenerate --stage storyboard_generation --shots <镜号>` 精确重做；不得因单张或少量图的问题重做整个阶段。故事板错误不得反向修改第一阶段 Prompt。
- 跨阶段仍由主 Agent 与状态机串联；`parallel_tasks` 只允许发生在 Stage 内，子 Agent/Task 不得推进 Stage。
- 不允许用 mock、占位图、渐变图、文本文件或复制旧图冒充真实资产。
- 所有自动测试使用临时目录；验证结束后不得在正式 workspace 留下测试剧集。

## 4. 内容自查

- 由第一阶段新创作剧本、分镜或视频 Prompt 时，在锁定前完成一次内容自查。用户已经提供完整版本时只检查拆分、编号、时长与文件合同，不做内容或导演审查。

## 5. 代码原则

- 优先实现小而明确的模块和可测试合同，不建立通用 Agent 平台。
- 生产失败必须诚实进入 `blocked`；禁止静默降级。
- 删除不再使用的兼容层、按钮和字段，不保留“以后也许有用”的空壳。

## 6. Provider 接入

- 可以替换图片或视频 Provider，但必须保留授权检查、真实文件校验、失败阻塞和视频 revision fingerprint。
- Provider 返回成功但没有有效本地文件时仍视为失败。
- Provider 的密钥、Cookie、session、账号文件和真实运行数据不得提交到 Git。
