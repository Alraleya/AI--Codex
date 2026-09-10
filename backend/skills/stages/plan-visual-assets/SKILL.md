---
name: plan-visual-assets
description: Plan the exact character, scene, and prop reference assets needed by the locked shots without writing design prompts or generating images.
---

# 定妆资源规划

## 职责

只读取第一阶段锁定的剧本、分镜、故事板 Prompt 和最终视频 Prompt，输出 `asset_plan.json`。不得生成定妆 Prompt、定妆说明或图片，不得改写任何镜头内容。

## 输出结构

`asset_plan.json` 必须是有效 JSON：

```json
{
  "version": "1.0",
  "assets": [
    {
      "id": "char_example",
      "type": "character",
      "name": "角色名",
      "required": true,
      "reason": "为什么需要或不需要独立参考图",
      "shots": [1]
    }
  ],
  "shots": [
    {
      "shot_number": 1,
      "storyboard_required": true,
      "references": [
        {
          "asset_id": "char_example",
          "necessity": "required",
          "purpose": "身份一致性"
        }
      ]
    }
  ]
}
```

## 规则

- `type` 只能是 `character`、`scene` 或 `prop`；ID 必须分别以 `char_`、`scene_`、`prop_` 开头。
- 必须先查看上下文中的“已验证可复用资产目录”。同一角色、场景或道具必须沿用目录中的稳定 ID；只有剧本明确要求新造型、新服装、新物理空间或新道具时，才能建新 ID。
- 资产规划完成后，状态机会在任何定妆出图前展示“建议复用 / 建议新生成”清单并等待用户确认；本阶段不得代替用户做最终复用决策。
- `required` 表示是否必须拥有独立定妆参考图，不表示该元素能否从剧情中消失。
- 角色的武器、念珠或标志性随身装备优先直接继承角色定妆板，不另立 `prop_` 资产。只有角色定妆板确实没有该物件，且镜头需要它作为独立身份、交互或状态参考时，才允许规划独立道具；reason 必须明确说明角色板为何不足。
- 普通、单次、外观不需要锁定的元素优先设为非必需，减少图片调用和参考冲突。
- 每个镜头只列实际出现或直接影响画面的资产；不得绑定全剧资产。
- `necessity` 必须与资产的 `required` 一致。
- 资产数量、ID、必要性和逐镜绑定关系在本阶段锁定；后续不得自行新增。
- `storyboard_required` 必须继承第一阶段 `storyboard_plan`，不得重新判断。

可选资产后续若生成或审查失败，可直接标记省略；剧情元素仍由锁定 Prompt 描述，但故事板和视频不绑定该资产。必需资产失败则必须阻塞。
