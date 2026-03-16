# Canopy Seed Architecture

This document explains how Canopy Seed works and why it was designed this way.

---

## System Overview

```
User's idea (text + optional image)
        ↓
[Big Brain Conversation]  — Claude / Gemini / Local model
  • Asks clarifying questions
  • Accepts vision input
  • Emits RESEARCH: markers when it needs external context
        ↓
[Research Engine]  — Iterative web fetch loop
  • 3–5 targeted web searches per research request
  • Synthesizes with Flash Lite (cost discipline)
  • Returns summary + citations
        ↓
[Context Builder]  — Accumulates conversation into PROJECT_CONTEXT.json
  • Structured: goals, constraints, architecture notes, research log
  • CONTEXT_READY emitted by Big Brain when spec is complete
        ↓
[Snapshot System]  — 3 rolling project snapshots
  • Patch-based (unified diff, not full file rewrite)
  • Rollback in one click to any previous state
        ↓
[AI Agent Swarm]  — Executes against project context (optional)
  • Extends JOAT's agent orchestration
  • Complexity judge routes tasks to appropriate model tier
        ↓
[Project Output]  — Working code in your exports/ folder
```

---

## The Context Problem

Most people's first interaction with AI fails at the input stage. They don't provide enough context. They don't know what they don't know. So the AI produces something generic, or wrong, or close-but-not-quite — and the user concludes "AI can't do what I need."

The existing solution is prompting guides and templates. These work for technical users. They fail for everyone else.

Canopy Seed's core thesis: **Don't ask users to learn to prompt. Have a conversation with them and do the prompting on their behalf.**

The conversation happens naturally. Big Brain asks one question at a time, accepts text and images, and internally builds a structured project specification. The user never sees the structure — they just see a conversation. But behind the scenes, Canopy is extracting:

- What problem they're solving
- Who their users are
- What constraints matter (performance, security, budget)
- What research gaps exist
- What architectural preferences they have

