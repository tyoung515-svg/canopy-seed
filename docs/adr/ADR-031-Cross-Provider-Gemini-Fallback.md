# ADR-031 — Cross-Provider Fallback: Gemini Pro → Claude Sonnet

**Status:** Approved — implementation deferred to Session 25
**Date:** 2026-03-02
**Session:** 24 (co-advisory: Claude Sonnet 4.6 + Gemini Pro 3.1)
**Baseline at logging:** 142 passing, 0 failing

---

## Context

During Session 24 smoke tests, Gemini Pro 3.1 (`gemini-3.1-pro-preview`) produced sustained
503 responses across multiple consecutive calls in the same pipeline run. The model is used
for two roles: `decompose()` (project subtask breakdown in `orchestrator.py`) and
`repair_audit()` / architectural repair (Giant Brain pass in `agent_pool.py`).

ADR-031-precursor (inline Session 24): exponential backoff was added to `_gemini_complete`
to handle transient 503s — 3 attempts with 1s/2s waits between pairs. This covers the
majority case (brief API overload). This ADR covers the sustained outage case: Gemini Pro
3.1 is unavailable for the duration of a build.

When the `backend == "gemini"` dispatch exhausts all retries, the current behaviour raises
`RuntimeError`, crashing the pipeline. There is no cross-provider recovery path.

---

## Problem Statement

Gemini Pro 3.1 serves as the sole orchestrator-tier model. Two pipeline phases depend on it:

| Call site | Role | Impact if 503 |
|-----------|------|---------------|
| `orchestrator.decompose()` | Break project into subtasks | Fallback to 1 subtask "Full Project Build" (degraded) |
| `orchestrator.repair_audit()` | Architectural repair | Unhandled crash |
| `agent_pool._run_architectural_fix()` | Giant Brain architectural fix | Unhandled crash |

The `backend == "gemini"` dispatch in `complete()` has a model-level fallback
(`GOOGLE_GEMINI_FALLBACK_MODEL`) but its default value equals the primary model string,
making it a no-op. There is no cross-provider escalation.

Claude Sonnet (`claude-sonnet-4-6`) is already wired as a backend and is used for Giant
Brain audits (`claude-audit`). It can perform decompose and repair tasks with equal or
better quality to Gemini Pro. ADR-029 established that both providers share structured
output schemas (Gemini uses `responseJsonSchema` in `generationConfig`; Claude uses
`output_schema` via `output_config.format.json_schema`) — the schemas themselves are
provider-agnostic JSON Schema objects.

---

## Proposed Architecture

### Fallback Cascade

```
Gemini Pro 3.1
  └─ attempt 1 (stream → blocking)
  └─ attempt 2 (stream → blocking, +1s wait)   ← ADR-031-precursor backoff
  └─ attempt 3 (stream → blocking, +2s wait)   ← ADR-031-precursor backoff
        │ all fail (sustained 503)
        ▼
Claude Sonnet 4.6
  └─ single attempt with schema translation
        │ fails
        ▼
  RuntimeError (hard fail — no further fallback)
```

Gemini Flash is not in the cascade. On a sustained Pro outage, Flash would give degraded
decomposition quality for the same provider quota. Claude provides comparable reasoning
quality from a fully independent infrastructure.

### Schema Translation

`decompose()` and `repair_audit()` pass `response_schema` (a dict) to `complete()`.
This parameter is Gemini-specific (`responseJsonSchema` injection). When the Claude
fallback fires, `response_schema` must be translated to `output_schema` for the Claude
path. Both accept identical JSON Schema objects — only the injection mechanism differs.

The translation happens inside the `backend == "gemini"` catch block in `complete()`,
not at the call sites. Call sites are unchanged.

```python
# Inside complete(), backend == "gemini" except block, after exhausting retries:
if response_schema:
    # Translate Gemini responseJsonSchema → Claude output_schema
    return await self._claude_complete(
        system, message, clean_history,
        max_tokens=max_tokens,
        json_mode=True,
        output_schema=response_schema,   # same dict, different injection path
    )
else:
    return await self._claude_complete(
        system, message, clean_history,
        max_tokens=max_tokens,
        json_mode=json_mode,
    )
```

### `thinking_level` Handling

Some `complete()` calls to `backend == "gemini"` pass `thinking_level` (e.g.,
`gemini-flash-med` / `gemini-flash-high` calls from Phase 0). These are Flash calls, not
Pro calls — they do not route through the `backend == "gemini"` branch and are not
affected by this ADR.

The only calls that route through `backend == "gemini"` are Pro calls from
`orchestrator.py` and Giant Brain architectural repair. None of these pass `thinking_level`.
The Claude fallback does not need to emulate a `thinking_level` parameter.

### Logging

When the Claude fallback fires, log a WARNING clearly distinguishing provider fallback
from the normal backoff retries:

```
WARNING core.ai_backend: Gemini Pro exhausted after 3 attempts — falling back to Claude Sonnet
```

---

## Implementation Plan

### 1. `core/ai_backend.py` — `complete()` dispatch, `backend == "gemini"` branch

