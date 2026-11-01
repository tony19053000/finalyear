# Lab coordination (D426)

One Claude Code session per laptop runs the lab steps; the Lead session plans and checks them.

```
Lead (L1, main)  --writes-->  lab/coord/Ln/TODO.md  --push main-->  GitHub
lab agent (Ln)   --merge main, run steps, write REPORT.md + lab/reports/ --push--> lab/Ln
Lead             --reads origin/lab/Ln, merges, fixes bugs, ticks roadmap, writes next TODO
```

## Setting up a laptop (human, once)

1. Install git, `uv` (https://docs.astral.sh/uv/), the GitHub CLI `gh`, and Claude desktop (Code tab).
2. `gh auth login` with an account that can push to `tony19053000/finalyear` (do this yourself; never give
   the token to Claude).
3. `gh repo clone tony19053000/finalyear` and open that folder in Claude Code.
4. Paste the cold-start prompt below, with your laptop id.

## Cold-start prompt for a lab laptop

```
You are the MeshForge lab agent for laptop L2 (change to L1/L3/L4). Read lab/coord/RULES.md fully and follow it
exactly, then do lab/coord/L2/TODO.md step by step on branch lab/L2. I am sitting here for physical steps and
admin prompts. Short simple messages.
```
