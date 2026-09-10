# 当前视频生产流程架构

> 2026-08-22：第一阶段成为唯一内容权威，后续阶段只执行或绑定资产。

## 核心原则

1. 第一阶段一次性完成并锁定剧本、详细分镜、故事板执行 Prompt、视频生成单元和最终视频 Prompt。
2. 用户已经提供完整剧本与分镜时，只机械拆分视频生成单元，不做语义审查、扩写、重排或时长再分配。
3. 视频生成单元可以包含多个内部短镜头。内部镜头不等于独立 Provider 调用。
4. 后续阶段不能修改第一阶段 Prompt；参考图路径存放在独立 JSON 绑定文件中。
5. 故事板错误只重做故事板，不反向修改剧本、分镜或视频 Prompt。

## 九阶段

| 顺序 | stage id | 职责 |
|---|---|---|
| 1 | `story_design` | 剧本与分镜定稿；产出不可变视频 Prompt 与故事板 Prompt |
| 2 | `asset_planning` | 规划角色/场景/道具数量、required/optional 和逐单元绑定 |
| 3 | `character_design` | 生成计划内角色定妆 |
| 4 | `visual_design` | 一起生成计划内场景与道具定妆 |
| 5 | `storyboard_binding` | 确定性生成故事板参考路径清单 |
| 6 | `storyboard_generation` | 按锁定分镜生成故事板 |
| 7 | `video_binding` | 确定性生成视频参考路径清单并记录 Prompt SHA-256 |
| 8 | `video_generation` | 提交第一阶段锁定 Prompt（最前追加剧名/剧集/镜头标识）与第七阶段参考图 |
| 9 | `edit_post` | 剪辑与交付 |

## 内容与资产权威

| 内容 | 唯一权威 |
|---|---|
| 剧情、台词、动作、镜头、时长、转场 | 第一阶段 `script.md` / `storyboard.md` |
| 视频模型提交正文 | 第一阶段 `shotNN_prompt_video.txt` |
| 故事板执行正文 | 第一阶段 `shotNN_prompt_storyboard.txt` |
| 资产数量与必要性 | 第二阶段 `asset_plan.json` |
| 角色外观 | 角色定妆图 |
| 场景和道具外观 | 场景与道具定妆图 |
| 实际上传路径 | `shotNN_*_references.json` |

## 可选资产

- `required=true`：缺失或失败必须阻塞。
- `required=false`：生成或审查失败可以省略，不生成占位图。
- 省略可选参考图不会删除剧情元素；元素仍由第一阶段锁定 Prompt 描述。
- 后续绑定只能选择 `asset_plan.json` 中已声明且成功产出的资产。

## 视频提交

视频调用读取：

1. `shotNN_prompt_video.txt` 原始字节；
2. `shotNN_video_references.json` 中的有序路径；
3. 当前视频模型、sessionId、时长与比例。

绑定文件保存 Prompt SHA-256。正文发生任何变化都会使绑定或视频审批失效。视频确认按生成单元隔离；可用 `confirm-video-batch` 并发提交多个单元，单集并发上限为 2。`confirm-video` 保留用于单镜精准重做。
