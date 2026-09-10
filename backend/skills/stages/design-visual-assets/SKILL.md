---
name: design-visual-assets
description: Produce only the scene and prop design specifications and provider-ready image prompts listed in the locked asset plan.
---

# 场景与道具定妆

只处理 `asset_plan.json` 中 `type=scene` 或 `type=prop` 的资产。不得新增、合并、拆分或重命名资产。

剧集级资产复用决策是权威输入。对每个 `decision=reuse` 的场景或道具，不返回任何文件，不重新设计、改写或生成；状态机会原字节复制已验证的设定、Prompt 和 PNG。只为 `decision=generate` 的资产返回下列文件。

每个场景返回：

- `scene_<name>_sheet.md`
- `scene_<name>_prompt.txt`

每个道具返回：

- `prop_<name>_sheet.md`
- `prop_<name>_prompt.txt`

设计只负责外观、空间、材质、比例和必要状态，不得改变第一阶段锁定的剧情、动作、镜头、台词或视频 Prompt。

角色定妆板已包含的武器、念珠或标志性随身装备不得再做独立道具板。只有锁定资产规划已明确说明角色板不包含该物件，且剧情需要独立身份、交互或状态参考时，才能制作。

场景定妆应锁定同一物理空间的布局、入口、出口、固定地标、光源和表面材质。道具定妆应锁定尺寸、材质、抓握点及剧本明确要求的状态。只生成资源规划中列出的资产。

每张场景板表现 exactly one physical environment，not several similar rooms，也不是 alternate set designs or a chronological storyboard。使用一个 dominant establishing view 建立空间，一个 overhead plan/blocking inset 证明平面关系；明确 primary entrance、camera-safe side of the axis、fixed screen-left/screen-right landmarks、light origin 与 shadow fall。只有隐藏几何确实影响执行时才增加 small reverse or oblique geometry-proof angle。任何视图都不得 mirrors or reverses the production axis。

在文字 review 中分别标明必需与可选资产。必需资产存在合同冲突时阻塞；可选资产无法可靠设计时允许不返回该资产，但必须在 summary 中列明省略原因，后续不得绑定占位图。
