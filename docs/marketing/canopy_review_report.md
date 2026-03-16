# Canopy Seed — Launch Day Review Report
**Reviewer:** Claude (Cowork)
**Date:** March 14, 2026
**Scope:** All files reviewed as of launch day. Flagging accuracy issues, staleness, and gaps before launch assets are created.

---

## File Index

Files reviewed:
- `Antigravity agents/task-dashboard/canopy-seed-demo/index.html` — The main demo website
- `Antigravity agents/task-dashboard/canopy-seed-demo/README.md` — GitHub repo README
- `Antigravity agents/task-dashboard/canopy-seed-demo/SETUP_INSTRUCTIONS.md` — GitHub Pages setup
- `Antigravity agents/task-dashboard/CANOPY_SEED_VISION.md` — Founding vision document (2026-02-25)
- `Ai short term vision/# Canopy Seed 🌱.txt` — Duplicate of README (identical content)
- `Ai short term vision/Canopy context file.txt` — Backend source code (context_builder.py), not a doc
- `Antigravity agents/task-dashboard/SESSION_STATE.md` — Dev status snapshot, dated 2026-02-24
- `Antigravity agents/task-dashboard/DEVHUB_BRAIN.md` — Dev Hub brain state doc
- `Ai short term vision/start prompt feb 28 marketing claud.txt` — Marketing session stub (Forest OS reference)

**Note on paths:** The file paths in the handoff brief (`Canopy Seed/canopy-ui/index.html`, `devhub.html`) do not match the actual project structure. The real demo lives at `Antigravity agents/task-dashboard/canopy-seed-demo/index.html`. The folder `Canopy Seed/` on the Desktop contains only an empty placeholder file. This should be reconciled — either move files or update any documentation that references the old paths.

---

## 1. `canopy-seed-demo/index.html` — Main Demo Website

### What's Accurate and Strong — Keep for Assets

The demo is genuinely excellent. The visual design (dark forest aesthetic, glass morphism, copper accents, animated seed-to-tree growth indicator) is polished and on-brand. The interactive narrative arc is the strongest single asset in the project:

- **7-stage flow** — Seed conversation → Blueprint → Dev Hub → QC Swarm → Guided Debug → Running App — tells the complete story cleanly
- **Battery Operations Suite example** is a real, concrete use case. Industrial application. Specific (cell inventory, assembly sign-off, 12 engineers, field + mobile). This is the right level of specificity for credibility.
- **Agent routing visualization** — the Complexity Judge routing tasks by score (Lite → Standard → Brain) is clear and shows real system behavior
- **QC Swarm section** — 3 parallel testers, auto-fix on failure, shows exactly what makes the system trustworthy
- **Guided Debug with vision** — "Big Brain reviews the build visually with screen capture" is a differentiated, memorable feature. Well-presented.
- **NotebookLM export** — well-explained with clear 3-step instructions. Good for non-technical audience.
- **"Demo mode — auto-plays or type your own message"** — note at the bottom of the composer is honest and helpful
- **Tagline "Naturally Growing Software"** — appears consistently throughout. Strong. Keep everywhere.
- **"Built with Canopy Seed" badge** at the top of the final app view — meta-demonstration in action. Good.

### What's Stale or Needs Updating

**1. Model names in the welcome screen chip selector**
- Displays: Claude Opus, Gemini Pro, GPT-4.1, Local
- Issue: This was listed as a known v1.1 bug at the Feb 28 launch ("Incorrect model names displayed in UI"). Current Claude API offers Claude Opus 4.6 (fine), but "Gemini Pro" is outdated — Gemini 2.5 Flash/Pro is the current offering. GPT-4.1 is real but not part of the actual system per internal docs.
- **Recommended fix before using demo in assets:** Update chip labels to match what the system actually routes to, or keep them as illustrative tiers (Lite / Standard / Brain) without specific model names if names are v1.1 scope.

