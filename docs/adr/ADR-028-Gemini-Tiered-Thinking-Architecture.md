# ADR-028 — Gemini 3.0 Tiered Thinking Architecture

**Status:** Logged — implementation deferred to Session 21
**Date:** 2026-03-01
**Session:** 20 (advisory from Gemini Pro 3.1)
**Baseline at logging:** 137 passing, 0 failing

---

## Context

During Session 20, after ADR-026 (Phase 0/1/2 phased architecture) was live and smoke-tested,
Gemini Pro 3.1 proposed a further refinement: instead of treating each model tier as binary
(Flash vs Pro), expose Gemini Flash 3.0's `thinking_level` parameter to create a gradient of
compute intensity within the Flash tier. This gives four distinct cost/quality levels without
touching the Pro 3.1 escalation path.

The smoke test that surfaced this proposal also confirmed the immediate need for the critic
resilience patch (ADR-028 precursor): Gemini Pro 3.1 returned 503 during the Council Review
pass, crashing the build. The critic resilience patch was applied inline in Session 20
(try/except in `_critic_call`, `CRITIC_UNAVAILABLE` sentinel, force-approve-with-notes).

---

## Problem Statement

Current model assignment (post ADR-026):

| Role | Backend string | Model |
|------|---------------|-------|
| Phase 0 Generator | `gemini-flash` | gemini-2.5-flash |
| Phase 0 Critic (Council Review) | `gemini` | gemini-3.1-pro-preview |
| Phase 1 Swarm agents | `gemini-flash` | gemini-2.5-flash |
| Giant Brain audit | `claude` | Claude Sonnet |
| Mechanical fixer | `openai-nano` | gpt-5-nano |
| Architectural fixer | `gemini` | gemini-3.1-pro-preview |

The Flash tier is binary: every Flash call gets the same compute budget regardless of the
task's complexity. A Phase 1 swarm worker writing a `__init__.py` and the Phase 0 Generator
drafting a complete test contract consume identical Flash tokens with identical latency. This
is wasteful in both directions — too much for mechanical tasks, not enough for contract
generation.

---

## Proposed Architecture

Gemini Flash 3.0 exposes a `thinking_level` parameter with (at minimum) `low/medium/high`
values, with an informal `minimal` sub-level documented in Gemini's advisory. Map these to
four named backend strings:

| Backend string | `thinking_level` | Intended use |
|---------------|-----------------|--------------|
| `gemini-flash-minimal` | `minimal` / `low` | Phase 1 Swarm workers (mechanical, obedient) |
| `gemini-flash-med` | `medium` | Phase 0 Generator (contract drafting) |
| `gemini-flash-high` | `high` | Phase 0 Critic rounds 1–2 (rigorous review at Flash cost) |
| `gemini` | — (Pro 3.1) | Phase 0 Critic round 3 escalation (fallback only) |

### Council Review loop change

Current loop: 2 revisions with Pro 3.1 as critic throughout.

Proposed loop:
```
revision 0 → Generator: gemini-flash-med → Critic: gemini-flash-high
revision 1 → Generator: gemini-flash-med → Critic: gemini-flash-high (with feedback)
cap reached → force-approve with critic_notes (no Pro 3.1 call needed in normal cases)
```

Pro 3.1 escalation only fires on revision cap when `critic_notes` is populated AND the
contract still has unresolved structural issues (optional: add a complexity heuristic).
This eliminates most Pro 3.1 calls (and their 503 exposure) from the hot path.

### Why "Dumb Generator, Smart Critic" still applies

Flash-high as critic is still a step above Flash-med as generator. The asymmetry is
preserved — the generator drafts quickly, the critic reviews rigorously — but both live
within the Flash cost envelope. Pro 3.1 becomes an emergency-only escalation rather than
the default critic.

---

## Implementation Plan

### 1. `core/ai_backend.py` — add three new backend branches

```python
elif backend == "gemini-flash-minimal":
    return await self._gemini_complete(
        system, message, max_tokens, json_mode,
        model=GEMINI_FLASH_MODEL, thinking_level="low"
    )
elif backend == "gemini-flash-med":
    return await self._gemini_complete(
        system, message, max_tokens, json_mode,
        model=GEMINI_FLASH_MODEL, thinking_level="medium"
    )
elif backend == "gemini-flash-high":
    return await self._gemini_complete(
        system, message, max_tokens, json_mode,
        model=GEMINI_FLASH_MODEL, thinking_level="high"
    )
```

Requires confirming that `_gemini_complete` accepts and passes through a `thinking_level`
kwarg. May require adding it as a parameter to the streaming and blocking call paths.

### 2. `core/agent_pool.py` — update `_generate_contract`

- Generator call: `backend="gemini-flash"` → `backend="gemini-flash-med"`
- Critic call in Council Review loop (revisions 0–1): `backend="gemini"` → `backend="gemini-flash-high"`
- Add optional Pro 3.1 escalation on cap (revision 2, if implemented)

### 3. Phase 1 Swarm dispatch

- Update `backend` in swarm worker prompt dispatch from `gemini-flash` → `gemini-flash-minimal`
- Verify `run_orchestrated_build` correctly passes backend to subtask agents

### 4. `config/settings.py` — new constants (optional)

```python
GEMINI_FLASH_MINIMAL_THINKING = "low"
GEMINI_FLASH_MED_THINKING = "medium"
GEMINI_FLASH_HIGH_THINKING = "high"
```

---

## Risks and Mitigations

**Risk:** `thinking_level` API parameter may differ from Gemini's advisory description.
**Mitigation:** Grep Gemini SDK docs before implementing. Confirm exact param name and accepted values. Do not assume — test a single call before wiring all three tiers.

**Risk:** `gemini-flash-high` at Council Review may still hit quota limits under load.
**Mitigation:** `CRITIC_UNAVAILABLE` resilience patch (inline Session 20) already handles this path — Flash-high service error degrades to force-approve with notes, not a crash.

**Risk:** Swarm workers at `gemini-flash-minimal` may produce lower-quality implementations.
**Mitigation:** Contract injection (ADR-026) constrains swarm output to the frozen API surface. The contract does the heavy lifting; the swarm just writes to spec. Lower thinking budget is appropriate.

---

## Acceptance Criteria

- `core/ai_backend.py` routes `gemini-flash-minimal`, `gemini-flash-med`, `gemini-flash-high` correctly
- Council Review loop uses Flash-high for both critic passes (not Pro 3.1)
- Pro 3.1 not called during normal contract generation (only on explicit escalation or never)
- All 137 existing tests still pass
- 2 new unit tests: one verifying `gemini-flash-high` backend string is routed with `thinking_level="high"`, one verifying `gemini-flash-minimal` routes with `thinking_level="low"`
- Smoke test (unit_converter or markdown_report) completes Phase 0 without any Pro 3.1 503 exposure in normal path

---

## Dependencies

- ADR-026 (Phase 0/1/2 architecture) — must be live ✓
- ADR-028 critic resilience patch — must be live ✓ (applied inline Session 20)
- Gemini Flash 3.0 `thinking_level` parameter confirmed available in project's SDK version

---

## Notes

- Backend string naming convention: `gemini-flash-{level}` mirrors existing `gemini-flash` pattern
- `"gemini-flash"` (no level suffix) should remain valid and route to default Flash thinking level for backward compat
- Block ID / Memory Block partitioning (also ADR-028 scope per Session 19 stub) is a separate concern and can be implemented independently. Consider splitting into ADR-029 if scope grows.
- Gemini advisory referenced "gemini-flash-3.0" model string — confirm whether this differs from current `GEMINI_FLASH_MODEL` value in settings before implementing
