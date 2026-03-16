# Canopy Seed — Architecture Guide
*How the system works, component by component, and why each decision was made the way it was.*

---

## The Big Picture

Canopy Seed is two things sharing one codebase:

1. **The Seeding Layer** — the conversation-first interface that converts human intent into a structured project context (what non-technical users see and interact with)
2. **The Execution Layer** — the AI agent swarm that takes that context and builds software against it (what developers and power users manage through the Dev Hub)

These two layers are intentionally separate. The Seeding Layer requires no technical knowledge. The Execution Layer is where the real building happens. The connection between them is `PROJECT_CONTEXT.json` — the structured document produced by the conversation that feeds directly into the agent swarm.

---

## System Architecture Overview

```
SEEDING LAYER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
User Conversation (text + images)
         ↓
    Big Brain AI         ←── Research Engine
  (asks questions,             (web fetch loop,
  builds understanding,         Flash Lite synthesis,
  detects what's missing)       DuckDuckGo queries)
         ↓
    Context Builder
  (accumulates structure,
   builds PROJECT_CONTEXT.json)
         ↓
    Snapshot System      ←── auto-saves before each change
  (3 rolling versions,
   patch-based rollback)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

                    PROJECT_CONTEXT.json
                           ↓
EXECUTION LAYER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
         Complexity Judge
       (scores 0–15 across 5 dimensions,
        selects model tier for task)
              ↓
         Agent Pool
       (dispatches to Anti/VS/local/cloud
        based on Judge recommendation)
              ↓
     ┌────────┬────────┐
  Backend  Frontend  Other
  (Anti)    (VS)    agents
     └────────┴────────┘
              ↓
         Tester Swarm
       (runs pytest on all changed files,
        parallel execution, AI failure analysis)
              ↓
         Auto-Fix Loop
       (patch-based repair, 3 retries,
        restore on repeated failure)
              ↓
         Giant Brain Review
       (Opus end-of-session audit,
        risk flag assessment)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Seeding Layer Components

### Big Brain

**What it is:** The AI model that conducts the user interview. It's not a chatbot — it has a specific job: understand what a user wants to build well enough that a team of AI agents can build it correctly.

**How it works:**
- Receives a carefully designed system prompt that defines its role as a friendly project advisor named "Canopy"
- Asks exactly one question per response — never two
- Adapts language complexity to the user (detects whether they're technical or not)
- Accepts vision input (photos, screenshots, sketches) and incorporates them naturally
- Emits internal signals that the system detects but the user never sees:
  - `RESEARCH: <query>` — tells the system to look something up before continuing
  - `CONTEXT_READY` — signals that enough has been gathered to produce a complete spec

**Model options:** Claude Sonnet (default, best reasoning), Claude Opus (heavy architectural tasks), Gemini (research-heavy projects), local models via LM Studio or Ollama (privacy-first deployments)

**Why a conversation instead of a form:** Forms require users to know what questions to answer. Conversations let the AI figure out what questions need asking. A non-technical user can't fill out a specification form. They can have a conversation.

---

### Research Engine

**What it is:** An iterative web research tool that activates when Big Brain identifies a gap in its knowledge.

**How it works:**
1. Big Brain emits `RESEARCH: <query>` mid-conversation
2. Research Engine takes that query and generates 3–5 targeted search questions from it (one AI call to Flash Lite — the cheapest capable model)
3. Each search query hits DuckDuckGo's HTML endpoint, extracting the top result links
4. The top 2 links per query are fetched using the existing `web_fetch.py` tool
5. All results are trimmed to 500 characters each (cost control) and sent to Flash Lite for a 2-3 sentence synthesis with citations
6. The synthesized result is injected back into the Big Brain conversation as a hidden system message

**What the user sees:** A small "Researching..." indicator for a few seconds, then the conversation continues with noticeably better answers. The sources appear in the sidebar.

**Why we built our own instead of using Gemini Deep Research:** Gemini Deep Research is a product feature in the Gemini web interface — it's not an API endpoint you can call programmatically. Building our own keeps the system fully integrated, avoids external product dependencies, and gives us full control over cost (Flash Lite for synthesis = negligible per-query cost).

**Cost discipline:** Every Research Engine AI call uses Flash Lite (Gemini 2.5 Flash Lite), never Sonnet or Opus. Research queries are cheap. The expensive models are reserved for decisions that need them.

---

### Context Builder

**What it is:** The accumulator. Sits underneath every Big Brain conversation, extracting structure and building the `PROJECT_CONTEXT.json` as the conversation progresses.

**What it tracks:**
- Project name and description
- Goals (what the project should achieve)
- Constraints (what it must not do, what it must be compatible with)
- Target users (who will use this)
- Technology preferences (if the user has any)
- Architecture notes (captured as Big Brain builds understanding)
- Open questions (things still unresolved)
- Research log (every ResearchEntry with query, summary, and citations)
- Conversation summary (for reference and export)

**The CONTEXT_READY trigger:** Big Brain decides when it has enough. When it emits `CONTEXT_READY`, Context Builder makes one final AI call — asking Big Brain to produce a structured JSON summary of everything it learned. That JSON populates the ProjectContext object, which is written to `exports/PROJECT_CONTEXT.json` and broadcast to the frontend via SSE.

**Why this approach:** Explicit structured extraction by the same model that conducted the conversation produces more accurate results than trying to parse unstructured chat logs after the fact. The model that learned the context is the best one to summarize it.

---

### Snapshot System

**What it is:** Version history for the project being built. Three rolling snapshots, automatically created before any agent makes changes.

**How it works:**
- Before any agent write operation, `SnapshotManager.create_snapshot()` runs
- It hashes all tracked source files and stores a bundle of what changed vs. the previous snapshot
- The three most recent snapshots are kept; the oldest is deleted when a fourth is created
- `restore_snapshot()` applies the bundle in reverse, restoring files to their previous state

**Why three:** Three snapshots gives you a meaningful undo history (current → previous → the one before that) without unbounded storage growth in a persistent process.

**Why this makes users fearless:** The single biggest friction point in letting AI agents modify real code is the fear that a change can't be undone. With Snapshot always running, that fear is unfounded. Every state is recoverable.

---

## Execution Layer Components

### Complexity Judge

**What it is:** A pre-dispatch scoring system that evaluates a task before assigning it to an agent, ensuring expensive models only handle tasks that warrant them.

**How it works:**
Scores tasks 0–15 across five dimensions:
- **File count** — how many files will likely be touched
- **Ambiguity** — how clear or unclear the task specification is
- **Risk** — whether the task touches security, data, or destructive operations
- **Context depth** — how much existing codebase context the agent needs
- **Output size** — estimated size of the expected output

**Tier mapping:**
| Score | Tier | Model |
|-------|------|-------|
| 0–3 | Lite | Flash Lite / Local |
| 4–6 | Standard | Flash 2.5 |
| 7–10 | Brain | Claude Sonnet |
| 11–13 | Heavy | Claude Opus |
| 14–15 | Escalate | Opus + human confirmation |

**Why this matters:** Without tier routing, every task goes to the most capable (most expensive) model. A task that needs Flash Lite doesn't need Opus. Over many tasks, this adds up significantly.

---

### Agent Pool

**What it is:** The orchestrator for all agent activity. Maintains a registry of available agents, dispatches tasks to the right agent based on the Complexity Judge's recommendation, tracks task state, and reports results via SSE.

**Named agents in the default configuration:**
- **Anti** (Gemini Pro) — Backend, DB layer, complex orchestration, multi-file architectural tasks
- **VS** (GPT) — Frontend, skills, router integration, execution-focused tasks
- **Brain** (Claude Sonnet) — Complex reasoning, specification writing, review
- **Giant Brain** (Claude Opus) — Reserved for end-of-session review and Escalate-tier tasks

**Task lifecycle:** Pending → In Progress → Completed / Failed. All state transitions are broadcast as SSE events to the Dev Hub dashboard.

---

### Tester Swarm

**What it is:** An automated test runner that validates every agent change by running the full pytest suite against the modified files.

**How it works:**
- Discovers all test file pairs in the project (source file ↔ test file)
- Runs them in parallel waves using asyncio semaphore (up to 8 concurrent)
- On failure: calls Flash Lite to analyze the failure output and produce a plain-language summary
- All results (pass, fail, analysis) are written to the Master Changelog DB and broadcast via SSE
- A SwarmSummary is produced at the end: total/passed/failed/duration

**Why parallel waves:** Running 16 tests sequentially takes 4x longer than running them in parallel with concurrency caps. The semaphore prevents resource exhaustion while still capturing most of the speed benefit.

---

### Auto-Fix Loop

**What it is:** When the Tester Swarm finds failures, the Auto-Fix Loop dispatches Claude Sonnet to write fixes, applies them, re-runs the tests, and retries up to three times.

**The fix strategy:**
1. Read the source file
2. Build a prompt: failure output + full source file + full test file (not just the diff)
3. Claude Sonnet produces a corrected version
4. Apply as a patch (not full file replacement — see ADR-001)
5. Re-run pytest on the specific failing test
6. If pass: mark fixed; if fail: retry with updated failure output
7. After 3 failures: restore original file from backup

**Why the full source file, not just the diff:** Providing only the diff risks the agent hallucinating missing context (variables, imports, class structure it can't see). The full file + full test file gives the agent everything it needs to produce a correct fix.

**Giant Brain review:** After all tasks complete, Claude Opus reviews all applied diffs as a batch, flags any that carry residual risk, and writes a session summary to the Changelog.

---

### Master Changelog

**What it is:** A SQLite database that records every agent action during a session — what file was read, what was modified, what the diff was, what the complexity score was, and what risk flags were identified.

**Why this exists:** Auditability. When AI agents modify code, "what changed and why" must be answerable. The Changelog answers it. The Giant Brain review gate uses it to catch things the individual agents missed. Sessions can be exported to `MASTER_CHANGELOG_{session}.md` for human review.

---

### Calibration System

**What it is:** A benchmarking tool that validates whether the Complexity Judge's predictions match reality.

**How it works:**
- Four benchmark tasks, one per tier, are run periodically
- Actual token usage is compared to predicted token usage
- If drift exceeds ±30% on a single run, or ±15% sustained over 5 runs, a flag is raised
- The user can accept or reject the suggested threshold adjustment; accepted adjustments update `config/complexity_thresholds.json`

**Why ±30% (not ±20%):** Cloud model output is inherently variable. A ±20% threshold fires false positives constantly. ±30% catches real drift without alarm fatigue. The ±15% sustained threshold catches gradual model behavior shifts that single-run variance would miss.

---

## API Server

**What it is:** An aiohttp HTTP server on port 7821 that serves all communication between the frontend dashboard and the backend Python process. Also serves the REST API that the Canopy UI calls.

**Route domains (organized by prefix):**
- `/api/health`, `/api/status` — system health
- `/api/chat` — JOAT Telegram-like chat interface
- `/api/canopy/*` — Seeding Layer endpoints (session, message, context, export, snapshots)
- `/api/devhub/agents/*` — Agent Pool management
- `/api/devhub/dispatch` — Task dispatch
- `/api/devhub/tester/*` — Tester Swarm control
- `/api/devhub/autofix/*` — Auto-Fix Loop control
- `/api/devhub/changelog/*` — Master Changelog access
- `/api/devhub/calibration/*` — Calibration system
- `/api/devhub/judge` — Complexity Judge endpoint
- `/api/devhub/events` — SSE stream (persistent EventSource connection)

**State management:** The API server uses `ExpiringDict` (TTL + LRU) for all in-memory state (`_active_swarms`, `_fix_results`, `_active_fix_loops`). Entries expire after 1 hour. Maximum 20 entries. This prevents memory leaks in a persistent process.

---

## Shell & File Safety Model

Canopy Seed inherits JOAT's four-tier shell safety model:

```
!shell <command>
    │
    ├─ flat allowlist?       → execute immediately
    ├─ confirm list?         → prompt user for "yes" → execute
    ├─ enabled categories?   → same confirm gate
    └─ else                  → elevated_pending (requires !unlock + "yes")
```

File access uses path gating: `allowed_read_paths` and `allowed_write_paths` in `config.yaml` define boundaries. Writes outside those paths require elevated approval.

**Why this matters for a tool that runs AI agents:** Agents that can execute arbitrary shell commands and write arbitrary files are dangerous without constraints. The safety model means that even if an agent produces a malicious or erroneous command, the blast radius is limited. Confirmation gates give the user a chance to catch problems before they execute.

---

## The Frontend: Canopy UI

**Technology:** Vanilla HTML, CSS, and JavaScript. No framework. No build step.

**Why no framework:** 
- Anyone can read the source without a build environment
- Deployment is "open a file in a browser"
- No dependency rot over time
- Served as `file://` protocol — zero server overhead for the UI itself

**Three-state SPA:**
1. **Welcome** — model selection and entry point
2. **Conversation** — the Big Brain chat interface with live context sidebar
3. **Building** — project review, export, and snapshot management

**SSE (Server-Sent Events):** A persistent `EventSource` connection to `/api/devhub/events` lets the Python backend push real-time updates to the browser without polling. All agent activity, research completions, context updates, and snapshot creations appear instantly in the UI.

---

## Data Persistence

All persistence is SQLite — no external database, no cloud dependency.

| Database | Contents |
|----------|----------|
| `memory/canopy.db` | Seeding sessions, research log entries |
| `memory/master_changelog.db` | Agent action log per session |
| `memory/calibration.db` | Benchmark runs, drift analysis |
| `memory/tester_swarm.db` | Swarm run history, per-file trends |
| `memory/snapshots.db` | Snapshot metadata and patch bundles |

**Why SQLite:** No server to run, no accounts to manage, no network dependency. The database is a file. It travels with the project. It can be committed to git (or not). It can be opened in any SQLite browser for inspection.

---

## The Technology Stack

| Layer | Technology |
|-------|------------|
| Backend language | Python 3.11+ |
| Web framework | aiohttp (async) |
| Database | SQLite (via aiosqlite) |
| Frontend | Vanilla HTML/CSS/JS |
| Configuration | YAML + Pydantic |
| Testing | pytest + pytest-asyncio |
| Communication | Telegram Bot API (long-polling) |
| AI backends | Anthropic Claude, Google Gemini, LM Studio, Ollama |
| Real-time UI updates | Server-Sent Events (SSE) |

---

## Design Principles

**1. Local-first.** Everything runs on the user's machine. No cloud dependency for the core loop. Data stays local by default. Cloud AI APIs are optional.

**2. No build step.** Installing Canopy Seed means cloning the repo, copying `.env.example` to `.env`, and running `python agent.py`. That's it. Non-technical users should be able to do this.

**3. Govern before you automate.** Every expansion of agent capability comes with a matching safety gate. Autonomy is earned through demonstrated reliability, not assumed.

**4. Failures are recoverable.** Snapshots before every change. Retry limits. Restore on repeated failure. Giant Brain review. The system is designed assuming agents will sometimes be wrong.

**5. Auditability is not optional.** Every agent action is logged. Every decision has a rationale. Every change can be traced back to a task, a session, and a timestamp.

**6. The gardener shapes the garden.** The user's needs drive what grows. The system doesn't decide what it becomes. The people who use it do, through the conversations they have and the problems they bring to it.
