# AI 漫剧生产系统 · Codex 调度版

> 更新时间：2026-09-09  
> 当前目标：用最小实现完成“Codex 制作、状态机裁决、工作台展示与标注”。

## 1. 当前实现

项目已经完成初版主从反转：

- Codex 是唯一面向用户的创建与制作入口。
- `scripts/workflow.py` 是 Codex 调用确定性状态机的本地命令。
- 工作台不再创建项目/剧集，不再启动 Codex、图片或视频 provider，也没有继续、暂停、重跑、模型切换或视频确认按钮。
- 工作台按“项目 → 剧集”分类展示；剧集页顶部展示九阶段状态机，下面按纵向信息流展示剧本、定妆图、故事板、视频等核心产出，Prompt 只通过文件夹入口查看。
- 工作台主制作区按视频生成单元归档：同一单元的锁定故事板 Prompt、锁定视频 Prompt、两类参考绑定、故事板成图、绑定定妆与最终视频集中展示，单元之间用标签切换。
- 工作台正式合同只支持 `flow_version=2.0` 新剧集。唯一例外是保留的 `meituan_sunce/s01e01（美团孙策）`：通过专用只读投影视图兼容浏览，不迁移状态、不允许从工作台写反馈，也不为其他 v1 剧集提供通用兼容层。
- 故事板采用灰度导演草稿，使用少量固定含义的彩色控制标记表达运动轨迹、视线、接触点、受力方向和英雄帧；角色、场景、道具定妆图分别承担身份、环境和关键道具的色彩/材质/光线权威。
- 工作台唯一允许的写操作是新增标注。
- 标注正文独立保存；`status.json` 只保存轻量增量游标。Codex 没有发现增量时不读取标注目录。
- 已加入 `Episode → Stage → Task → Asset Revision → Annotation` 数据合同、Repair Plan、不可变旧版本和标注生命周期。
- 文字生成由 Codex 可见子 Agent 在产出内完成一次硬错误自检；故事板只在全组图片就绪后执行一次精简的整组审查，检查每镜内部精度与定妆引用，不强制相邻镜头首尾匹配。
- 故事板 Prompt 现在必须声明逐镜闭合 roster、首末帧锁定和动作因果锁；参考选择器只上传 roster 内的 active 定妆图，缺少显式锁定时直接阻塞，不再用全剧角色或自由文本猜测补齐。
- 第一阶段一次性完成并锁定剧本、详细分镜、故事板执行 Prompt、视频生成单元和最终视频 Prompt。用户已经提供完整剧本与分镜时，第一阶段只机械拆分和编号，不做语义审查、扩写、重排或时长再分配。
- “视频生成单元”和单元内部剪辑镜头已分离：一个10秒生成单元可以保留多个1–2秒内部硬切镜头，不会被拆成多次 Provider 调用。
- 第二阶段只规划角色/场景/道具资产数量、required/optional 和逐单元绑定关系，不生成定妆 Prompt 或图片。场景与道具定妆合并为一个阶段。
- 第二阶段完成后、任何定妆出图前，状态机必须按稳定资产 ID 查询项目公共定妆库和前集已完成资产，展示“建议复用/新生成”清单并等待用户确认。确认结果与 `asset_plan.json` 指纹绑定，后续设计阶段必须执行该决策；复用项原字节复制已验证的设定、Prompt 和 PNG，不调用图片 Provider。
- 角色武器、念珠和标志性随身装备以角色定妆板为权威，默认不再规划独立道具；只有角色板不包含且镜头需要独立身份、交互或状态参考时例外。
- 原“连续分镜规划”和“故事板复核与视频提示词”均已删除。故事板版式、P01–Pnn、`storyboard_required` 和最终视频 Prompt 全部由第一阶段锁定；后续参考绑定只写 JSON 路径清单与 Prompt SHA-256。
- 必需定妆资产失败会阻塞；可选资产失败可直接省略，不生成占位图，也不会进入故事板或视频参考绑定。
- 故事板图片按镜头独立生成并登记；镜头之间不强制首尾姿态、道具状态或机位连续，跨镜变化交给剪辑和转场。
- 第一阶段支持接收用户原始剧本；创建时必须将原文保存到 `inputs/original_script.md` 并写入 `script_lock` 指纹。一旦存在 `creative_brief.provided_script`，`script.md` 必须逐字保持原文。对完整分镜输入只允许按视频生成单元机械拆分；不得做动作、台词、空间、道具或节奏的二次审查和改写。
- `storyboard_decision` 在开始前记录是否需要故事板，新剧集默认为 `no`。`dialogue_direct` 只跳过故事板参考绑定和故事板生成；第一阶段仍产出锁定视频 Prompt，资源规划和视频参考绑定照常执行。
- 状态机在 `status.json` 中只保存一条 revision 绑定的 `agent_request`。`waiting_agent` 时不持有剧集锁；子 Agent 回传 JSON 后才由状态机校验并推进。
- 每个大节点使用一次短生命周期子 Agent。节点完成后只保留完整产物和一条短 `handoff`；下一节点使用新上下文，不继承上一节点对话。
- 新增全局 Codex `SubagentStart` / `SubagentStop` 钩子：仅对白名单视频项目生效，且只有当前会话先读取目标剧集的 `workflow.py next-action`、状态机存在等待中的视频流程 Agent 请求时，才按项目/剧集隔离记录子 Agent 生命周期、节点标识、阶段/任务/revision、模型、耗时分解和结果上下文；普通对话静默跳过。钩子失败静默降级，不重试、不阻断主流程。工作台将钩子账本与 usage ledger 合并展示子 Agent 执行时间和 token 估算。
- 剧集进入 `done` 后自动整理文件存储：最终产物保持原 canonical 路径，中间 Prompt、故事板参考包和运行元数据移动到剧集目录的 `intermediate/`；从已完成剧集重做前会恢复中间输入，避免破坏既有状态机与最终产物 URL。
- 新剧集在 `story_design` 开始前必须先选择视频模型：`fast` 映射即梦 `seedance2.0fast_vip`，`mini` 映射即梦 `seedance2.0mini`；模型选择写入本集并锁定，不能在制作中途切换。视频生成审批按生成单元隔离；可用 `confirm-video-batch --shots 1,2,...` 并发提交，单集最多 2 个并发单元，`confirm-video --shot N` 仍可用于单镜精准重做。
- 每集视频还必须配置剧集级 `creative_brief.video_session_id`。它不再从全局环境变量读取；同一剧集的所有镜头调用都复用该值，多个剧集彼此隔离。sessionId 在首个视频镜头生成后锁定，且会参与逐镜审批 fingerprint。

