# ADR-030 — Big Fixer & Three-Tier Post-Giant-Brain Threshold

**Status:** Approved — implementation deferred to Session 25
**Date:** 2026-03-02
**Session:** 24 (co-advisory: Claude Sonnet 4.6 + Gemini Pro 3.1)
**Baseline at logging:** 142 passing, 0 failing

---

## Context

Giant Brain (ADR-023/024) is the post-build review layer that audits failing pytest output,
generates structured fix manifests (ADR-029), and drives up to two mechanical/architectural
fix passes. When Giant Brain's two passes are exhausted the pipeline currently hard-stops,
writing a failure record and leaving the build in a broken state. There is no recovery path
short of human intervention.

Session 24 smoke tests confirmed this cliff in practice: both test runs (11:14 and 11:43)
reached the Giant Brain exhaustion point — the first due to a force-approved Phase 0 contract,
the second due to a JSON parse failure in audit pass 2 (remedied by ADR-029 structured
outputs). In both cases, fixable tests remained failing when the pipeline stopped.

This ADR defines a recovery layer that sits between Giant Brain exhaustion and human
intervention: the **Big Fixer**, a single-model fallback agent with full historical context, and
a **three-tier threshold** that routes builds to the appropriate recovery path based on how
close they are to passing.

---

## Problem Statement

Current post-Giant-Brain behaviour:

```
Giant Brain pass 1 → Giant Brain pass 2 → HARD STOP (failure record written)
```

This creates a brittle cliff: a build that is 95% passing (one edge-case test remains broken)
receives identical treatment to a build that is 30% passing (fundamental architecture wrong).
Both are discarded. There is no mechanism to:

1. Distinguish "almost there" from "fundamentally broken"
2. Give a last-resort agent the full context of what Giant Brain already tried
3. Escalate a contract-level flaw back to Phase 0 with appropriate context

Additionally, test collection errors (SyntaxError in `__init__.py`, missing imports) produce
0% pass rates that are structurally different from 0% caused by all tests failing — a
collection error is a single targeted fix, not a wholesale rebuild.

---

## Proposed Architecture

### Three-Tier Threshold Routing

After Giant Brain pass 2 exits (or a collection error is detected), compute the pass rate
from the final pytest output and route as follows:

```
┌─────────────────────────────────────────────────────────────────┐
│                   Giant Brain Pass 2 Complete                   │
│                  (or Collection Error Detected)                 │
└───────────────────────────┬─────────────────────────────────────┘
                            │
              ┌─────────────▼─────────────┐
              │  Test collection error?   │
              │  (0 collected, SyntaxErr) │
              └─────────────┬─────────────┘
                  yes │          │ no
                      ▼          ▼
              [Big Fixer]   Compute pass rate
                            (passed / total * 100)
                                  │
          ┌───────────────────────┼──────────────────────┐
          │ < LOW_THRESHOLD       │ LOW to HIGH range     │ > HIGH_THRESHOLD
          │ (default: 70%)        │                       │ (default: 95%)
          ▼                       ▼                       ▼
     DUMP → user_guided       [Big Fixer]           [Fix #3 Geared Up]
     _debug_state                                         │
                                                     pass │ fail
                                                          ▼
                                                    [Big Fixer]
```

**Big Fixer exhaustion (2 attempts):**

```
Big Fixer attempt 1 → still failing → Big Fixer attempt 2 → still failing
→ user_guided_debug_state  (with escalate_to_contract option, once per build)
```

### Tier Definitions

**Tier 1 — DUMP (`< LOW_THRESHOLD`, default 70%)**

Pass rate is too low for targeted fixing to be tractable. The failures likely indicate
contract-level design flaws, missing module dependencies, or architecture mismatches that no
fixer can resolve without regenerating the contract. Write a `user_guided_debug_state` record
with the full Giant Brain audit history for the developer to inspect.

**Tier 2 — Big Fixer directly (`LOW_THRESHOLD ≤ pass rate < HIGH_THRESHOLD`, default 70%–95%)**

Enough is passing that targeted fixes are likely to close the gap, but the remaining failures
are too numerous or varied for Fix #3's lighter approach to be worth the extra latency.
Dispatch Big Fixer immediately.

**Tier 3 — Fix #3 Geared Up (`≥ HIGH_THRESHOLD`, default 95%)**

One or two tests remain failing. The existing Giant Brain infrastructure can handle this with
augmented context. Run a third Giant Brain pass with:
- Full audit history from passes 1 and 2 injected into the system prompt
- Per-file fix cap lifted (unlimited fixes per file per pass)

If Fix #3 succeeds: build passes, done. If Fix #3 fails: hand off to Big Fixer.

**Collection Error Override**