**2. Dev Hub subtitle: "Manager routing each task to the right model tier — Haiku · Gemini Pro · GPT-4.1"**
- Same issue as above. The internal DEVHUB_BRAIN.md shows the real roster: Ministral 3B 14B (local), Claude Sonnet 4.6, Claude Opus 4.6, Gemini 2.5 Flash. GPT-4.1 is not in the actual agent roster.
- The demo is aspirational/illustrative here, which is fine for a demo — but should not be the basis for marketing copy that claims "uses GPT-4.1" unless OpenAI integration is actually wired.
- **Flag:** Do not copy the exact model names from the Dev Hub section into launch materials unless you confirm they match what's actually running.

**3. QC tester models: "Gemini Flash · GPT-4.1-mini · Claude Haiku"**
- The actual tester swarm uses Gemini Flash 2.5 Lite for failure analysis per SESSION_STATE. Claude Haiku is plausible. GPT-4.1-mini is unconfirmed.
- Same guidance: fine for demo illustration, don't copy into factual marketing claims.

**4. Demo is at the wrong path**
- The GitHub link in README says: `https://tyoung515.github.io/canopy-seed-demo`
- If this has not been deployed to GitHub Pages yet, the link is dead. Confirm this is live before sharing.

### What's Missing

- No version number displayed anywhere in the demo itself. The running app shows "v1.0" but the demo has no version indicator. Consider adding "v1.0 · March 2026" to the footer or welcome card.
- No way to contact Travis or learn more. For launch day, a GitHub repo link or contact method on the demo would help.

---

## 2. `canopy-seed-demo/README.md` (and identical `# Canopy Seed 🌱.txt`)

### What's Accurate and Strong

- **Plain language throughout** — this is a well-written README. Non-technical audience would understand it.
- **Getting Started section** — concrete and complete (Python version, API keys, running the server, first use). Keep as-is.
- **Big Brain explanation** ("It's not a scary algorithm — it's just an AI with a long conversation history and instructions to be a good listener.") — excellent plain-language framing. Use in assets.
- **Snapshots and Rollback** — clearly explained. The "undo for your entire codebase" metaphor is good.
- **"Built with AI, Documented by Humans"** section — strong positioning for transparency. Keep.
- **Model choice section** — the description of Claude, Gemini, and local models is accurate and good.

### What's Stale or Needs Updating

**1. Project Structure in README lists `canopy-ui/` as the browser UI directory**
- The actual UI is at `task-dashboard/` (or wherever the demo is hosted). `canopy-ui/` doesn't exist in the current project layout.
- **Fix:** Update the directory tree to reflect the actual structure, or note that this is the expected structure once packaged.

**2. Referenced files that may not exist: `ARCHITECTURE.md`, `DECISIONS.md`, `CONTRIBUTING.md`**
- README tells users to read these files, but they do not appear in the actual canopy-seed-demo directory. SESSION_STATE says these were planned for V1 scope but there's no confirmation they were written.
- **Fix:** Either create these files before going public, or remove the references from the README. Dead links in a README are a trust signal in the wrong direction.

**3. "Claude (Anthropic) — Free tier available via Anthropic's console."**
- Anthropic's free tier situation changes frequently. Verify this is still accurate at launch time.

**4. "macOS support coming soon"**
- No timeline given. Fine to leave as-is for v1.0, but flag for the v1.1 scope list.

### What's Missing

- No link to the live demo (`https://tyoung515.github.io/canopy-seed-demo`). Consider adding to the top of the README so people can see it without installing anything.
- No GitHub stars/badge or any social proof indicators (expected to be empty at launch, just noting).

---

## 3. `canopy-seed-demo/SETUP_INSTRUCTIONS.md`

### What's Accurate and Strong

Clear, correct instructions. The 5-step GitHub Pages setup is accurate. The "Updating the demo later" section with bash commands is practical and specific. Good supporting document.

### What's Stale