底层仍保留九阶段状态机、原子状态写入、事件 JSONL、资产登记、依赖校验、原生可见子 Agent、图片 Provider 和即梦视频 CLI 合同。后端不再以 `codex exec` 启动文字生成或 Reviewer。

## 2. 已锁定的职责边界

### Codex 负责

- 与用户讨论创意、创建项目和剧集。
- 每轮先读取目标剧集的精简状态。
- 发现增量标注后读取标注文件，并判断应该继续、重做、解释还是忽略。
- 在用户说“继续制作”或“重新生成”后调用确定性工作流命令。
- 读取 `next-action`，用原生 `spawn_agent` 委派唯一待办；将其 schema JSON 通过 `submit-agent-result` 回传。
- 视频生成前单独向用户确认当前 revision。

主控只做四件事：读状态、读取 `next-action`、委派子 Agent、提交结果。主控不替子节点重做内容，也不把上一节点全文复制给下一节点。

### 流程代码负责

- 决定唯一合法的下一阶段。
- 校验依赖、输出文件名、数量、内容和真实资产。
- 原子写入 `status.json`，并登记资产和事件。
- 加锁、防并发、崩溃恢复、下游失效和视频 fingerprint。
- 失败时诚实进入 `blocked`。

状态机不做创意总结，只校验 handoff 字段、产物合同、revision 和依赖。