If pytest reports 0 tests collected (SyntaxError, ImportError, or collection-abort), bypass
the threshold calculation entirely and route directly to Big Fixer regardless of any numeric
pass rate. A collection error is a single targeted fix (remove contamination, repair import),
not a sign of widespread failure.

---

## Big Fixer Design

### Model

Gemini Pro 3.1 (`gemini-3.1-pro-preview`). Rationale:
- Giant Brain audit already used Claude (via `claude-audit` backend, ADR-029). A different
  model provides a fresh perspective and avoids reinforcing the same reasoning pattern.
- Gemini Pro 3.1's large context window accommodates the full history injection.
- Pro 3.1 is already wired in `ai_backend.py` and used for architectural fixes — no new
  backend string required.

### Operation

One model, one pass, direct edits. The Big Fixer does not generate a manifest for a
downstream mechanical fixer — it reads the context, identifies fixes, and writes corrected
file contents directly.

**Context injected:**
- Final failing pytest output (full, not truncated)
- Source files for each failing test and each file referenced in tracebacks
- Giant Brain pass 1 audit manifest (what was tried, what was changed)
- Giant Brain pass 2 audit manifest (what was tried, what was changed)
- The current test contract (Phase 0 output)

**Constraints:**
- Maximum 2 attempts per build. After 2 Big Fixer attempts still failing, route to
  `user_guided_debug_state`.
- Each attempt runs pytest after edits and checks the result before attempting a second pass.
- Big Fixer may emit an `escalate_to_contract` signal if it determines the failures are
  caused by a contract-level design flaw rather than implementation errors.

### `escalate_to_contract` Exit

If Big Fixer determines that the test failures cannot be resolved by editing implementation
files (e.g., the contract specifies an API signature that is internally inconsistent, or the
contract requires functionality that conflicts across modules), it may emit:

```json
{"escalate_to_contract": true, "reason": "..."}
```

This triggers a Phase 0 re-run with:
- Big Fixer's `reason` injected as context into the Phase 0 Generator prompt
- A note that this is a second-attempt contract (to dissuade the Generator from repeating
  the same design)

**Infinite loop guard:** `escalate_to_contract` may fire **at most once per build**. If the
re-generated contract also produces a build that reaches Big Fixer exhaustion, the pipeline
routes to `user_guided_debug_state` — no further contract regeneration. This prevents a
Phase 0 → Phase 1 → Giant Brain → Big Fixer → Phase 0 loop.

---

## Fix #3 "Gear Up" Design

Fix #3 reuses the existing Giant Brain infrastructure (same `claude-audit` backend, same
GIANT_BRAIN_SCHEMA structured output, ADR-029) with two modifications:

1. **History injection:** The Giant Brain system prompt for pass 3 includes a `## Previous
   Attempts` section containing the pass 1 and pass 2 audit summaries and fix lists. This
   prevents Fix #3 from re-attempting fixes that Giant Brain already confirmed unsuccessful.

2. **Per-file cap lifted:** The `"Do not emit more than N fixes for the same file"` constraint
   is removed from the pass 3 system prompt. Since only 1-2 tests are failing (≥ 95% pass
   rate), the remaining issues are likely concentrated; the cap would only create artificial
   truncation.

Fix #3 is not a new agent — it is a configuration variant of the existing Giant Brain pass
dispatched by the threshold router.

---

## Configuration

New constants in `config/settings.py`:

```python
# Three-tier threshold routing (post Giant Brain)
BIG_FIXER_LOW_THRESHOLD = 70    # Pass rate % below which → DUMP (skip Big Fixer)
BIG_FIXER_HIGH_THRESHOLD = 95   # Pass rate % above which → Fix #3 first, then Big Fixer
BIG_FIXER_MAX_ATTEMPTS = 2      # Max Big Fixer attempts before user_guided_debug_state
```

These values may be tuned per-project without code changes.

---

## Implementation Plan

### 1. `core/agent_pool.py` — threshold router

After Giant Brain pass 2 returns, add a `_post_giant_brain_router` function:

```python
async def _post_giant_brain_router(
    self,
    pytest_output: str,
    source_map: dict,
    audit_history: list[dict],
    contract: str,
    collection_error: bool = False
) -> BuildResult:
```

- Parses pass rate from `pytest_output`
- Applies collection error override
- Routes to `_run_big_fixer()`, `_run_fix3()`, or `user_guided_debug_state`

### 2. `core/agent_pool.py` — `_run_fix3`

Wrapper around existing Giant Brain audit call with:
- `pass_number=3` injected into system prompt header
- `previous_attempts=audit_history` formatted into a `## Previous Attempts` section
- Per-file cap constraint removed from system prompt (conditionally)

