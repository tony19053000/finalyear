# Lab announcement - from the Lead (2026-11-03)

Every lab agent reads this file after each `git merge origin/main`. Newest message on top.

## 2026-11-03 - the lab after the 2-day break

**The laptops now**

| id | host | OS | IP (Wi-Fi, DHCP - re-check) | GPU | role / state |
|---|---|---|---|---|---|
| L1 | JARVISM3 | Linux | 192.168.31.13 | RTX 3050 4 GB | **controller installed + fabric created + running** (`https://192.168.31.13:47110/`, gRPC 47101). Admin user `admin` (password: human only). V0.2 done. |
| L2 | AILESH | Windows 11 | 192.168.31.155 | RTX 2050 4 GB, CUDA torch | V0.2 done. Agent installer downloaded; install + join next. |
| L3 | **MOHIT (NEW laptop)** | Windows 11 | 192.168.31.71 | none (Intel UHD) | **Replaced the old L3 (TIWARI_JI, retired; backup branch `lab/L3-old-TIWARI_JI`, ignore it).** Doing round 1 + V0.2 catch-up. |
| L4 | new, arrived | (from its round 1) | (from its `L4-ready`) | (from its round 1) | Starting round 1 + V0.2 catch-up. |

Old L3 results (TIWARI_JI) never count for L3. Only signals in the CURRENT `REPORT.md` of a branch count.

**What happens next, in order**
1. **L3 and L4:** round 1, then V0.2 catch-up (L4 TODO phases P0 + CU, L3 uses its own name). They emit
   `L3-ready` / `L4-ready` and `s1-serve-up L3` / `s1-serve-up L4`.
2. **L1 and L2:** when those signals appear, run your S1 client against L3 and L4 (Lead note at the end of your
   TODO), push, emit `s1-client-done <you>-to-L3` / `-to-L4`. L3 and L4 also measure each other.
3. **L1 (meanwhile):** C17-h install L1's own agent (HUMAN sudo), then C17-i invites: **L2's invite first**
   (L2 is ready now), L3's and L4's when they reach their install step.
4. **L2 (meanwhile):** install the agent and join with L1's invite as soon as L1 has created it (human types the
   code). Remove the leftover "MeshForge S2" firewall rule when your TODO says so.
5. Then V0.3 onward with all laptops joined. Phases that "need L4" also wait for the new L3's `joined`.

**Known issues / fixes**
- L3 found that PowerShell `-EncodedCommand` hangs on some Windows laptops (Defender). Lab tools only, not the
  product. Fix "fix(lab): run PowerShell via temp .ps1 -File" is coming on main; rerun the affected steps after it
  (inventory R1-3, S7 R1-5). L2/L4 on Windows: if inventory or S7 times out, it's this - report and continue.
- IPs come from the router (no reservations): re-check your IP before each phase and re-emit `<Ln>-ready` if it
  changed.
- Wi-Fi only (D425). Keep laptops on charger, sleep off, Windows profile Private.
- Never type or write passwords, invite codes or tokens; the human does.
