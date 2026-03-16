import asyncio
import json

from core.calibration import CalibrationSystem


def test_benchmark_returns_four_runs(mock_ai_backend, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mock_ai_backend.response = "ok short response"
    system = CalibrationSystem(mock_ai_backend, db_path=str(tmp_path / "memory" / "calibration.db"))

    result = asyncio.run(system.run_benchmark())

    assert len(result.runs) == 4


def test_drift_flagged_when_over_threshold(mock_ai_backend, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mock_ai_backend.response = "word " * 1000
    system = CalibrationSystem(mock_ai_backend, db_path=str(tmp_path / "memory" / "calibration.db"))

    result = asyncio.run(system.run_benchmark())

    assert any(run.flagged for run in result.runs)


def test_apply_adjustments_writes_json(mock_ai_backend, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    system = CalibrationSystem(mock_ai_backend, db_path=str(tmp_path / "memory" / "calibration.db"))

    asyncio.run(system.apply_adjustments({"lite": 1.5}))

    thresholds_path = tmp_path / "config" / "complexity_thresholds.json"
    data = json.loads(thresholds_path.read_text(encoding="utf-8"))
    assert data["lite"] == 750


def test_get_history_returns_runs(mock_ai_backend, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mock_ai_backend.response = "history run"
    system = CalibrationSystem(mock_ai_backend, db_path=str(tmp_path / "memory" / "calibration.db"))

    asyncio.run(system.run_benchmark())
    asyncio.run(system.run_benchmark())
    history = asyncio.run(system.get_history(last_n=5))

    assert len(history) >= 2