### 3. `core/agent_pool.py` — `_run_big_fixer`

New method:
- Builds context bundle (pytest output + source files + audit history + contract)
- Dispatches to `backend="gemini"` (Pro 3.1) with a Big Fixer system prompt
- Parses response for direct file edits or `escalate_to_contract` signal
- Applies edits, runs pytest, evaluates result
- Loops up to `BIG_FIXER_MAX_ATTEMPTS`

### 4. `core/agent_pool.py` — `escalate_to_contract` guard

Add an `_escalate_to_contract_fired: bool` flag on the build context object.
Check before firing — if already `True`, route directly to `user_guided_debug_state`.

### 5. `config/settings.py` — new constants

Add `BIG_FIXER_LOW_THRESHOLD`, `BIG_FIXER_HIGH_THRESHOLD`, `BIG_FIXER_MAX_ATTEMPTS`.

### 6. Tests

- Unit test: `_post_giant_brain_router` routes correctly for pass rates 50%, 85%, 97%
- Unit test: collection error override fires regardless of pass rate
- Unit test: `escalate_to_contract` guard prevents second escalation
- Integration: smoke test build that previously hard-stopped at Giant Brain exhaustion
  reaches Big Fixer and either passes or writes a `user_guided_debug_state` record

---

## Risks and Mitigations

**Risk:** Big Fixer receives corrupted or incomplete context (too-large source map exceeds
Pro 3.1 context window).
**Mitigation:** Limit source file injection to files directly referenced in pytest tracebacks.
Do not inject the entire project. If a file exceeds 500 lines, inject only the relevant class
or function block with `# ... truncated ...` markers.

**Risk:** Fix #3 re-attempts the same failed fix from passes 1–2, wasting a pass.
**Mitigation:** History injection explicitly labels each prior fix as "attempted — outcome:
failed" so the model has strong signal to try a different approach.

**Risk:** `escalate_to_contract` fires on a transient issue (flaky test, environment
problem), causing an unnecessary full rebuild.
**Mitigation:** Big Fixer system prompt instructs: "Only emit `escalate_to_contract` if you
have determined that no change to implementation files can make the tests pass as written.
Prefer direct edits."

**Risk:** Threshold defaults (70%/95%) are wrong for a given project.
**Mitigation:** Both values are configurable in `config/settings.py`. The defaults are
conservative — 70% is a low bar for DUMP (most projects should clear it) and 95% is a
high bar for Fix #3 (most projects below that have enough failures to warrant Big Fixer).

---

## Acceptance Criteria

- `_post_giant_brain_router` correctly routes for all three tiers and the collection error
  override — verified by unit tests
- Big Fixer dispatches to `backend="gemini"` with correct context bundle
- Big Fixer applies file edits and re-runs pytest within the same pipeline invocation
- `escalate_to_contract` fires at most once per build — second reach of Big Fixer
  exhaustion routes to `user_guided_debug_state`
- `BIG_FIXER_LOW_THRESHOLD`, `BIG_FIXER_HIGH_THRESHOLD`, `BIG_FIXER_MAX_ATTEMPTS` are
  present in `config/settings.py` and read by the router
- Fix #3 includes `## Previous Attempts` in audit system prompt and omits per-file cap
- All 142 existing tests still pass after implementation
- Smoke test (unit_converter) that previously hard-stopped at Giant Brain exhaustion
  progresses through Big Fixer or Fix #3 and either passes the build or writes a
  structured `user_guided_debug_state` record (no unhandled exception, no hard stop)

---

## Dependencies

- ADR-023 (Giant Brain Post-Build Review) — must be live ✓
- ADR-024 (Giant Brain Fixer Hardening) — must be live ✓
- ADR-029 (Structured Outputs + Adaptive Thinking) — must be live ✓ (Session 24)
- `config/settings.py` pattern established — live ✓

---

## Notes

- The DUMP path (`< LOW_THRESHOLD`) writes a `user_guided_debug_state` record, not a bare
  failure. This record should include: the Phase 0 contract, the full Giant Brain audit
  history for both passes, the final pytest output, and a human-readable summary of what
  was tried. This makes the hand-off to the developer as actionable as possible.
- "Big Fixer" is a working name. Implementation may use any identifier in code.
- Phase 1 context injection (inject neighboring module API summaries at swarm dispatch time)
  is a related future improvement that would reduce the frequency of Giant Brain and Big
  Fixer invocations by giving swarm workers better inter-module awareness. Deferred to a
  separate ADR.
- The three-tier thresholds were proposed jointly by Claude Sonnet 4.6 and Gemini Pro 3.1
  during Session 24 advisory. Gemini's additions: configurable thresholds in settings.py,
  infinite loop guard on `escalate_to_contract`.
