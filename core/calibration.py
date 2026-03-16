import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import aiosqlite

from core.complexity_judge import TIER_TOKEN_MIDPOINTS, judge_task_static


BENCHMARK_TASKS = [
    {"tier": "lite",     "title": "Add a docstring", "description": "Add a one-line docstring to an existing Python function.", "target_files": ["benchmark/lite.py"]},
    {"tier": "standard", "title": "Write a sort function", "description": "Implement merge sort in Python with type hints.", "target_files": ["benchmark/standard.py"]},
    {"tier": "brain",    "title": "Design a caching layer", "description": "Design and implement an async LRU cache with TTL expiry.", "target_files": ["benchmark/brain.py"]},
    {"tier": "heavy",    "title": "Architect a plugin system", "description": "Design a plugin system with dynamic loading, dependency resolution, and sandboxing.", "target_files": ["benchmark/heavy.py"]},
]


@dataclass
class BenchmarkRun:
    tier: str
    predicted_tokens: int
    actual_tokens: int
    drift_pct: float
    flagged: bool


@dataclass
class CalibrationResult:
    runs: List[BenchmarkRun] = field(default_factory=list)
    any_flagged: bool = False
    suggested_adjustments: Dict[str, float] = field(default_factory=dict)


class CalibrationSystem:
    DRIFT_SINGLE_THRESHOLD = 30.0
    DRIFT_SUSTAINED_THRESHOLD = 15.0

    def __init__(self, ai_backend, db_path="memory/calibration.db"):
        self.ai_backend = ai_backend
        self.db_path = Path(db_path)

    async def _ensure_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS benchmark_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tier TEXT NOT NULL,
                    predicted_tokens INTEGER NOT NULL,
                    actual_tokens INTEGER NOT NULL,
                    drift_pct REAL NOT NULL,
                    flagged INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.commit()

    def _backend_for_tier(self, tier: str) -> str:
        if tier in {"lite", "standard"}:
            return "gemini"
        return "claude"

    async def _persist_run(self, run: BenchmarkRun) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO benchmark_runs (tier, predicted_tokens, actual_tokens, drift_pct, flagged)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run.tier,
                    run.predicted_tokens,
                    run.actual_tokens,
                    run.drift_pct,
                    1 if run.flagged else 0,
                ),
            )
            await db.commit()

    async def run_benchmark(self) -> CalibrationResult:
        await self._ensure_db()
        result = CalibrationResult()

        for task in BENCHMARK_TASKS:
            judge = judge_task_static(
                task_description=f"{task['title']}: {task['description']}",
                files=task.get("target_files", []),
            )

            predicted_tokens = int(TIER_TOKEN_MIDPOINTS.get(judge.tier, TIER_TOKEN_MIDPOINTS.get(task["tier"], 1000)))

            response = await self.ai_backend.complete(
                backend=self._backend_for_tier(task["tier"]),
                system="You are a coding assistant.",
                message=task["description"],
                max_tokens=8192,
            )
            actual_tokens = len((response or "").split())

            if predicted_tokens <= 0:
                drift_pct = 0.0
            else:
                drift_pct = abs((actual_tokens - predicted_tokens) / predicted_tokens) * 100.0

            flagged = drift_pct > self.DRIFT_SINGLE_THRESHOLD
            run = BenchmarkRun(
                tier=task["tier"],
                predicted_tokens=predicted_tokens,
                actual_tokens=actual_tokens,
                drift_pct=drift_pct,
                flagged=flagged,
            )

            await self._persist_run(run)
            result.runs.append(run)

            if flagged:
                result.suggested_adjustments[task["tier"]] = (actual_tokens / predicted_tokens) if predicted_tokens else 1.0

        result.any_flagged = any(r.flagged for r in result.runs)

        history = await self.get_history(last_n=20)
        for tier in {"lite", "standard", "brain", "heavy"}:
            tier_history = [r for r in history if r.tier == tier][:5]
            if len(tier_history) == 5:
                avg_drift = sum(r.drift_pct for r in tier_history) / 5.0
                if avg_drift > self.DRIFT_SUSTAINED_THRESHOLD:
                    result.any_flagged = True
                    if tier not in result.suggested_adjustments:
                        last = tier_history[0]
                        if last.predicted_tokens > 0:
                            result.suggested_adjustments[tier] = last.actual_tokens / last.predicted_tokens

        return result

    async def apply_adjustments(self, adjustments: Dict[str, float]) -> None:
        thresholds_path = Path("config") / "complexity_thresholds.json"
        thresholds_path.parent.mkdir(parents=True, exist_ok=True)

        defaults = {"lite": 500, "standard": 1500, "brain": 4000, "heavy": 8000}

        if thresholds_path.exists():
            try:
                current = json.loads(thresholds_path.read_text(encoding="utf-8"))
            except Exception:
                current = defaults.copy()
        else:
            current = defaults.copy()

        for key, default_value in defaults.items():
            if key not in current or not isinstance(current.get(key), (int, float)):
                current[key] = default_value

        for tier, ratio in (adjustments or {}).items():
            if tier in current and isinstance(current[tier], (int, float)) and isinstance(ratio, (int, float)):
                current[tier] = current[tier] * float(ratio)

        thresholds_path.write_text(json.dumps(current, indent=2), encoding="utf-8")

    async def get_history(self, last_n: int = 5) -> List[BenchmarkRun]:
        await self._ensure_db()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT tier, predicted_tokens, actual_tokens, drift_pct, flagged
                FROM benchmark_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, int(last_n)),),
            )
            rows = await cursor.fetchall()

        return [
            BenchmarkRun(
                tier=row[0],
                predicted_tokens=int(row[1]),
                actual_tokens=int(row[2]),
                drift_pct=float(row[3]),
                flagged=bool(row[4]),
            )
            for row in rows
        ]