Current structure (simplified):
```python
if backend == "gemini":
    try:
        ...
        return await self._gemini_complete(...)
    except Exception as e:
        is_http_404_or_5xx = ...
        if is_http_404_or_5xx and fallback_model and fallback_model != primary_model:
            # model-level fallback (currently dead — same model)
            ...
        raise
```

New structure:
```python
if backend == "gemini":
    try:
        ...
        return await self._gemini_complete(...)
    except Exception as e:
        logger.warning(
            f"Gemini Pro exhausted after 3 attempts — falling back to Claude Sonnet"
        )
        try:
            if response_schema:
                return await self._claude_complete(
                    system, message, clean_history,
                    max_tokens=max_tokens,
                    json_mode=True,
                    output_schema=response_schema,
                )
            return await self._claude_complete(
                system, message, clean_history,
                max_tokens=max_tokens,
                json_mode=json_mode,
            )
        except Exception as claude_exc:
            logger.error(f"Claude fallback also failed: {claude_exc}")
            raise RuntimeError(
                f"All providers failed. Gemini error: {e}. Claude error: {claude_exc}"
            ) from e
```

Remove the dead `GOOGLE_GEMINI_FALLBACK_MODEL` model-level fallback block — it adds
noise and will never fire given the current default value.

### 2. `core/ai_backend.py` — `_claude_complete` signature

Verify `_claude_complete` already accepts `output_schema: Optional[dict] = None`
(added in ADR-029). No change needed if confirmed.

### 3. `config/settings.py` — remove or correct `GOOGLE_GEMINI_FALLBACK_MODEL`

Either remove the env var default entirely, or set it to a genuinely different model
(e.g., `gemini-3-flash-preview`) for use cases outside this ADR. Document in a comment
that cross-provider fallback now handles the primary recovery path.

### 4. No changes to call sites

`orchestrator.decompose()`, `orchestrator.repair_audit()`, and
`agent_pool._run_architectural_fix()` are unchanged. The fallback is fully encapsulated
in `complete()`.

---

## Risks and Mitigations

**Risk:** Claude Sonnet produces structurally different decompose output than Gemini Pro,
breaking `_parse_subtasks()`.
**Mitigation:** DECOMPOSE_SCHEMA (ADR-029) enforces the exact JSON structure
`_parse_subtasks()` expects. Claude's constrained decoding via `output_schema` guarantees
schema-valid output. This is the same guarantee ADR-029 provides for Giant Brain audit.

**Risk:** Claude fallback on `decompose()` produces fewer/worse subtasks than Gemini Pro,
degrading Phase 1 output quality.
**Mitigation:** This is a graceful degradation, not a crash. A degraded build is
preferable to a failed build. Claude Sonnet with extended thinking is capable of high-quality
decomposition; the fallback should not produce obviously inferior results.

**Risk:** Claude API also unavailable during a Gemini outage (simultaneous provider failure).
**Mitigation:** Raises a `RuntimeError` with both error messages logged. No silent failure.
The pipeline hard-stops with a clear diagnostic rather than hanging.

**Risk:** The dead `GOOGLE_GEMINI_FALLBACK_MODEL` block is removed, breaking a use case
that was intentionally configured via environment variable.
**Mitigation:** The block only fires when `fallback_model != primary_model`, and the
default value makes them equal. Any intentional config via env var implies a non-default
value — audit environment configs before removing the block. Annotate if keeping.

---

## Acceptance Criteria

- `complete()` for `backend == "gemini"` falls back to `_claude_complete` after Gemini
  exhausts 3 attempts with backoff
- `response_schema` is correctly translated to `output_schema` in the fallback path
- `decompose()` returns a valid subtask list when Gemini Pro is mocked to 503
  (unit test with mock)
- `repair_audit()` returns a valid repair manifest when Gemini Pro is mocked to 503
  (unit test with mock)
- Claude fallback failure raises `RuntimeError` with both error messages
- WARNING log fires on provider fallback (distinguishable from backoff retries)
- All 142 existing tests still pass after implementation
- Smoke test with Gemini Pro artificially throttled (or during a real 503 window)
  completes Phase 1 via Claude fallback

---

## Dependencies

- ADR-029 (Structured Outputs) — `_claude_complete` `output_schema` param — live ✓
- ADR-031-precursor (backoff fix, inline Session 24) — live before this ADR ships

---

## Notes

- This ADR deliberately does not add a Gemini Flash tier to the cascade. Flash as an
  intermediate step between Pro and Claude would add complexity without meaningful benefit:
  the sustained-outage case (what this ADR targets) is unlikely to have Flash available
  while Pro is not. Flash and Pro share the same API endpoint and quota system.
- Future consideration: if `decompose()` is called during a Pro 503 and Claude fallback
  fires, log which provider handled the decomposition in the build record. This helps
  post-build analysis of quality differences between providers.
- The `backend == "gemini-flash-*"` paths (`gemini-flash-minimal`, `gemini-flash-med`,
  `gemini-flash-high`) are separate dispatch branches and are not affected by this ADR.
  Phase 0 generator/critic calls continue to use Flash exclusively.