Codex 不得直接编辑 `status.json`，不得跳过依赖，也不得用假资产补成功。

### 工作台负责

- 扫描项目与剧集并分类展示。
- 读取 `status.json`、事件 JSONL 和活跃资产。
- 顶部展示完整九阶段状态机，下面只展示核心文本、定妆图、故事板和视频等最终产出。
- Prompt、Task 和阶段辅助文件不进入主展示区，只保留按阶段或全局的文件夹入口。
- 新增阶段级或资产级标注。
- 分开新增 `suggestion` 与 `redo`；展示 Task 进度、修复计划和资产版本。

除新增标注外，工作台不提供业务写接口。

## 3. 正式生产流程

| 顺序 | stage id | 页面名称 | 核心产物 |
|---|---|---|---|
| 1 | `story_design` | 剧本与分镜定稿 | 剧本、锁定分镜、`shot_plan`、`storyboard_plan`、逐单元最终视频 Prompt 与故事板 Prompt |
| 2 | `asset_planning` | 定妆资源规划 | `asset_plan.json`：资产数量、必要性和逐单元绑定计划 |
| 3 | `character_design` | 角色定妆 | 计划内角色设定、Prompt 和真实身份板 |
| 4 | `visual_design` | 场景与道具定妆 | 计划内场景/道具设定、Prompt 和真实定妆板 |
| 5 | `storyboard_binding` | 故事板参考绑定 | `shotNN_storyboard_references.json` |
| 6 | `storyboard_generation` | 多格故事板出图 | 严格按第一阶段锁定分镜生成故事板 |
| 7 | `video_binding` | 视频参考绑定 | `shotNN_video_references.json` 与锁定 Prompt 指纹 |
| 8 | `video_generation` | 视频生成 | 提交第一阶段锁定 Prompt，并在 provider-facing 文本最前追加剧名/剧集/镜头标识；用户逐单元确认 revision 后调用 |
| 9 | `edit_post` | 剪辑交付 | 剪辑、声音、字幕和交付方案 |

关键内容合同保持不变：

- 视频生成单元数由第一阶段锁定；每个生成单元内部可以包含多个短镜头。
- 新生成的单个视频镜头必须为 4–15 秒。
- 需要故事板的生成单元，其网格和 P01–Pnn 在第一阶段锁定；P01 为0秒，末格等于单元时长。后续不得重新规划。
- 最终视频 Prompt 正文由第一阶段直接锁定；实际提交给 Provider 时只允许在正文最前追加剧名、剧集和镜头标识，本地锁定文件与指纹不变；本地参考图保存在独立 JSON 中，后续不再转换或重写正文。
- 交付多段内容前检查镜号、总时长、角色关系、对白顺序、动作因果、空间、道具和风格连续性。
- 故事板底图为黑白或灰度导演草稿，不承担完整色彩和最终渲染；定妆图负责具体视觉外观。控制标记只作为模型可读的动作控制语言：青色实线箭头表示运动轨迹，黄色虚线表示视线，红色圆圈表示真实接触点，红色箭头表示受力方向，紫色小星标表示英雄帧。
- 控制标记必须少量、局部、无文字图例；视频 Prompt 必须要求模型把标记转换成自然动作，最终视频不得出现任何控制标记、故事板网格、标签或草稿线。
- 故事板、动作脊柱和视频 Prompt 必须共享同一动作状态来源；接触动作统一按“主体/工具 → 接触点 → 受力方向 → 结果状态”自查。
- 故事板出图按镜头建立独立参考清单：逐镜 Prompt 先声明闭合的 `[本镜可见角色]`、唯一 `[本镜场景]` 和 `[本镜关键道具]` roster，系统只选择其中实际 active 的角色/场景/道具定妆图；未列对象不得因存在于全剧资产中而混入本镜。P01 是镜头本地起始状态，不强制复制上一镜末帧；镜头之间的跳切、状态变化和转场由剪辑处理。Provider 单次最多接收 5 张参考图时，同一职责组的真实定妆 PNG 可原样缩放拼合成镜头专属参考包；合成包不是新设计，必须在清单、Prompt 和故事板资产元数据中保留每张源图的职责、资产 Task、路径、revision 及映射关系。禁止按资产顺序截断或静默丢弃参考图。故事板各镜头可并行出图，整组完成后再执行一次精简审查。
- 视频参考最多8张，只能来自 `asset_plan.json` 的逐单元绑定计划和当前故事板。`video_binding` 将锁定 Prompt 的单元可见角色与规划角色参考做闭环检查：可见角色疑似缺少绑定或存在非可见角色旧绑定时写入逐单元绑定清单和阶段交接，旧格式推断出的差异标为低置信度，供视频确认前提示用户，不因此阻塞流程。缺失必需资产仍会阻塞；缺失可选资产记录为 omitted 且不绑定。

