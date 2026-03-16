# ADR-024 — Giant Brain Fixer Hardening

**Status:** Planned — Session 20
**Owner:** Agent 1 (core/agent_pool.py, core/tester_swarm.py) + Agent 2 (prompt)
**Priority:** V1.1 — implement before Smoke Test 21

---

## Problem

Giant Brain's first live run (unit_converter, Session 19 smoke test) exposed 2 bugs:

1. Fixer rewrites `__init__.py` and drops `__version__` → editable install breaks for all
   subsequent TesterSwarm runs → Giant Brain audits against broken-install test output
2. Giant Brain cannot see the editable install error — it only receives pytest output,
   not the pip install -e . failure that precedes it. This caused 5 fix attempts on
   converter.py when the real problem was a broken `__init__.py`.

Additionally: Giant Brain emitted 5 fixes for converter.py across 2 passes
(3 in pass 2 alone) — violating the "most conservative fix" rule and wasting
Nano quota on redundant attempts.

---

## Solution

1. `__version__` preservation rule in mechanical fixer prompt for `__init__.py`
2. Install error piped into `_collect_test_output` → Giant Brain sees it as context
3. Max 2 fixes per file per manifest rule in Giant Brain system prompt
4. Return type changes reclassified as architectural in complexity rules

---

## What changes

- core/agent_pool.py: `_run_fixer_pass` (preserve `__version__` instruction)
- core/agent_pool.py: `_collect_test_output` (prepend `install_error` if present)
- core/agent_pool.py: `_giant_brain_audit` system_prompt (complexity + max-fixes rules)
- core/tester_swarm.py: `SwarmSummary.install_error` field + capture in `run()`

---

## Success Criteria

- Smoke Test 21 (unit_converter re-run): 0 failing tests, no User Guided Debug State
- `__init__.py` `__version__` attribute present after any fixer pass
- Giant Brain receives install error in its context when editable install fails
- No more than 2 fixes per file in any manifest
