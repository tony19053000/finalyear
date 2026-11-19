---
name: commit-only-when-told
description: Never commit/push UI work until the user explicitly says to commit
metadata:
  type: feedback
---

Do not commit or push changes (dev/ui or lab/chat) until the user says "commit". Don't even offer commit as the default next step after each change.

**Why:** user said on 2026-11-19 "I will tell you when to commit."
**How to apply:** after UI changes, show and ask for review only; batch commits when told. Related: [[dev-ui-taste-and-rules]].