Once the seed is mature (Canopy detects `CONTEXT_READY` in Big Brain's response), Canopy generates `PROJECT_CONTEXT.json` — a complete, AI-readable specification that downstream agents can execute against.

---

## Big Brain Design

### Why a Conversation Interface Instead of a Form

Forms force users to know what they need to specify. Conversations let the AI figure out what to ask.

A form for "build software" would have 50 fields: "deployment target?", "expected users?", "data sensitivity?", "integration points?". Most users would skip half of them or fill them with generic answers.

A conversation goes like this:

User: "I want a task manager."

Big Brain: "What's the biggest pain point with existing task managers for you?"

User: "They're too complicated. I just need to know what's due today."

Big Brain: "Got it. How many people use your task system — just you, a team, or open to anyone?"

By asking context-dependent questions, Big Brain steers the conversation toward complete context without overwhelming the user. And the user can say "I don't know" — Big Brain then triggers the research engine to find an answer.

### Why Model-Agnostic (Not Locked to One Provider)

If Canopy locked users to Claude, anyone without an Anthropic account is locked out. If it locked to Gemini, same problem.

Users should pick based on:
- **What they already have access to.** If you use Google Workspace, Gemini makes sense.
- **What works better for their project.** Claude is better for architectural reasoning. Gemini is better for research-heavy specs.
- **Privacy concerns.** A local model via LM Studio never leaves your computer.
- **Cost.** Local models are free. API costs vary by provider.

Canopy detects `RESEARCH:` markers in any AI response, so any model can trigger the research engine. Context builder accumulates conversation regardless of which backend Big Brain uses.

### Why RESEARCH: and CONTEXT_READY Are Hidden from the User

These are semantic tokens in Big Brain's response stream. If we exposed them to the user, it would break the illusion of a natural conversation.

Instead: Big Brain knows to emit them. Canopy detects them server-side. The UI shows progress ("Researching...") without exposing the machinery.

Example:

```
User: How do I sync a mobile app with a backend?

Big Brain's response to Canopy:
"RESEARCH: State management patterns for mobile-web sync
Let me look into the latest approaches for that...
[user sees: "Researching state management patterns..."]

Big Brain continues after research:
"Based on current best practices, most teams use one of three patterns:
1. Eventual consistency with conflict resolution (like Realm/MongoDB sync)
2. Optimistic updates with server reconciliation (React Query pattern)
3. Custom websocket protocol...
```

The user sees one continuous response. They never see the `RESEARCH:` token or the fact that a web search happened.

### System Prompt Design

Big Brain operates with a custom system prompt that:
- Emphasizes clarifying questions over immediate solutions
- Instructs it to emit `RESEARCH:` when external context is needed
- Defines what `CONTEXT_READY` means (conversation is complete enough to build from)
- Sets boundaries (won't commit to unrealistic timelines, will flag risky assumptions)

The system prompt was designed to match how a good technical PM conducts requirements — asking, listening, probing assumptions, knowing when you have enough to move forward.

---

## Research Engine

### Why We Built Our Own Instead of Using Gemini Deep Research

Gemini Deep Research is a black-box service that does multi-step research and returns a long document. Canopy needs something different:

1. **Speed.** Gemini Deep Research takes 2+ minutes. Canopy's research calls happen during a conversation — users expect 10–20 second latency.
2. **Transparency.** We need to log sources and citations so users can verify what we researched.
3. **Control.** We need to route searches to specific sources and apply custom filtering.
4. **Cost.** Gemini Deep Research is expensive. We use Flash Lite for synthesis (much cheaper).

So we built a simpler pipeline:
- Query → 3–5 targeted web searches → fetch and filter results → synthesize with Flash Lite → return summary + citations

This is less ambitious than Deep Research, but faster, cheaper, and more transparent.

### The Iterative Fetch Strategy

Instead of one big "search the web" call, we use a waterfall:

1. Query → Generate 3 search variations
2. Fetch top 5 results per variation
3. Filter results by domain policy (no payment sites, no social media, only HTTPS)
4. Extract relevant sections from HTML (strip ads, templates, nav)
5. Pass filtered text to Flash Lite with: "Summarize this for a developer building X"
6. Flash Lite synthesizes + returns citations

If Flash Lite says "I need more about topic Y", we can iterate. But in practice, one pass is enough.

### Why Flash Lite for Synthesis (Cost Discipline)

We could use Claude Sonnet for synthesis (higher quality). But for a 10–15 second research call during a conversation, Sonnet is overkill. Flash Lite is 1/10th the cost and still good enough for summaries.

The trade-off: slightly lower quality synthesis in exchange for 10x cost savings and faster latency. For the research engine's job (filling context gaps), this trade-off is correct.

### The DuckDuckGo HTML Approach and Why

Canopy's research engine uses DuckDuckGo because:
- **No API key required.** Works out of the box. Anyone can use Canopy without configuring search APIs.
- **Straightforward HTML fetching.** Results are HTML; we extract them and pass to Flash Lite.
- **Privacy-friendly.** DuckDuckGo doesn't track; results aren't personalized.

The downside: less sophisticated than Google Search (which we couldn't use without an API key anyway). But for the typical research queries Canopy makes ("React state management patterns", "Python async best practices"), DuckDuckGo results are sufficient.

---

## Snapshot System

### Why Patch-Based Diffs Instead of Full File Backups

A snapshot could store:
1. **Full file backups.** Easy to understand, simple to restore, but storage-heavy. A 50-file project × 3 snapshots × 100KB/file = 15MB per session.
2. **Unified diffs.** Smaller, more transparent (you see exactly what changed), harder to restore wrongly.

Canopy uses unified diffs because:
- **Storage.** A diff is typically 5–10KB per change; full backup is 100KB+.
- **Transparency.** You can read a diff and understand what the agents changed.
- **Auditability.** Diffs are perfect for changelogs and post-hoc analysis.

Restoration is simple: apply the diff in reverse order (newest to oldest) until you reach the desired state.

### The 3-Snapshot Limit and Why

Canopy keeps 3 rolling snapshots. When a 4th is created, the oldest is deleted.

Why 3?
- **Not 1:** You need at least one fallback if the current state breaks.
- **Not 10:** Snapshot cleanup becomes complex, storage grows, users get confused by too many choices.
- **3:** Sweet spot. Covers "latest", "previous", "last good state". Matches how most developers mentally model undo.

Why rolling (not indefinite)? Because this is a **conversation-driven workflow**, not a version control system. Git's job is long-term history. Snapshots' job is safety net during active development. Keeping 3 is enough; older states live in Git if the user exports.

### How Rollback Is Safer Than Undo in Most Tools

Most editors implement "undo" as a linear stack — each keystroke gets reversed. This is dangerous in Canopy because:
- **Agents made many changes.** Undoing one change atomically is wrong (undo 1 of 50 changes leaves the code in an inconsistent state).
- **We can't know causality.** Did change 10 depend on change 5? An undo stack doesn't know.

Snapshots avoid this:
- **All-or-nothing.** You rollback to a complete state, not a partial one.
- **Explicit boundaries.** Each snapshot is a checkpoint the agents agreed on (or you created).
- **Transparent.** You can see the diff before rolling back and decide if it's what you want.

---

## The Stability Decisions

Canopy includes three architectural decisions that came from hard-learned lessons in AI system design. They prevent entire classes of bugs:

### 1. ExpiringDict: Why Unbounded Dicts Are Dangerous

**The problem:** In a long-running Python process that never restarts, dicts grow unbounded. If your code ever does `state[user_id] = {...}` without cleanup, one day you'll wake up to a process consuming 20GB of RAM.

**The solution:** `ExpiringDict` (from the `expiringdict` library) automatically evicts old entries based on TTL (time-to-live) or LRU (least-recently-used).

Canopy uses this for:
- Conversation history (auto-clear old sessions)
- Pending commands (auto-expire if user doesn't confirm)
- Research results (auto-clear if not used)

**Where this came from:** This decision was recommended in a GPT architectural audit of the original JOAT system. The audit found that JOAT's conversation memory could grow unbounded. We fixed it in Canopy at day 1.

### 2. Patch-Based Auto-Fix: Why Full File Replacement Is Dangerous

If an AI agent writes a bug, Canopy's auto-fix loop tries to fix it. But how?

**Wrong approach:** Have Sonnet read the entire file and write back a corrected version.
- **Risk:** Sonnet might misunderstand context and change things that weren't broken.
- **Irreversibility:** If Sonnet's "fix" introduces a new bug, you've lost the original.

**Right approach:** Have Sonnet write only the corrected function/class as a patch, then apply it with full-file restoration capability.

Concretely:
1. Test fails on `functions.py`
2. Sonnet gets: failure message + just the failing function
3. Sonnet writes back a corrected function
4. Canopy applies the patch
5. Tests re-run
6. If still failing: restore original, try again with more context
7. After 3 failures: restore original and mark "human review needed"

**Where this came from:** GPT's architectural audit flagged that full-file replacement is a single point of failure. One bad replacement can break everything downstream.

### 3. Calibration Drift ±30%/±15% Sustained (Not ±20% Single-Run)

Canopy's complexity judge uses calibration to predict which model tier a task needs. But how tight should the threshold be?

**Early approach:** ±20% drift allowed per single run.
- **Problem:** Noise is high. A task might hit ±20% on Tuesday and ±5% on Wednesday. Tight thresholds thrash between model tiers.

**Better approach:** ±30% burst / ±15% sustained.
- **±30% allowed on a single run.** One task being 30% over budget is fine; it's noise.
- **±15% sustained across 3+ runs of the same task.** If the same task is consistently 15% over budget, the threshold is wrong; adjust it.

This prevents constant tier thrashing while still catching systematic underestimates.

**Where this came from:** Gemini's architect review of Canopy's design flagged that tight calibration thresholds create instability. The ±30/±15 split was recommended as a pragmatic balance between sensitivity and stability.

---

## What Was Deliberately Not Done

### No Cloud Backend

Canopy has no cloud infrastructure. No servers. No user accounts. No cloud sync.

**Why?**
1. **Security.** Your project context stays on your machine. If you're planning proprietary software, it never leaves your disk.
2. **Trust.** You can read every line of code Canopy runs. If there's a cloud backend, there are attack vectors you can't audit.
3. **Cost.** No cloud = no monthly bills. Pay once, run forever.

**Trade-off:** You can't access your project from multiple machines. You have to run Canopy locally or sync your exports folder manually.

This is the right trade-off for V1. If users want cloud sync later, it can be added without breaking the local-first core.

### No User Accounts

Canopy doesn't ask for login credentials or user registration.

**Why?**
1. **Friction.** Account signup is one more barrier. Many people try something once and never come back if there's friction at the start.
2. **For V1, it's unnecessary.** Canopy runs on your computer; it knows who you are (you own the computer).

**Trade-off:** No multi-user access per machine, no account recovery if you forget a password, no cloud backup of your projects.

This is correct for a local-first tool. If Canopy becomes widely used and people want to share projects, multi-user can be added to the local version (permissions, shared folders, etc.).

### No Framework (Vanilla JS)

The Canopy UI is vanilla JavaScript, not React, Vue, or Svelte.

**Why?**
1. **No build step.** Users can download Canopy, open the HTML file, and it works. No `npm install`, no webpack, no Vite.
2. **Readability.** Anyone can open `canopy-ui/index.html` and read the UI code. With a framework, the code is abstracted into components and build artifacts.
3. **Simplicity.** For a linear UI (conversation panel, research panel, snapshot list), vanilla JS is sufficient.

**Trade-off:** The JavaScript code is longer and less componentized than it would be in React. State management is manual, not declarative. If the UI grows to 10+ complex pages, a framework might make sense.

This is the right choice for V1 public release: anyone can audit the UI without learning a framework.

### No Docker

Canopy doesn't ship Docker images.

**Why?**
1. **Lower barrier.** Users who don't know Docker can still use Canopy (just run Python).
2. **Simplicity.** Docker adds configuration complexity.
3. **Local development.** Canopy is meant to run on your machine; Docker isolates it, which adds friction.

**Trade-off:** Harder to standardize environments across team members. Windows vs. Linux paths need manual tweaking. No guaranteed reproducibility.

This is correct for an early release. Docker can be added later if people want it.

### NotebookLM Integration Is Export-Based, Not API-Based

Canopy can export your project context to NotebookLM (Google's tool that creates podcasts/notes from documents).

We export a formatted markdown file and tell you: "Upload this to NotebookLM to get an audio guide."

We don't integrate with NotebookLM's API because: **it doesn't have a public API yet.** NotebookLM is experimental. The export approach is future-proof — if NotebookLM adds an API, we can upgrade to it without breaking existing workflows.

---

## Agent Architecture (The Swarm That Built This)

Canopy Seed was built by a team of AI agents, each with clearly bounded ownership:

| Agent | Model | Role | Files Owned |
|-------|-------|------|------------|
| **CS1 (Anti)** | Gemini Pro 3.1 | Backend core | `core/`, `tools/`, `memory/`, `skills/` (except fetch) |
| **CS2 (VS)** | GPT 5.1 | Frontend UI | `canopy-ui/` |
| **CS3 (VS)** | GPT 5.1 | Research engine + export | `core/research_engine.py`, research UI, NotebookLM export |
| **CS4 (VS/GPT)** | GPT (this file) | Documentation | `README.md`, `ARCHITECTURE.md`, `DECISIONS.md`, `CONTRIBUTING.md`, code headers |
| **Orchestrator** | Claude Sonnet 4.6 | Specs, review, coordination | All agent specs, issue triage, code review |

**Why this works:**
- **Clear ownership.** Agent CS1 doesn't touch `canopy-ui/`. Agent CS2 doesn't modify `core/`. No conflicts.
- **Explicit boundaries.** Each agent gets a spec file with: exact file paths, function signatures for shared interfaces, a DO NOT TOUCH list, test requirements.
- **Specialization.** Anti is good at backend systems. VS is good at UI/frontend. Each does what it's best at.
- **Coordination.** Sonnet (the human architect) writes the specs, reviews the outputs, and coordinates between agents if they need to coordinate.

This pattern scales. If we need to add more features, we write a spec for a new agent, give it bounded ownership, and run it.

---

## Design Trade-Offs Summary

| Decision | Chosen | Alternative | Why |
|----------|--------|-------------|-----|
| Storage | Patches (diffs) | Full backups | Smaller, transparent, auditable |
| Snapshots | 3 rolling | Unlimited | Sweet spot for safety without bloat |
| Conversation | Natural | Form-based | More accessible to non-technical users |
| Model | Agnostic | Locked to one | Users pick based on access, preference, cost |
| Research | Custom engine | Gemini Deep | Faster, cheaper, more transparent |
| Backend | Local-only | Cloud + local | Security, privacy, no vendor lock-in |
| UI | Vanilla JS | Framework | No build step, easier to audit |
| Accounts | None | Optional logins | Lower friction for V1 |
| Docker | Not included | Included | Lower barrier for non-technical users |

Each trade-off reflects this principle: **Canopy should be accessible to anyone with an idea and a computer, not just professional developers.**

---

## Testing and Quality Assurance

Canopy includes automated tests for all core modules:
- `tests/test_context_builder.py` — conversation flow, context extraction
- `tests/test_research_engine.py` — research queries, synthesis
- `tests/test_snapshot.py` — snapshot creation, rollback
- Integration tests verify the full flow from conversation to project context to agent dispatch

Run tests with: `pytest tests/ -v`

The test suite is part of the definition of correctness. If the tests pass, the system works.

---

## For Developers: Where to Start

1. Read `DECISIONS.md` to understand the trade-offs.
2. Read `core/context_builder.py` to see how conversations become structure.
3. Read `core/research_engine.py` to see how web research is integrated.
4. Look at `core/api_server.py` to see the HTTP API that the UI talks to.
5. Look at `canopy-ui/index.html` and `canopy-ui/app.js` to see how the UI works.
6. Run the tests and add a test for any feature you add.

All code has header comments. Security-critical code has inline SAFETY: comments. No orphan TODOs — every TODO is linked to an issue or resolved.

---

## Versioning and Future Direction

**V1 (current):** Local-first, conversation-first, AI-transparent.

**V2 (potential):**
- Multi-user local project sharing
- Docker support for consistent environments
- Git integration (auto-commit snapshots, branch management)
- Skill marketplace (share custom skills with others)

**Beyond:**
- Optional cloud sync (with user consent)
- Team collaboration (sharing projects across machines)
- Skill composition (chaining skills into workflows)

All future development will preserve the core principle: user control, transparency, local-first by default.