None. This is a utility doc for internal use. No staleness issues.

---

## 4. `CANOPY_SEED_VISION.md` — Founding Vision Document (Feb 25, 2026)

### What's Accurate and Strong

This is the richest source document for launch assets. Most of it holds up.

- **Origin story** — "Friday night, Travis started building JOAT. By Tuesday, full orchestration platform. Four days. One person. No team." — This is the hook. Compelling and specific.
- **The problem framing** — "Most people's experience with AI fails at the input stage. They don't provide enough context." — accurate and relatable.
- **"Don't ask users to learn to prompt. Have a conversation with them and do the prompting on their behalf."** — clean, quotable.
- **Differentiation section** — comparison to Copilot/Cursor, no-code tools, project management tools is still accurate and well-argued.
- **The growth model** — "seed software" framing, "the gardener doesn't need to understand the roots" — strong metaphor, on-brand.
- **"The meta-demonstration: This repository was built using Canopy Seed's own methodology"** — excellent story element.
- **Strategic context paragraph** — "Travis Young is CIO of Systematic Power Solutions, a battery and car audio conglomerate (XS Power, Sundown Audio, ~12 companies)" — accurate, good credibility detail.
- **"The goal in releasing publicly is not fame. It's the networking and opportunity velocity that comes from demonstrating AI-native operational leverage in a real industrial context. Being seen building the tool, not just the tool."** — This is Travis's authentic voice. Strong.

### What's Stale or Needs Updating

**1. Architecture diagram shows "Anti (Gemini Pro)" and "VS (GPT)" as agent names**
- The current SESSION_STATE and DEVHUB_BRAIN.md confirm the agent naming (Anti = Gemini backend, VS = frontend/docs). The specific model assignments (Gemini Pro, GPT) may have shifted.
- The arch diagram in the vision doc is internal context, not launch-facing, so low urgency.

**2. "NotebookLM-style synthesis" is listed as a Big Brain feature**
- This is listed as a planned feature in the architecture but may not be implemented in v1.0. Verify before including in external claims.

**3. V1 Scope section lists files like `DECISIONS.md`, `CONTRIBUTING.md`, inline comments**
- As noted above, these may not have been completed. If not, the V1 scope section overstates what shipped.

**4. "Python 3.11+" requirement and "aiohttp backend on port 7821"** — still accurate per SESSION_STATE. Good.

### What's Missing

