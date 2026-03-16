"""
Complexity Judge — Phase 4
Scores incoming tasks on 5 dimensions and selects the most cost-efficient model tier.
Runs on Flash 2.5 Lite (cheap, fast). Result is written to the Master Changelog.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Tier Definitions ─────────────────────────────────────────────────────────

TIER_MAP = [
    (0,  3,  "lite",     "gemini-flash-lite",  False),
    (4,  6,  "standard", "gemini-2.5-flash",   False),
    (7,  10, "brain",    "claude-sonnet-4-6",  False),
    (11, 13, "heavy",    "claude-opus-4-6",    False),
    (14, 15, "escalate", "claude-opus-4-6",    True),   # human confirm required
]

THRESHOLDS_FILE = Path(__file__).parent.parent / "config" / "complexity_thresholds.json"


def _load_thresholds() -> Dict:
    """Load tuneable thresholds from config. Falls back to spec defaults."""
    defaults = {
        "file_count":     {"0": 1, "1": 3, "2": 7},    # max files for score 0/1/2
        "domain_spread":  {"0": 1, "1": 2, "2": 3},
        "ambiguity":      {"0": 0, "1": 1, "2": 3},
        "external_deps":  {"0": 0, "1": 1, "2": 2},
        "historical_fail":{"0": 0, "1": 1, "2": 2},
        "tier_bounds": [          # [min, max] per tier order (lite, standard, brain, heavy, escalate)
            [0,  3],
            [4,  6],
            [7,  10],
            [11, 13],
            [14, 15],
        ],
        "tier_names": ["lite", "standard", "brain", "heavy", "escalate"],
        "tier_models": [
            "gemini-flash-lite",
            "gemini-2.5-flash",
            "claude-sonnet-4-6",
            "claude-opus-4-6",
            "claude-opus-4-6",
        ],
    }
    try:
        if THRESHOLDS_FILE.exists():
            loaded = json.loads(THRESHOLDS_FILE.read_text(encoding="utf-8"))
            if loaded:
                defaults.update(loaded)
    except Exception as e:
        logger.warning(f"Could not load complexity_thresholds.json: {e}. Using defaults.")
    return defaults


# ── Result Dataclass ──────────────────────────────────────────────────────────

@dataclass
class JudgeResult:
    score: int
    tier: str
    recommended_model: str
    reasons: List[str]
    flag_for_human: bool
    dimension_scores: Dict[str, int] = field(default_factory=dict)
    raw_response: str = ""


# ── Scoring Helpers ───────────────────────────────────────────────────────────

def _score_file_count(file_count: int, thresholds: Dict) -> tuple[int, str]:
    t = thresholds["file_count"]
    if file_count <= int(t["0"]):
        return 0, ""
    elif file_count <= int(t["1"]):
        return 1, f"{file_count} files (2–{t['1']} -> score 1)"
    elif file_count <= int(t["2"]):
        return 2, f"{file_count} files (>{t['1']} -> score 2)"
    else:
        return 3, f"{file_count} files (8+ -> score 3)"


def _score_domain_spread(layers: int, thresholds: Dict) -> tuple[int, str]:
    t = thresholds["domain_spread"]
    if layers <= int(t["0"]):
        return 0, ""
    elif layers <= int(t["1"]):
        return 1, f"{layers} domain layers (score 1)"
    elif layers <= int(t["2"]):
        return 2, f"{layers} domain layers (score 2)"
    else:
        return 3, f"{layers} domain layers (all layers -> score 3)"


def _score_ambiguity(questions: int, thresholds: Dict) -> tuple[int, str]:
    t = thresholds["ambiguity"]
    if questions <= int(t["0"]):
        return 0, ""
    elif questions <= int(t["1"]):
        return 1, f"{questions} clarifying question needed (score 1)"
    elif questions <= int(t["2"]):
        return 2, f"{questions} questions needed (score 2)"
    else:
        return 3, "Underspecified — needs planning pass (score 3)"


def _score_external_deps(dep_level: int, thresholds: Dict) -> tuple[int, str]:
    # dep_level: 0=none, 1=api_reads, 2=api_writes, 3=cross_system
    labels = ["no external deps", "API reads", "API writes", "cross-system mutations"]
    label = labels[min(dep_level, 3)]
    score = min(dep_level, 3)
    return score, f"{label} (score {score})" if score > 0 else ""


def _score_historical_failure(failures: int, thresholds: Dict) -> tuple[int, str]:
    t = thresholds["historical_fail"]
    if failures <= int(t["0"]):
        return 0, ""
    elif failures <= int(t["1"]):
        return 1, f"{failures} prior failure on similar task (score 1)"
    elif failures <= int(t["2"]):
        return 2, f"{failures} prior failures (score 2)"
    else:
        return 3, "Pattern of failures on this task type (score 3)"


def _resolve_tier(score: int, thresholds: Dict) -> tuple[str, str, bool]:
    bounds = thresholds.get("tier_bounds", [[0,3],[4,6],[7,10],[11,13],[14,15]])
    names  = thresholds.get("tier_names",  ["lite","standard","brain","heavy","escalate"])
    models = thresholds.get("tier_models", ["gemini-flash-lite","gemini-2.5-flash",
                                            "claude-sonnet-4-6","claude-opus-4-6","claude-opus-4-6"])
    
    # Simple linear scan
    if not len(bounds) == len(names) == len(models):
        # Fallback if config is malformed
        return "standard", "gemini-2.5-flash", False

    for i, (lo, hi) in enumerate(bounds):
        if lo <= score <= hi:
            flag = (names[i] == "escalate")
            return names[i], models[i], flag
    
    # Clamp to escalate if somehow above max
    return names[-1], models[-1], True


# ── Static (Local) Scoring ────────────────────────────────────────────────────

def judge_task_static(
    task_description: str,
    files: List[str],
    file_count: Optional[int] = None,
    domain_layers: int = 1,
    ambiguity_questions: int = 0,
    external_dep_level: int = 0,
    historical_failures: int = 0,
) -> JudgeResult:
    """
    Score a task locally (no AI call) based on caller-supplied dimension values.
    Use this when the caller can determine dimensions from metadata (e.g. Brain dispatch loop).
    """
    thresholds = _load_thresholds()

    fc = file_count if file_count is not None else len(files)

    s_fc,  r_fc  = _score_file_count(fc, thresholds)
    s_ds,  r_ds  = _score_domain_spread(domain_layers, thresholds)
    s_amb, r_amb = _score_ambiguity(ambiguity_questions, thresholds)
    s_dep, r_dep = _score_external_deps(external_dep_level, thresholds)
    s_hf,  r_hf  = _score_historical_failure(historical_failures, thresholds)

    total = s_fc + s_ds + s_amb + s_dep + s_hf
    reasons = [r for r in [r_fc, r_ds, r_amb, r_dep, r_hf] if r]
    tier, model, flag = _resolve_tier(total, thresholds)

    return JudgeResult(
        score=total,
        tier=tier,
        recommended_model=model,
        reasons=reasons,
        flag_for_human=flag,
        dimension_scores={
            "file_count": s_fc,
            "domain_spread": s_ds,
            "ambiguity": s_amb,
            "external_deps": s_dep,
            "historical_failure": s_hf,
        },
    )


# ── AI-Powered Scoring (optional) ────────────────────────────────────────────

JUDGE_PROMPT_TEMPLATE = """\
You are a task complexity judge. Score this task on 5 dimensions (0–3 each).
Return JSON only. No explanation outside the JSON.