## 4. Codex 本地命令

统一入口：

```bash
python3 scripts/workflow.py --workspace workspace <command>
```

常用命令：

```bash
# 查看项目与剧集
python3 scripts/workflow.py --workspace workspace list

# 每轮先读精简状态
python3 scripts/workflow.py --workspace workspace status \
  --project <project> --episode <episode>

# 只有 pending_count > 0 时读取
python3 scripts/workflow.py --workspace workspace annotations \
  --project <project> --episode <episode>

# 创建
python3 scripts/workflow.py --workspace workspace create-project \
  --project <project> --name <name>
python3 scripts/workflow.py --workspace workspace create-episode \
  --project <project> --episode <episode> --name <name> \
  --topic <topic> --hook <hook> --duration <seconds>

# 如果用户已提供原剧本，传入 UTF-8 文本文件；第一阶段的 script.md 将被原文锁定
python3 scripts/workflow.py --workspace workspace create-episode \
  --project <project> --episode <episode> --name <name> \
  --topic <topic> --hook <hook> --duration <seconds> --script-file <provided-script.md>

# 偏对话式剧本：仍完成道具定妆，之后直通最终视频 Prompt，不生成故事板
python3 scripts/workflow.py --workspace workspace create-episode \
  --project <project> --episode <episode> --name <name> \
  --topic <topic> --hook <hook> --duration <seconds> --production-mode dialogue_direct

# 默认不需要故事板；仍可在创建时显式选择 yes 或 pending
python3 scripts/workflow.py --workspace workspace set-storyboard-decision \
  --project <project> --episode <episode> --decision no

# 若剧本未明确视觉风格，创建后会停在风格询问，不使用默认风格
python3 scripts/workflow.py --workspace workspace set-style \
  --project <project> --episode <episode> --style 写实

# 若剧本未明确画幅，创建后会停在画幅询问，不使用默认画幅
python3 scripts/workflow.py --workspace workspace set-aspect-ratio \
  --project <project> --episode <episode> --aspect-ratio 16:9

# 用户明确要求后推进或重做
python3 scripts/workflow.py --workspace workspace advance \
  --project <project> --episode <episode>
python3 scripts/workflow.py --workspace workspace pause \
  --project <project> --episode <episode>
python3 scripts/workflow.py --workspace workspace regenerate \
  --project <project> --episode <episode> --stage <stage>

# 快速回退：先不带选择查看确认问题；确认后传 --keep-stages 或 --reset-all
python3 scripts/workflow.py --workspace workspace rollback \
  --project <project> --episode <episode> --from-stage storyboard_binding

# 视频阶段单镜增量制作：先查看确认问题，再显式指定范围；不会自动调用视频生成
python3 scripts/workflow.py --workspace workspace incremental-shot \
  --project <project> --episode <episode> --shot 3 \
  --notes "只修复本镜故事板执行" --scope storyboard --confirm

# 全局换画风：归档现有活跃资产，并从剧本与分镜设计重新开始
python3 scripts/workflow.py --workspace workspace restart-style \
  --project <project> --episode <episode> --style <style>

# 读取当前需要由 Codex 原生子 Agent 执行的唯一任务
python3 scripts/workflow.py --workspace workspace next-action \
  --project <project> --episode <episode>

# 子 Agent 返回 JSON 后，由状态机验证 request id、revision 和 schema 再推进
python3 scripts/workflow.py --workspace workspace submit-agent-result \
  --project <project> --episode <episode> \
  --request <request-id> --result-file <agent-result.json>

# 资产规划后先确认复用清单；未选中的候选将转为新生成
python3 scripts/workflow.py --workspace workspace confirm-asset-reuse \
  --project <project> --episode <episode> --reuse-all
python3 scripts/workflow.py --workspace workspace confirm-asset-reuse \
  --project <project> --episode <episode> \
  --reuse-ids char_example,scene_example

# 只重做有问题的故事板，其他图原样保留
python3 scripts/workflow.py --workspace workspace plan-repair \
  --project <project> --episode <episode> \
  --stage storyboard_generation --shots 2,5 \
  --annotations a_000003,a_000004 --repair-notes <repair-notes.json>

python3 scripts/workflow.py --workspace workspace regenerate \
  --project <project> --episode <episode> \
  --stage storyboard_generation --repair-plan rp_000001

# 标注对应修改成功后推进游标
python3 scripts/workflow.py --workspace workspace handle-annotations \
  --project <project> --episode <episode> --through <revision>

# 单镜确认并生成；需要并发时使用下面的批量命令
python3 scripts/workflow.py --workspace workspace confirm-video \
  --project <project> --episode <episode> --shot <shot-number>

# 并发确认并生成多个视频生成单元；单集最多 2 个并发单元
python3 scripts/workflow.py --workspace workspace confirm-video-batch \
  --project <project> --episode <episode> --shots 1,2,3 --max-concurrency 2

# 新剧集开始前选择本集视频模型；fast=seedance2.0fast_vip，mini=seedance2.0mini
python3 scripts/workflow.py --workspace workspace set-video-model \
  --project <project> --episode <episode> --model fast

# 配置本集所有视频镜头复用的 Dreamina sessionId；首个视频生成后不可更换
python3 scripts/workflow.py --workspace workspace set-video-session \
  --project <project> --episode <episode> --session-id <sessionId>
```

