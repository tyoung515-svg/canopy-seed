# ADR-023 — Giant Brain Post-Build Review

**Status:** Planned — Session 18
**Owner:** Agent 2 (core/agent_pool.py, core/tester_swarm.py)
**Priority:** V1.1 — implement before Smoke Test 20

---

## Problem

The current 3-round repair loop operates test-by-test with a narrow view. It
issues patch subtasks to Flash repair agents without seeing the full picture
across all files. This causes:

- Cross-module API mismatches that no single repair subtask can fix
- Test regeneration in repair rounds creating a moving target
- Repair loop bouncing indefinitely on coherence problems
- Flash agents rewriting tests instead of fixing implementation

The repair loop was designed for small mechanical errors. It is the wrong tool
for the class of failure most commonly seen: inconsistent APIs across modules
written independently by swarm agents.

---

## Solution

Replace the 3-round repair loop with a single Giant Brain post-build review
pass, followed by a 2-tier fixer.

### New Pipeline

```
Seeding (Sonnet)
→ Orchestration with Tier Assignment (Gem Pro)
→ Swarm by Tier with SwarmMemory (Flash)
→ TesterSwarm + pip install -e .
→ Naming Triage round 0 (Flash — fast, cheap, catches obvious import/naming errors)
→ IF still failing:
    → Giant Brain Audit (Sonnet — sees all source files + all failing test output)
    → Giant Brain emits structured Fix Manifest
    → Fixer Pass (routed by complexity — see below)
    → Re-run tests
    → IF pass: done
    → IF still failing: one more Giant Brain pass
    → IF still failing after second pass: User Guided Debug State
```

The 3-round blind repair loop is removed entirely.

---

## Giant Brain Audit

**Model:** Claude Sonnet (already used for Seeding — consistent reasoning layer)
**Input:** All source files in the export directory + full pytest failure output
**Task:** Identify root causes, not symptoms. Look at all files together.

### Fix Manifest Output Format

Giant Brain must emit a structured JSON manifest. This is a hard requirement —
narrative output is not acceptable. The manifest drives the fixer routing.

```json
{
  "summary": "brief description of root cause",
  "fixes": [
    {
      "file": "unit_converter/converter.py",
      "complexity": "mechanical",
      "description": "Rename function 'convert' to 'convert_units'",
      "action": "rename_function",
      "from": "convert",
      "to": "convert_units"
    },
    {
      "file": "unit_converter/__init__.py",
      "complexity": "mechanical",
      "description": "Export convert_units and ConversionError",
      "action": "rewrite_exports"
    },
    {
      "file": "unit_converter/converter.py",
      "complexity": "architectural",
      "description": "Add temperature conversion logic with offset-based math",
      "action": "add_feature"
    }
  ]
}
```

**Complexity classification rules (Giant Brain must follow):**
- `mechanical` — rename, add import, fix signature, wire export, add missing class
- `architectural` — multi-file logic error, missing feature, coherence redesign

Giant Brain must NEVER include a fix of type "regenerate tests" or
"rewrite test file". Tests are read-only input. If tests appear to have
unrealistic expectations, Giant Brain flags this in the summary and
escalates to User Guided Debug State immediately rather than fixing tests.

---

## 2-Tier Fixer

The fixer reads the manifest and routes each fix by complexity.

### Tier 1 — Mechanical (GPT-5 Nano)
**Model string:** `gpt-5-nano`
**When:** fix.complexity == "mechanical"
**Task:** Apply the specific fix described. Precise instructions from the
manifest. Does not need to reason about the broader system.
**Update:** model string in ai_backend.py from `gpt-4o-mini` → `gpt-5-nano`

### Tier 2 — Architectural (Gem Pro)
**Model string:** `gemini-3.1-pro-preview` (already integrated)
**When:** fix.complexity == "architectural"
**Task:** Implement the described change with full context of the affected
file and its dependencies.

Tier 1 fixes run first (they are cheap and fast). Tier 2 fixes run after,
so Tier 1 changes are visible when Tier 2 agents read the files.

---

## Rules for Giant Brain

1. Read ALL source files before forming any opinion
2. Read the FULL pytest failure output, not just the first error
3. Emit structured JSON manifest only — no narrative fix instructions
4. Never suggest rewriting or regenerating test files
5. If root cause is ambiguous, pick the most conservative fix
6. If fix would require changing the test contract, escalate to
   User Guided Debug State instead

---

## What Changes in agent_pool.py

- Remove `_run_repair_loop` (3-round loop)
- Add `_giant_brain_audit(source_files, test_output) -> FixManifest`
- Add `_run_fixer_pass(manifest: FixManifest) -> None` (tier-routed)
- Add `_rerun_tests() -> TestResult`
- Main flow: naming_triage → if failing → giant_brain_audit →
  fixer_pass → rerun → if failing → giant_brain_audit (once more) →
  if failing → user_guided_debug_state

---

## Model String Update

In ai_backend.py, update openai-nano backend:
- `gpt-4o-mini` → `gpt-5-nano`
- Update OPENAI_MODEL default in config/settings.py and .env.example

---

## Success Criteria

- Smoke Test 20 build completes with 0 failing tests without entering
  User Guided Debug State
- No test file is rewritten during the fixer pass
- Fix manifest is valid JSON with complexity field on every fix
- Tier 1 fixes execute via GPT-5 Nano, Tier 2 via Gem Pro
- Test baseline remains 97 passing, 0 failing in the pipeline's own suite

---

## Out of Scope for V1.1

- Giant Brain handling install errors (pip install -e . failures) —
  that stays with _sanitize_pyproject_toml backstop
- Giant Brain reviewing pyproject.toml — ADR-019 handles that layer
- Multi-pass fixer loops — maximum two Giant Brain passes then escalate