Task: {task_description}
Files involved: {file_list}
Project context summary: {context_summary}

Return:
{{
  "file_count_score": <int 0-3>,
  "domain_spread_score": <int 0-3>,
  "ambiguity_score": <int 0-3>,
  "external_deps_score": <int 0-3>,
  "historical_failure_score": <int 0-3>,
  "total": <int 0-15>,
  "reasons": ["brief reason per dimension that scored > 0"]
}}
"""


async def judge_task_ai(
    task_description: str,
    files: List[str],
    project_context: Dict[str, Any],
    ai_backend,
    model_override: str = "",
) -> JudgeResult:
    """
    Score via a single-shot AI call (Flash 2.5 Lite or override).
    Falls back to static scoring if the AI call fails.
    """
    thresholds = _load_thresholds()

    file_list_str = ", ".join(files) if files else "none specified"
    context_summary = project_context.get("summary", str(project_context)[:400])

    prompt = JUDGE_PROMPT_TEMPLATE.format(
        task_description=task_description[:800],
        file_list=file_list_str[:400],
        context_summary=context_summary[:400],
    )

    try:
        raw = await ai_backend.generate_chat_completion(
            system_prompt="You are a structured JSON task complexity judge. Return only valid JSON.",
            user_prompt=prompt,
            model_key=model_override or "gemini-flash-lite",
            max_tokens=512,
            temperature=0.1
        )

        # Strip markdown code fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        data = json.loads(cleaned)
        s_fc  = int(data.get("file_count_score", 0))
        s_ds  = int(data.get("domain_spread_score", 0))
        s_amb = int(data.get("ambiguity_score", 0))
        s_dep = int(data.get("external_deps_score", 0))
        s_hf  = int(data.get("historical_failure_score", 0))
        total = int(data.get("total", s_fc + s_ds + s_amb + s_dep + s_hf))
        reasons = data.get("reasons", [])

        tier, model, flag = _resolve_tier(total, thresholds)
        return JudgeResult(
            score=total,
            tier=tier,
            recommended_model=model,
            reasons=reasons,
            flag_for_human=flag,
            dimension_scores={
                "file_count": s_fc,
                "domain_spread": s_ds,
                "ambiguity": s_amb,
                "external_deps": s_dep,
                "historical_failure": s_hf,
            },
            raw_response=raw,
        )
    except Exception as e:
        logger.warning(f"AI judge failed ({e}), falling back to static scoring.")
        return judge_task_static(task_description, files)


# ── Pre-Flight Cost Estimate ──────────────────────────────────────────────────

PRICING_FILE = Path(__file__).parent.parent / "config" / "model_pricing.json"

def _load_pricing() -> Dict:
    defaults = {
        "claude-opus-4-6":    {"input_per_1k": 0.015,   "output_per_1k": 0.075},
        "claude-sonnet-4-6":  {"input_per_1k": 0.003,   "output_per_1k": 0.015},
        "gemini-2.5-flash":   {"input_per_1k": 0.00015, "output_per_1k": 0.0006},
        "gemini-flash-lite":  {"input_per_1k": 0.0,     "output_per_1k": 0.0},
        "lmstudio-local":     {"input_per_1k": 0.0,     "output_per_1k": 0.0},
    }
    try:
        if PRICING_FILE.exists():
            loaded = json.loads(PRICING_FILE.read_text(encoding="utf-8"))
            if loaded:
                defaults.update(loaded)
    except Exception:
        pass
    return defaults

TIER_TOKEN_MIDPOINTS = {
    "lite":     300,    # ~200–400 tokens for minimal single-function output
    "standard": 2000,   # ~1500–2500 tokens for a focused single-file change
    "brain":    7000,   # ~5500–8500 tokens for multi-file refactor
    "heavy":    20000,  # ~16000–24000 tokens for full-stack feature
    "escalate": 35000,
}

def estimate_cost(results: List[JudgeResult]) -> Dict:
    """
    Build a pre-flight cost summary from a list of JudgeResults.
    Returns dict suitable for the pre-flight card UI.
    """
    pricing = _load_pricing()
    tier_counts: Dict[str, int] = {}
    tier_tokens: Dict[str, int] = {}
    total_tokens = 0
    total_cost = 0.0

    for r in results:
        tier = r.tier
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        tokens = TIER_TOKEN_MIDPOINTS.get(tier, 4000)
        tier_tokens[tier] = tier_tokens.get(tier, 0) + tokens
        total_tokens += tokens

        model = r.recommended_model
        p = pricing.get(model, {"input_per_1k": 0.003, "output_per_1k": 0.015})
        # rough 40/60 input/output split
        total_cost += (tokens * 0.4 / 1000 * p["input_per_1k"] +
                       tokens * 0.6 / 1000 * p["output_per_1k"])

    any_escalated = any(r.flag_for_human for r in results)
    confidence = "HIGH" if len(results) <= 3 and not any_escalated else \
                 "LOW"  if any_escalated else "MEDIUM"

    return {
        "task_count":     len(results),
        "tier_counts":    tier_counts,
        "tier_tokens":    tier_tokens,
        "total_tokens":   total_tokens,
        "estimated_cost": round(total_cost, 4),
        "confidence":     confidence,
        "has_escalated":  any_escalated,
    }