`advance` 会运行到下一条原生 Agent 待办、完成、阻塞、session 配置闸门或视频确认闸门。任何真实视频调用必须走 `confirm-video` 或 `confirm-video-batch`；批量命令在单集内最多同时提交 2 个视频生成单元。视频 Provider 从当前剧集状态读取 `video_session_id`，不会跨剧集复用环境变量。

新剧集默认 `storyboard_decision=no`，首次 `advance` 不会因故事板决定停顿。`storyboard_binding` 和 `storyboard_generation` 会以 `skipped` 状态记录，不创建占位资产；资源规划、定妆和视频参考绑定仍正常执行。显式选择 `pending` 时仍保留用户决定闸门，显式选择 `yes` 时则正常生成故事板。

故事板支持镜头级重做。Repair Plan 固化 Stage、Task、基础资产版本、annotation watermark、修复指令和下游影响；`regenerate --repair-plan` 仅替换指定镜号。旧图先复制到不可变版本目录，新图完成真实文件与合同校验后，只进入一次精简整组审查。阶段级重做仅用于故事板规划或全部 Prompt 整体失效。

## 5. 状态与增量标注

`status.json` 仍是唯一业务状态来源：

```text
workspace/status/<project>/<episode>.json
```

核心标注摘要：

```json
{
  "annotations": {
    "state": "pending",
    "latest_revision": 12,
    "handled_revision": 9,
    "pending_count": 3,
    "redo_count": 1,
    "manifest_path": "annotations/index.json",
    "updated_at": ""
  }
}
```

实际标注保存在：

```text
workspace/projects/<project>/episodes/<episode>/annotations/
├── index.json
├── a_000001.json
└── a_000002.json
```

规则：

