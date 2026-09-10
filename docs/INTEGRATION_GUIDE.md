# 首次接入指南

这份指南面向刚克隆仓库、准备把流程接到自己 Codex 和生成 Provider 的使用者。先验证本地状态机，再逐步打开真实生成，避免把安装、流程和付费 Provider 问题混在一起。

## 接入完成的标准

1. Python 状态机测试通过；
2. 前端安装并构建通过；
3. Codex 能发现 `$manga-orchestrator`；
4. 至少创建一个试制项目和剧集，并能读取 `status` / `next-action`；
5. 要使用的图片或视频 Provider 能返回真实文件，或者明确保持未配置并接受流程在该处阻塞。

## 1. 准备运行环境

必需：Python 3.9+、Node.js 20+、npm，以及能在仓库根目录工作的 Codex Desktop、Codex CLI 或 IDE 扩展。建议安装 `ffprobe`，用于更严格的视频校验。

先运行不会触发生成服务的检查：

```bash
python3 --version
node --version
npm --version

python3 -m unittest discover -s backend/tests -v
cd frontend
npm install
npm run build
cd ..
```

Python 测试失败时先修状态机，不要继续接 Provider。

## 2. 让 Codex 发现核心 Skill

仓库已经把主控 Skill 放在：

```text
.agents/skills/manga-orchestrator/
├── SKILL.md
└── agents/openai.yaml
```

