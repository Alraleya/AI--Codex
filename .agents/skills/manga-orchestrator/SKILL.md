---
name: manga-orchestrator
description: One-click set up, advance, repair, or review this AI manga/video workflow through deterministic handoffs and visible native Codex subagents; trigger on “一键配置 AI漫剧-Codex 环境” and never directly edit status.json.
---

# AI Manga Orchestrator

Use this skill when onboarding this repository or advancing, repairing, or reviewing an episode.

## First-run branch

When the user says `一键配置 AI漫剧-Codex 环境`, read [docs/INTEGRATION_GUIDE.md](../../../docs/INTEGRATION_GUIDE.md), then run `python3 scripts/bootstrap.py` from the repository root.

- Fix safe repository-local setup failures automatically and rerun the failed check.
- If Python 3.9+ or Node.js 20.9+ is absent, use an available supported package manager when the user's setup request authorizes it; otherwise report the exact system dependency that remains.
- Finish by reporting Python/Node readiness, tests, frontend build, Skill discovery, image Provider readiness, video Provider readiness, and the workbench launch command.
- Do not create a project or episode and do not generate text, images, or video during setup.

For any other first-run or preflight request, perform only the requested checks:

1. Use `scripts/bootstrap.py` for Python, Node/npm, Python tests, frontend installation/build, and read-only Provider checks.
2. Confirm that this repository-level skill is discoverable from `.agents/skills`.
3. Do not treat an unconfigured Provider as a successful integration.
4. Create a minimal project or episode only when the user asks for it.
5. Stop before paid or external generation unless the current user request authorizes that exact action. Video still requires its normal revision-bound confirmation.

Do not copy the internal `backend/skills/stages` folders into a personal skill directory. The state machine injects those instructions into the matching child payload.

## Operating contract

- `status.json` is the only business-state authority. Read and write it only through `scripts/workflow.py`.
- The workbench only displays state, assets, and annotations. Do not use it for dispatch.
- Never run `codex exec`, `backend/runners/codex.py`, or a background reviewer runner.
- Delegate every requested model task with the native `spawn_agent` capability, so the work is visible in Codex.
- A child may return content only. It must not edit `status.json`, advance the workflow, or call a paid provider.
- Do not hold an episode lock while a child runs; `next-action` and `submit-agent-result` already manage this boundary.
- Before each episode turn, run `scripts/workflow.py status`. Read annotation contents only when its summary reports `pending_count > 0`.

## Dispatch loop

1. Read the episode summary with `scripts/workflow.py status`.
2. If the run is not `waiting_agent`, use the user-authorized workflow command (`advance` or a scoped `regenerate`) once.
3. Read `scripts/workflow.py next-action`. If it returns `idle`, stop and report the real state. If it returns a request, use only that request's `payload` as the child context.
4. Spawn exactly one native child for the request. Use the repository role `manga-stage-producer` for `stage_generation` and `manga-storyboard-reviewer` for `storyboard_sequence_review`. Tell it to return only the JSON object that conforms to `payload.output_schema`; attach only the listed local images. Do not ask it to edit files.
5. Save the child's final JSON response to a temporary result file under the episode directory. Submit it with `scripts/workflow.py submit-agent-result --request <id> --result-file <path>`.
6. Repeat from step 3 until the run is `waiting_confirmation`, `blocked`, `paused`, or `done`. Do not continue through video generation without the normal video confirmation.

Before asking for video confirmation, inspect the `video_binding` handoff. If it reports character-binding warnings, read only the affected current `shotNN_video_references.json` files, summarize every `character_reference_closure.issues` item to the user, and make clear that these are advisory findings. Do not block or silently repair the bindings; the user decides whether to confirm generation or request a correction.

## Child roles

- `stage_generation`: create the complete stage-output JSON from `payload.instructions`, `creative_brief`, and listed images. Perform the required self-check before setting `review.passed`.
- `storyboard_sequence_review`: inspect all attached boards once as a stage barrier. A `block` must name affected `shotNN` tasks and give concrete repair instructions.

## Result hygiene

- Before submission, check that the child returned a JSON object, not Markdown or explanation.
- Never invent a success result. Invalid JSON, stale request ids, missing images, or a failed child must remain blocked or waiting for a retry decision.
- For a `block`, let the state machine create the repair boundary. Regenerate only the named shots; do not regenerate siblings or a whole stage unless a repair plan selects them.
