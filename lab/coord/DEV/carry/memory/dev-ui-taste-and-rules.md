---
name: dev-ui-taste-and-rules
description: DEV workstation rules + user's strict UI taste for the MeshForge dashboard/website (carried from old laptop HANDOFF 2026-11-19)
metadata:
  type: feedback
---

User reviews every step; small steps; show screenshots at 1920x830; ask before big redesigns; revert immediately when asked. No reviewer agent; implementer subagent = Opus high with a full brief. Short point-to-point messages.

Taste: clean, sparse, few boxes; tables PLAIN on page background with hairlines (preferred over frosted glass); no pills, no glow; mono uppercase small labels, light big numbers, even spacing; explanations in tooltips; settings pages calm, not dashboard-like. Demo (?demo=1) must mirror the real system "same to same" — only data replaced, never change routes/APIs. No inline style={} (CSP).

Rules: push only dev/ui (only when user says) and lab/chat; never main or lab/L*; never touch L1-L4; frontend only; never commit wsl-controller-test-creds.txt; back up frontend/src before big changes (frontend-src-backup-vNN, local only).

**Why:** user's explicit, repeated preferences; first full redesign was rejected as "worse".
**How to apply:** every dashboard/website change. Related: [[hive-logo-direction]], [[hive-bee-mascot-choice]], [[agent-team-models]].