从仓库根目录打开 Codex。Codex 会扫描仓库级 `.agents/skills`；也可以用 `$manga-orchestrator` 显式调用。官方说明见 [Build skills](https://developers.openai.com/codex/skills)。

首次建议发送：

```text
$manga-orchestrator 做首次接入检查。只检查本地依赖、状态机、Skill 和 Provider 可用性，不生成图片或视频。
```

如果列表里没有该 Skill：

1. 确认当前工作目录位于本仓库内；
2. 确认 `.agents/skills/manga-orchestrator/SKILL.md` 存在；
3. 重启当前 Codex 会话；
4. 不要把 `backend/skills/stages` 复制到个人 Skill 目录。这些是状态机按阶段注入的内部 Skill。

## 3. 理解两个 Skill 层

`.agents/skills/manga-orchestrator/SKILL.md` 是日常入口，负责读取状态、执行一个合法流程动作、取得唯一 `next-action`、调度可见子 Agent，并把 JSON 结果交回状态机。

`backend/skills/stages/*/SKILL.md` 分别约束剧本设计、资产规划、定妆和剪辑交付。`backend/workflow/context.py` 会在对应阶段把它们写入子 Agent payload，普通使用者不需要单独调用。

完整映射见 [Skill 与流程合同](SKILLS_AND_WORKFLOW.md)。

仓库还在 `.codex/agents/` 定义两个受限角色：

- `manga-stage-producer`：完成一个文字生产节点，只返回 schema JSON；
- `manga-storyboard-reviewer`：只读检查故事板图片，返回 barrier review JSON。

首次接入时确认这两个 TOML 文件仍在仓库中。删除它们会让主控无法按预期角色委派。

## 4. 选择 Provider 接入方式

### 文字与审查

文字阶段由当前 Codex 会话调度原生可见子 Agent。Python 后端不读取 OpenAI API key，也不启动后台 `codex exec`。若当前环境不支持原生子 Agent，流程会停在 `waiting_agent`，不会伪造结果。

### 图片

默认图片 Provider 是 `codex_imagegen`，要求本机存在已登录且支持图片生成的 Codex CLI：

```bash
codex --version
codex login status
codex features list
```

也可以接自定义图片 CLI：

```bash
export JIMENG_IMAGE_CLI=/absolute/path/to/your-image-cli
export JIMENG_IMAGE_ARGS_JSON='["--prompt-file","{prompt_file}","--output","{output}","--asset-type","{asset_type}"]'
```

模板必须消费 `{prompt_file}`、`{output}`、`{asset_type}`，并在 `{output}` 写入真实 PNG。切换某集：

```bash
python3 scripts/workflow.py --workspace workspace set-image-provider \
  --project demo --episode ep01 --provider jimeng_image_cli
```

### 视频

视频适配器优先使用 `JIMENG_VIDEO_CLI`，其次查找 PATH 中的 `dreamina`。使用通用 CLI 时：

```bash
export JIMENG_VIDEO_CLI=/absolute/path/to/your-video-cli
export JIMENG_VIDEO_ARGS_JSON='["--prompt-file","{prompt_file}","--references-file","{references_file}","--duration","{duration_sec}","--ratio","{aspect_ratio}","--model","{model_version}","--session","{session_id}","--output","{output}"]'
export JIMENG_VIDEO_RESOLUTION=720p
export JIMENG_VIDEO_POLL_SEC=600
```

参数模板必须包含：`{prompt_file}`、`{references_file}`、`{duration_sec}`、`{aspect_ratio}`、`{model_version}`、`{session_id}`、`{output}`。CLI 必须在 `{output}` 写入真实 MP4；退出码为 0 但没有可验证文件仍算失败。

仓库里的 `fast` / `mini` 是当前即梦适配器的逻辑名。接其他视频服务时，应在 `backend/runners/video.py` 替换模型映射与返回解析，保留 `VideoRequest`、人工确认和 revision fingerprint 合同。

创建剧集时会生成剧集级数字 sessionId。若 Provider 要求平台已有 sessionId，请在第一条视频生成前设置：

```bash
python3 scripts/workflow.py --workspace workspace set-video-session \
  --project demo --episode ep01 --session-id <provider-session-id>
```

首条视频生成后该值锁定。

## 5. 创建最小试制剧集

```bash
python3 scripts/workflow.py --workspace workspace create-project \
  --project demo --name "Demo"

python3 scripts/workflow.py --workspace workspace create-episode \
  --project demo --episode ep01 --name "First Shot" \
  --topic "雨夜屋檐下，两位旧友重逢" \
  --hook "一句称呼揭示两人的真实关系" \
  --duration 8 --style "写实电影感" --aspect-ratio 16:9 \
  --production-mode dialogue_direct --video-model fast

python3 scripts/workflow.py --workspace workspace status \
  --project demo --episode ep01
```

已有完整剧本时使用 `--script-file`。原文保存到 `inputs/original_script.md`，第一阶段 `script.md` 必须逐字一致，只允许按视频生成单元机械拆分。

需要故事板时显式使用：

```text
--production-mode storyboard --storyboard-decision yes
```

默认 `dialogue_direct` 跳过故事板绑定和出图，但仍执行资产规划、定妆、视频 Prompt 锁定和视频参考绑定。

## 6. 在 Codex 中推进

```text
$manga-orchestrator 继续制作 demo 的 ep01。
```

主控循环：

```text
status
  → advance 或指定范围 regenerate
  → next-action
  → 原生子 Agent 返回 schema JSON
  → submit-agent-result
  → 下一状态
```

它会在这些边界停下：`waiting_agent`、`waiting_confirmation`、`blocked`、`paused` 或 `done`。

## 7. 处理两个人工闸门

资产规划后，系统先展示复用清单，确认前不开始定妆出图：

```bash
python3 scripts/workflow.py --workspace workspace confirm-asset-reuse \
  --project demo --episode ep01 --reuse-all
```

也可使用 `--generate-all` 或 `--reuse-ids id1,id2`。

视频必须由当前 Codex 对话确认当前镜头的 revision fingerprint：

```bash
python3 scripts/workflow.py --workspace workspace confirm-video \
  --project demo --episode ep01 --shot 1
```

多镜批量确认：

```bash
python3 scripts/workflow.py --workspace workspace confirm-video-batch \
  --project demo --episode ep01 --shots 1,2 --max-concurrency 2
```

这些命令会调用真实视频 Provider。先让 Codex 汇总 Prompt、参考图、时长、模型和 fingerprint，再由用户明确确认。

## 8. 启动工作台并验证反馈闭环

```bash
python3 scripts/start_workbench.py --open
```

检查：

1. 首页能看到 `demo / ep01`；
2. 九阶段状态与 CLI `status` 一致；
3. 已生成资产能预览；
4. 新增 suggestion 后 `annotations.pending_count` 增加；
5. Codex 只在 `pending_count > 0` 时读取增量标注；
6. redo 会绑定 Repair Plan，不被直接静默关闭。

## 常见阻塞

- `waiting_agent` 不动：运行 `next-action`，在支持原生子 Agent 的 Codex 环境中重试；不要手写成功 JSON。
- 图片 Provider unavailable：检查 Codex 登录/图片功能，或自定义 CLI 的路径和占位符。
- 视频 session/model/confirmation 错误：检查本集模型、session、绑定文件和 fingerprint；上游变化会使旧确认失效。
- 工作台没有剧集：确认启动器和 CLI 使用同一个 `--workspace` 绝对路径。

## 二次开发入口

| 能力 | 入口 |
|---|---|
| 阶段顺序与依赖 | `backend/core/flow.py` |
| 状态与字段校验 | `backend/core/status.py` |
| 阶段上下文与 Skill 注入 | `backend/workflow/context.py` |
| 结果文件合同 | `backend/workflow/output.py` |
| 图片 Provider | `backend/runners/image.py` |
| 视频 Provider | `backend/runners/video.py` |
| 主流程 CLI | `scripts/workflow.py` |
| 工作台 API | `backend/api.py` |
| 工作台页面 | `frontend/app/` |

接新 Provider 时至少测试：未授权拒绝、无效文件拒绝、成功真实文件、超时/失败进入 `blocked`。