1. 工作台先原子写入单条标注和索引，再更新状态摘要。
2. Codex 每轮只读精简状态。
3. `pending_count == 0` 时不得读取标注文件。
4. 有增量时只读取 `handled_revision` 之后的内容。
5. `suggestion` 修改成功或明确无需修改后，确定性命令才更新游标。
6. `redo` 必须进入 `planned → processing → verifying → resolved`，不能直接 handle。
7. Repair Plan 只处理创建时的 annotation watermark 与明确绑定 id；运行期间的新标注留到下一轮。
8. 标注不能直接改变 stage 状态或触发 provider。

## 6. Workspace 结构

```text
workspace/
├── projects/
│   └── <project>/
│       ├── project.json
│       └── episodes/
│           └── <episode>/
│               ├── episode.json
│               ├── annotations/
│               ├── repairs/
│               ├── versions/
│               ├── intermediate/
│               │   ├── character_design/
│               │   ├── asset_planning/
│               │   ├── visual_design/
│               │   ├── storyboard_binding/
│               │   ├── video_binding/
│               │   ├── reference_packages/
│               │   └── run_metadata/
│               └── 真实最终产物（路径保持不变）
├── status/<project>/<episode>.json
├── events/<project>/<episode>.jsonl
└── locks/<project>/<episode>.lock
```

项目首页通过扫描项目元数据和状态文件生成，不使用数据库或第二套索引状态。

## 7. 工作台页面

### 项目首页 `/`

- 左侧项目列表。
- 右侧显示所选项目的剧集卡片。
- 每集显示运行状态、进度、风格、时长、镜头数和待处理标注数。
- 没有新建、创意顾问或生成入口。

### 剧集页 `/episodes/[id]?project=<project>`

- 剧集概要与整体进度。
- 完整九阶段导航，选择阶段只改变展示。
- 按视频生成单元集中浏览该单元全部执行产物和实际绑定的定妆参考。
- 当前阶段真实资产预览。
- 阶段级或资产级建议/重做标注、Task 进度、Repair Plan 状态和版本切换。
- 增量 revision 和待处理数量。
- 最近事件和当前错误。
- 没有继续、暂停、重跑、Provider、模型或视频确认按钮；`waiting_agent` 时仅展示当前子 Agent 类型、Stage 和 Task。

## 8. 工作台 API

```text
GET  /api/health
GET  /api/projects
GET  /api/projects/:project/episodes
GET  /api/episodes/:episode/status?project=...
GET  /api/episodes/:episode/events?project=...
GET  /api/episodes/:episode/annotations?project=...
GET  /api/episodes/:episode/assets/:path?project=...
POST /api/episodes/:episode/annotations
```

不存在创建、推进、暂停、重跑、确认视频、模型切换或 Provider 切换的工作台 API。

## 9. Provider 与资产原则

- 文字只使用 Codex。
- 图片默认使用 Codex 内置 GPT Image 2，可由 Codex 命令切换即梦图片 CLI。
- 视频使用即梦视频 CLI；未接通时必须阻塞。
- 不支持 MiniMax、mock fallback、Supabase、runner 选择器或旧 12-stage 结构。
- 不允许使用占位图、渐变图、文本文件、旧图复制或假视频冒充成功。
- 图片和视频必须通过真实字节与元数据校验后登记为活跃资产。

## 10. 测试与完成定义

自动测试全部使用临时目录，不得向正式 `workspace/` 写测试剧集。

初版完成条件：

- 后端状态、流程、资产和标注合同通过自动测试。
- 前端正式构建通过。
- 浏览器可以切换项目、选择剧集、浏览九阶段和真实资产。
- 浏览器新增标注后，状态摘要 revision 与 pending_count 正确更新。
- Codex 命令只读取未处理标注。
- 工作台生产写接口和旧制作按钮已删除。
- 视频生成仍受独立确认和当前 fingerprint 约束。

## 11. 初版暂不做

- 数据库、账号、权限、云同步和多人协作。
- 可视化流程编辑器。
- 工作台内创建或生成。
- 画面坐标、涂鸦和框选标注；初版只做阶段级与资产级文本标注。
- 像素差异叠加、人工回滚和跨版本合并。
