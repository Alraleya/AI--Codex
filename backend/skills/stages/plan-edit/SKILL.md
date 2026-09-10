---
name: plan-edit
description: Create an edit and delivery plan from real generated videos, final prompts, review evidence, and script continuity.
---

# Plan Edit

Write `edit_post.md` in Chinese using only verified video assets and their metadata.

- Map each source clip to timeline in/out points without inventing missing footage.
- Begin with a compact "剪映执行摘要" that tells the editor what to do first, the intended rhythm, and the three most important emotional beats.
- Include a shot-by-shot timeline table with absolute timecode, source unit, in/out or trim, cut/transition, and editorial purpose.
- Define music entries and exits by timecode, mood/instrument direction, volume level, ducking under dialogue, and whether the editor should use a licensed/local track; never invent a track that is not supplied.
- Define ambience and sound effects by timecode and action: footsteps, impacts, whooshes, comedic accents, room tone, silence, and audio fades.
- Mark every character introduction that needs an on-screen name/title: timecode, character name, display duration, position/safe area, typography treatment, and whether it should be omitted when the character is already established.
- Define dialogue treatment, subtitle timing, line breaks, emphasis words, safe areas, color matching, and delivery settings.
- Reconcile actual clip durations with target shot durations. State trims, handles, speed changes, freeze frames, or gaps explicitly; never hide a mismatch.
- Preserve dialogue order, action causality, screen direction, and prop state across cuts.
- Compare every actual clip's first/last usable frame with the adjacent storyboard state; place a cut, sound bridge, or transition only when it repairs a real continuity need.
- Keep generated dialogue and sound separable from editorial subtitles; do not assume on-image text is usable.
- State the final aspect ratio, resolution, frame rate, audio format, captions, and QC checklist.

Use these headings in `edit_post.md` whenever the evidence is available: `剪映执行摘要`, `时间线与剪切`, `音乐进入/退出`, `音效与环境声`, `人物出场名`, `对白与字幕`, `画面统一`, `导出设置`, `交付前检查`.

Reject the review if the plan relies on an absent or invalid clip. Do not claim that a final movie exists until a real file is rendered and validated.
