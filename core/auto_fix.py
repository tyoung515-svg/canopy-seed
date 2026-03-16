import asyncio
import difflib
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import aiosqlite

from core.tester_swarm import TestResult, SwarmSummary

logger = logging.getLogger(__name__)


def _strip_markdown_fences(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""

    fenced = re.search(r"```(?:[a-zA-Z0-9_+\-]+)?\s*\n([\s\S]*?)\n```", cleaned)
    if fenced:
        return fenced.group(1).strip()

    cleaned = re.sub(r"^\s*```(?:[a-zA-Z0-9_+\-]+)?\s*", "", cleaned, count=1)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned, count=1)
    cleaned = re.sub(r"(?m)^\s*```(?:[a-zA-Z0-9_+\-]+)?\s*$", "", cleaned)
    return cleaned.strip()


@dataclass
class FixAttempt:
    test_file: str
    source_file: str
    attempt_number: int
    success: bool
    diff: str
    final_failure_output: str


@dataclass
class FixSummary:
    fixed: int
    failed: int
    restored: int
    attempts: List[FixAttempt] = field(default_factory=list)
    giant_brain_review: str = ""


class AutoFixLoop:
    def __init__(self, ai_backend, sse_broadcast=None):
        self.ai_backend = ai_backend
        self.sse_broadcast = sse_broadcast

    async def _broadcast(self, event_type: str, data: dict) -> None:
        if self.sse_broadcast is None:
            return
        try:
            await self.sse_broadcast(event_type, data)
        except Exception as exc:
            logger.debug(f"SSE broadcast failed: {exc}")

    async def _run_pytest(self, test_file: str, export_dir: str) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pytest",
            test_file,
            "-q",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=export_dir,
        )
        stdout, stderr = await process.communicate()
        output = (stdout or b"").decode("utf-8", errors="replace") + (stderr or b"").decode("utf-8", errors="replace")
        return process.returncode, output

    def _resolve_path(self, file_path: str, export_dir: str) -> Path:
        candidate = Path(file_path)
        if candidate.is_absolute():
            return candidate
        return Path(export_dir) / candidate

    async def _write_giant_brain_review(self, session_id: str, review_text: str) -> None:
        db_path = Path("memory") / "master_changelog.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS giant_brain_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    review_text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                "INSERT INTO giant_brain_reviews (session_id, review_text) VALUES (?, ?)",
                (session_id, review_text),
            )
            await db.commit()

    async def run(self, swarm_summary: SwarmSummary, export_dir: str, session_id: str = "") -> FixSummary:
        summary = FixSummary(fixed=0, failed=0, restored=0)

        failing_results = [
            result
            for result in swarm_summary.results
            if not result.passed and bool(result.source_file)
        ]

        all_diffs: List[str] = []

        for result in failing_results:
            source_path = self._resolve_path(result.source_file, export_dir)
            test_path = self._resolve_path(result.test_file, export_dir)

            if not source_path.exists() or not test_path.exists():
                summary.failed += 1
                summary.attempts.append(
                    FixAttempt(
                        test_file=str(test_path),
                        source_file=str(source_path),
                        attempt_number=1,
                        success=False,
                        diff="",
                        final_failure_output="Source or test file not found",
                    )
                )
                continue

            original_source_contents = source_path.read_text(encoding="utf-8")
            test_contents = test_path.read_text(encoding="utf-8")
            failure_output = result.failure_output or ""
            fixed_this_result = False

            for attempt in range(1, 4):
                await self._broadcast("fix_started", {"test_file": str(result.test_file), "attempt": attempt})

                current_source_contents = source_path.read_text(encoding="utf-8")
                try:
                    proposed = await self.ai_backend.complete(
                        backend="claude",
                        system="You are a strict senior Python engineer. Fix the code so the test passes. Return ONLY the raw corrected file contents. CRITICAL: Do NOT wrap your output in markdown code blocks (```python). Do NOT write any explanations.",
                        message=(
                            f"SOURCE FILE ({result.source_file}):\n{current_source_contents}\n\n"
                            f"TEST FILE ({result.test_file}):\n{test_contents}\n\n"
                            f"FAILURE OUTPUT:\n{failure_output}"
                        ),
                        max_tokens=8192,
                    )
                except Exception as exc:
                    logger.warning(
                        f"AI fix attempt failed for {result.test_file} on attempt {attempt}: {exc}",
                        exc_info=True,
                    )
                    summary.attempts.append(
                        FixAttempt(
                            test_file=str(result.test_file),
                            source_file=str(result.source_file),
                            attempt_number=attempt,
                            success=False,
                            diff="",
                            final_failure_output=str(exc),
                        )
                    )
                    await self._broadcast(
                        "fix_failed",
                        {
                            "test_file": str(result.test_file),
                            "attempt": attempt,
                            "error": str(exc),
                        },
                    )
                    failure_output = str(exc)
                    continue

                proposed = _strip_markdown_fences(proposed).strip() + "\n"

                diff = "".join(
                    difflib.unified_diff(
                        current_source_contents.splitlines(keepends=True),
                        proposed.splitlines(keepends=True),
                        fromfile=f"a/{result.source_file}",
                        tofile=f"b/{result.source_file}",
                    )
                )

                source_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.write_text(proposed, encoding="utf-8")

                returncode, latest_output = await self._run_pytest(str(test_path), export_dir)

                if diff:
                    all_diffs.append(diff)

                if returncode == 0:
                    summary.fixed += 1
                    fixed_this_result = True
                    summary.attempts.append(
                        FixAttempt(
                            test_file=str(result.test_file),
                            source_file=str(result.source_file),
                            attempt_number=attempt,
                            success=True,
                            diff=diff,
                            final_failure_output="",
                        )
                    )
                    await self._broadcast("fix_succeeded", {"test_file": str(result.test_file), "attempt": attempt})
                    break

                summary.attempts.append(
                    FixAttempt(
                        test_file=str(result.test_file),
                        source_file=str(result.source_file),
                        attempt_number=attempt,
                        success=False,
                        diff=diff,
                        final_failure_output=latest_output,
                    )
                )
                await self._broadcast("fix_failed", {"test_file": str(result.test_file), "attempt": attempt})
                failure_output = latest_output

            if not fixed_this_result:
                source_path.write_text(original_source_contents, encoding="utf-8")
                summary.failed += 1
                summary.restored += 1
                await self._broadcast("fix_restored", {"test_file": str(result.test_file)})

        if not all_diffs:
            review_text = "No changes to review."
        else:
            review_text = await self.ai_backend.complete(
                backend="claude",
                system="You are Claude Opus, performing an end-of-session code review. Be concise. Flag any residual risk.",
                message=f"Review these diffs from this build session and flag any risks:\n\n{''.join(all_diffs)}",
                max_tokens=2048,
            )

        summary.giant_brain_review = review_text
        await self._write_giant_brain_review(session_id=session_id, review_text=review_text)
        await self._broadcast("giant_brain_complete", {"review": review_text})
        return summary
