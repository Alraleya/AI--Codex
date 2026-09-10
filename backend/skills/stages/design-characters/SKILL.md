---
name: design-characters
description: Extract and visually lock every continuity-relevant on-screen character during character_design, producing one paired specification and a provider-ready, hierarchically multi-view identity-sheet prompt per stable character key.
---

# Design Characters

Only process `type=character` entries already locked in `asset_plan.json`. Do not discover, add, merge, split, or rename characters. Do not create an image for a voice-only character. Return exactly one pair for each character that remains scheduled for production:

- `char_<name>_sheet.md`
- `char_<name>_prompt.txt`

The episode-level asset-reuse decision is authoritative. For every `decision=reuse` character, return no files and do not redesign, reinterpret, or regenerate it; the state machine copies the already verified specification, prompt, and PNG byte-for-byte. Produce the pair above only for `decision=generate` characters. A character's weapon and signature carried equipment must follow the confirmed character sheet. Do not create or imply a separate conflicting weapon design here.

In the sheet, lock role, apparent age, silhouette and proportions, face geometry, hair/fur/skin markings, wardrobe layers or harness, footwear where applicable, carried items, palette, material response, expression range, movement signature, screen-side placement of asymmetric cues, and forbidden drift. Distinguish similar characters with at least three durable cues. State which side/back features must be proven by auxiliary views. End with 2–3 concise identity cues suitable for video-reference binding.

In every character design record, include the explicit note: `装饰物细节图按角色实际需要添加`. Treat accessory and ornament close-ups as conditional production detail, not a universal review requirement.

Write each prompt as a self-contained identity-sheet request in this order: purpose, subject locks, layout hierarchy, view/camera logic, lighting and materials, palette, expanded `style_constraints`, then only likely failure exclusions.

## Hierarchical multi-view board contract

Every board represents exactly one stable identity. Explicitly say that every body, head, and detail inset is the same character at the same age, proportions, grooming, wardrobe, markings, and accessory state—not a cast, group, relative, duplicate, or alternate costume.

Use a clear hierarchy instead of an equal-sized turnaround lineup:

1. One dominant neutral three-quarter full-body identity anchor occupies roughly 50–65% of the usable canvas and establishes the canonical proportions, face, silhouette, costume, and carried-item state.
2. Add at least one smaller subordinate body view at a different angle, so the board contains at least two body angles in total. Prefer a clean side profile to prove silhouette and limb/body proportions. Add a small rear three-quarter or rear view only when back markings, hairstyle, tail, garment construction, harness routing, or rear-mounted props matter.
3. Add one neutral face close-up. Use at most 2–3 small expression head studies only for expressions the production actually needs; they must preserve the neutral head geometry.
4. Add only necessary material, color, pattern, fastener, hand/paw, or prop-contact details. If a characteristic story pose is essential for animation, include one small pose inset without scenery, impact effects, or narrative progression.

Keep all subordinate views visibly smaller than the dominant anchor, separated by generous plain negative space, and oriented consistently rather than mirrored. Repeat the exact asymmetric cues in every view: left/right markings, ear folds, scars, hair part, jewelry, armband, card, harness, and carried item must stay on the same anatomical or costume side. Use neutral reference poses, a plain background, matched eye/camera height, stable proportions, and one lighting setup across the board.

Do not request a row of equal-sized front/side/back figures, multiple hero figures, alternate outfits, age variants, or action poses that could be interpreted as several characters. Exclude story scenery, extra people or animals, narrative action, labels, logos, and watermarks.

## Review gate

Use `asset_plan.json` as the necessity authority. Fail the stage when a required character is missing or invalid. An optional character asset may be omitted rather than repaired; list it in the summary and do not create a placeholder. Also fail if a voice-only role receives an image, the prompt and sheet disagree, the board has only one body angle, identity views drift, or a necessary side/back/contact detail is not visible. The absence of a separate ornament close-up is not, by itself, a review failure; add one only when continuity needs it.
