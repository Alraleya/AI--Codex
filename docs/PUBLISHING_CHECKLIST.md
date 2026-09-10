# 发布到 GitHub 前检查

仓库按“源码与 Skill 可公开、运行数据只留本地”整理。创建 GitHub 仓库和选择许可证仍由仓库所有者完成。

## 建议公开

`.agents/skills/manga-orchestrator/`、`.codex/agents/`、`.github/workflows/ci.yml`、`backend/`、`frontend/`、`scripts/`、`docs/`、`AGENTS.md`、`PROJECT_STATE.md`、`README.md`、`pyproject.toml` 和依赖锁文件。

## 不应公开

`.gitignore` 应排除 `workspace/` 内的剧集与资产、`.env`、临时审查目录、输出、草稿、Playwright 文件、构建缓存和本机系统文件。不要为演示取消 `workspace/` 的忽略；需要示例时单独制作去身份 fixture 或截图。

## 发布前检查

```bash
rg -n -i --hidden \
  --glob '!workspace/**' \
  --glob '!frontend/node_modules/**' \
  --glob '!frontend/.next/**' \
  '(api[_-]?key|secret|password|bearer|authorization|cookie)'

git init
git status --short --ignored
git check-ignore -v workspace/status/example.json .env .tmp/example.json

python3 -m unittest discover -s backend/tests -v
cd frontend && npm install && npm run build && cd ..
```

`workspace/.gitkeep` 应可纳入版本控制，其他 `workspace/**` 应被忽略。

## 所有者必须决定

- **许可证**：发布前选择 MIT、Apache-2.0 或其他适合的许可证。没有许可证时，其他人不能放心复用；整理过程不替所有者做法律选择。
- **Provider 说明**：仓库不附带即梦/Dreamina CLI；账号、费用、地区可用性和条款由使用者负责；`fast` / `mini` 是当前适配器逻辑名。
- **素材权利**：确认有权分享剧本、角色、世界观、参考图、生成结果、品牌、字体、音乐和音效。

## 初始化与推送

GitHub 仓库地址为 `Alraleya/AI--Codex`。许可证确定后：

```bash
git init
git add .
git status --short
git commit -m "Initial public release"
git branch -M main
git remote add origin "git@github.com:Alraleya/AI--Codex.git"
git push -u origin main
```

提交前逐行检查 `git status`，尤其确认没有 `workspace/`、`.env`、本机绝对路径、真实 sessionId、Cookie 或商业素材。