- No Forest OS mention in this document (correct — keep it that way per Travis's guidance)
- No "v1.0" version designation — consider tagging this document as "Vision v1.0"

---

## 5. `SESSION_STATE.md` — Dev Status (Feb 24, 2026)

### Assessment

This is a technical handoff document, not a launch asset. It captures dev state 18 days before launch day. Key observations for the review:

- **Phase 5 (Tester Swarm) was "IN PROGRESS" as of Feb 24.** Backend integration and frontend were listed as PENDING (agents 20 and 21 not yet run). Unknown if Phase 5 shipped.
- **Phase 6 (Auto-Fix Loop) was backlog** at time of writing.
- If Phase 5 didn't ship: the demo's QC Swarm section (3 parallel testers, auto-fix, 47/47 passing) is showing a feature that exists in the demo but may not be in the actual installable code. This is fine for a demo but should not be described in marketing as "currently working in the installed version" unless confirmed.
- **Known bugs at the Feb 28 git launch (from handoff brief):** Model routing fallback, minor UI resizing, incorrect model names in UI. These are labeled v1.1. Do not claim "all features working perfectly" in launch copy.

---

## 6. Marketing Stub (`start prompt feb 28 marketing claud.txt`)

This file references a "SESSION_HANDOFF_MARKETING.md" in an `outputs/` folder and mentions Forest OS partner pitch docs. Those files were not found in the accessible directory. If Forest OS marketing materials exist elsewhere, they're not in scope for this Canopy Seed review. Per Travis's guidance: keep Forest OS and Canopy Seed separate in all launch assets.

---

## Critical Items to Resolve Before Assets Go Out

**HIGH — Resolve today:**

1. **The "7 days / $150" hook** — Travis's handoff brief mentions "one person built an automated software factory in 7 days for $150." The founding vision document says "4 days." None of the docs mention "$150." If this figure is accurate, it's the strongest hook you have — but verify the numbers before publishing. Misquoting your own origin story in launch materials is the kind of thing that gets screenshot-corrected on social media.

2. **The key line: "We don't force anyone to do anything. We just stick more tokens in."** — Travis flagged this as a line to preserve if it appears. It does NOT appear in any file reviewed. Either add it to the website/README/vision doc before launch, or confirm it was cut.

3. **GitHub Pages live link** — Confirm `https://tyoung515.github.io/canopy-seed-demo` is live before it goes in any asset.

4. **Missing referenced files** — `ARCHITECTURE.md`, `DECISIONS.md`, `CONTRIBUTING.md` are referenced in the README but don't appear in the repo. Write them or pull the references.

**MEDIUM — Flag for awareness:**

5. Model names in demo (Haiku, Gemini Pro, GPT-4.1) are aspirational/illustrative, not the exact current roster. Fine for a demo. Do not copy into factual marketing copy.

6. Phase 5 (Tester Swarm) completion status unknown. Don't claim the auto-fix loop is live unless confirmed.

7. The actual Canopy Seed project files are not in the `Canopy Seed/` folder on the Desktop — they're in `Antigravity agents/task-dashboard/canopy-seed-demo/`. If Travis is directing people to a GitHub repo, confirm the right files are there.

**LOW — Nice to fix later:**

8. `canopy-ui/` path in README doesn't match actual structure.
9. "macOS support coming soon" — no timeline.
10. Free tier caveat for Anthropic — verify.

---

## Summary Scorecard

| File | Launch-Ready? | Notes |
|------|--------------|-------|
| `index.html` (demo) | ✅ Yes | Visual design strong. Model names in Dev Hub are illustrative — fine for demo, don't copy as facts |
| `README.md` | ⚠️ Mostly | Fix dead file references (ARCHITECTURE, DECISIONS, CONTRIBUTING) before going public |
| `SETUP_INSTRUCTIONS.md` | ✅ Yes | Clean utility doc |
| `CANOPY_SEED_VISION.md` | ✅ Yes (with caveats) | Rich source for assets. Verify 4 days vs 7 days claim. Check V1 scope completeness. |
| `SESSION_STATE.md` | 🔒 Internal only | Dev status doc. Not for external use. Phase 5 completion status unknown. |
| `DEVHUB_BRAIN.md` | 🔒 Internal only | Technical reference |

---

## v3 Update — March 14, 2026 (New Files Reviewed)

Travis uploaded six updated files: `PROJECT_STATE.md`, `SESSION_56_AGENT1.md`, `SESSION_56_AGENT2.md`, `index.html` (new), `devhub.html` (new). This section documents what changed, what is now resolved, and what the current accurate state is.

---

### Resolved Issues (from original report)

**✅ Timeline and cost confirmed.** Travis confirmed: ~3 weeks total (functional in week 1, two more weeks of refinement and testing), under $200 all-in including all API calls and subscriptions. The "4 days vs 7 days" discrepancy in the original review is now moot — the accurate public narrative is "three weeks, under $200."

**✅ Phase 5 (Tester Swarm) fully shipped.** `PROJECT_STATE.md` (Session 41 close, March 3) shows the full post-build pipeline fully operational: TesterSwarm → Repair Loop → AutoFix → Giant Brain → ADR-030 router → Changelog. Every stage is marked ✅. The demo's QC Swarm section is now an accurate representation, not a preview.

**✅ Domain and email confirmed.** `canopyseeds.com` (domain), `Contact@canopyseed.com` (email). Use these in all public materials.

**✅ canopy-ui/ directory now confirmed.** The real functional UI is `canopy-ui/` (uploaded as `index.html` and `devhub.html`). The old `canopy-seed-demo/` was an earlier interactive demo, now superseded. The path confusion flagged in the original report is resolved.

**✅ Model routing confirmed — currently end-to-end Gemini.** Current production stack confirmed by Travis (March 14):
- Orchestrator (Phase 0, contract/spec): `gemini-3.1-pro-preview`
- Lite/Standard workers (Phase 1, swarm build, score ≤6): Gemini 3.0
- Brain tier (score 7–10): `gemini-3.1-pro-preview` (same model as orchestrator, re-routed)
- Giant Brain / post-build audit (Phase 2): `gemini-3.1-pro-preview`
- **Reason:** Economics. Full Gemini stack is the cheapest path to the $0.31 figure right now.
- Claude and local models are supported in the UI and available as options; economics currently favor all-Gemini.
- Do not use GPT-4.1 in any marketing claims — it is not part of the actual system.

---

### 7. `PROJECT_STATE.md` — Session 41 Close (March 3, 2026)

**What it confirms:**
- Smoke test ST39 was the first fully clean automated pass. All prior tests were passing before that point.
- 151 tests passing across 10 test files at Session 41 close.
- Complete pipeline status: Seeding Layer ✅, Execution Layer ✅, entire post-build pipeline ✅.
- `canopy-ui/` directory is real and contains the current browser interface.
- Vault system (ADR-040) introduced in Session 55: encrypted local API key storage. Three profiles — Gemini (default), Claude, Qwen. Forge proxy bypass option for teams.

**Test count reconciliation (important for asset accuracy):**
- Session 41 (March 3): 151 tests passing
- Project Helix v3 report (verified metric): 168 tests
- Session 56 pre-session baseline: 379 tests
- Session 56 target: 405 tests (adding 26 new tests for native function calling)

The jump from 151 → 379 represents Sessions 42–55 of development after the March 3 snapshot. **168 is the correct verified number for public claims** (sourced from the Helix v3 technical report which verified it across 115 production runs). The 379→405 range is accurate for current dev state but has not yet been externally verified. Do not use 379 or 405 in marketing materials yet.

---

### 8. `SESSION_56_AGENT1.md` and `SESSION_56_AGENT2.md` — In Progress

Session 56 is a major architectural upgrade currently in development. Two agents are specced:

**Agent 1 — Native Gemini function calling (`ai_backend.py`):**
- Adds `gemini-customtools` backend routing to `gemini-3.1-pro-preview-customtools` model
- Native multi-turn tool call loop (MAX_TOOL_ROUNDS=8)
- `tools` and `json_mode` are mutually exclusive — system correctly distinguishes these paths
- Max token ceiling raised to 65,536 for Gemini 3.x

**Agent 2 — Big Fixer refactor (`agent_pool.py`):**
- Replaces monolithic JSON blob approach with iterative native tool calling
- Uses `TOOL_DEFINITIONS` and `dispatch_tool` from `core/swarm_tools`
- Dispatcher bound to `export_dir` for path-safe operations
- Always returns `(SwarmSummary, repair_history)` 2-tuple
- 26 new tests in `test_session56_native_tools.py` targeting 379→405 baseline

**For asset purposes:** Session 56 is not yet shipped. Do not claim native Gemini function calling is live. The pipeline's self-correcting behavior (Phase 3 Fix Loop) is already shipped and accurate to describe; Session 56 is making the internals cleaner, not changing the observable behavior description.

---

### 9. New `canopy-ui/index.html` — Real Functional UI (Light Theme)

The new UI (260 lines) is a complete departure from the old dark-forest demo:
- **Light/neutral theme** — no dark forest aesthetic, cleaner and simpler
- **Three states:** Welcome → Conversation → Building (functional, not illustrative)
- **Model selector:** Claude (recommended), Gemini, Local (Nemo) — actual working options
- **"Load & Build" path** for users with an existing `PROJECT_CONTEXT.json`
- **CS3 Research Modal** — clickable research citations with source detail
- **Snapshot Panel** — version history (3-slot system)
- **Connects to `app.js` and `styles.css`** — this is the real installable product

**Changes needed before this file is public-facing:**
- Title updated to "Canopy Seed — Naturally Growing Software" ✅ (applied in this session)
- Meta tags added: description, OG, Twitter Card with canopyseeds.com ✅
- Stats row added to welcome card: $0.31 per app · under 5 min · 168 tests ✅
- Contact/domain added to privacy note ✅

---

### 10. New `canopy-ui/devhub.html` — Developer Hub (Light/Cream Theme)

The new DevHub (1194 lines) is the technical operator view:
- **Vault startup modal (ADR-040)** — handles first-launch setup, unlock, Forge bypass, profile selection
- **Profile picker:** Gemini (default), Claude, Qwen
- **Agent monitoring cards** — live status of agent pool
- **Build log panel** — SSE-driven real-time log
- **Test results, auto-fix results** — live streaming
- **Giant Brain review section** — post-build audit output
- **Complexity Judge Calibration panel** — drift detection and adjustment
- **SSE Debug Panel** (Ctrl+Shift+D) — developer testing tool, not visible to end users
- **Green-dark palette:** `--green-dark: #2D5016`

**Status:** No domain/contact additions needed — this is an internal developer tool, not a public-facing page. No hardcoded test numbers to update. The `⚠ Drift detected` message is an intentional UI state, not a stale data issue.

---

### Updated Critical Items List

**✅ RESOLVED:**
- Timeline: 3 weeks, under $200 — confirmed and reflected in all assets
- Domain: canopyseeds.com ✅
- Email: Contact@canopyseed.com ✅
- Phase 5/6 shipped: TesterSwarm and AutoFix loop fully operational ✅
- Model routing confirmed (no GPT-4.1 in any assets) ✅
- UI path: canopy-ui/ is confirmed as the real product ✅

**⚠️ STILL OPEN:**
- "We don't force anyone to do anything. We just stick more tokens in." — Not found in any source doc. Still unverified. Do not use in launch materials.
- `ARCHITECTURE.md`, `DECISIONS.md`, `CONTRIBUTING.md` — referenced in README but not confirmed to exist. Verify before going public with the repo link.
- GitHub Pages URL (`tyoung515.github.io/canopy-seed-demo`) — confirm live or update to canopyseeds.com.
- Session 56 ships more tests (target 405). When verified, update the public metric from 168.

---

### Updated Scorecard (v3)

| File | Status | Notes |
|------|--------|-------|
| `canopy-ui/index.html` (new real UI) | ✅ Updated | Domain, meta tags, stats added this session |
| `canopy-ui/devhub.html` (new DevHub) | ✅ Current | Internal tool, no public-facing changes needed |
| `PROJECT_STATE.md` (Session 41) | 🔒 Internal | Confirms architecture, test count, vault system |
| `SESSION_56_AGENT1.md` / `AGENT2.md` | 🔒 In progress | Session 56 native function calling — not yet shipped |
| `canopy-seed-demo/index.html` (old demo) | ⚠️ Superseded | Light edits applied; real UI is canopy-ui/. Use old demo for illustrative assets only. |
| `canopy_asset_ready.md` | ✅ Updated | Three-chapter narrative, verified metrics, domain/email |
| `canopy_review_report.md` | ✅ This doc | Current through March 14, v3 update applied |

*Last updated: March 14, 2026 — v3. Sources added: PROJECT_STATE.md (Session 41), SESSION_56_AGENT1.md, SESSION_56_AGENT2.md, new canopy-ui/index.html, new canopy-ui/devhub.html, Travis direct confirmation on domain and email.*

