import asyncio
from pathlib import Path

from core.snapshot import SnapshotManager


def test_snapshot_manager_supports_no_arg_init(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = SnapshotManager()
    assert manager.backup_dir.exists()


def test_create_snapshot_accepts_files_positional(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source_file = tmp_path / "demo.py"
    source_file.write_text("print('demo')\n", encoding="utf-8")

    manager = SnapshotManager()
    snapshot_path = asyncio.run(manager.create_snapshot([str(source_file)]))

    assert Path(snapshot_path).exists()


def test_create_snapshot_cleanup_keeps_only_latest_three(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    source_file = tmp_path / "demo.py"
    source_file.write_text("print('demo')\n", encoding="utf-8")

    manager = SnapshotManager()
    for idx in range(5):
        asyncio.run(manager.create_snapshot(files=[str(source_file)], name=f"snapshot_{idx}"))

    snapshot_files = sorted(manager.backup_dir.glob("snapshot_*.zip"))
    assert len(snapshot_files) == 3
    assert (manager.backup_dir / "snapshot_4.zip").exists()


def test_list_snapshots_returns_metadata_newest_first(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    source_file = tmp_path / "demo.py"
    source_file.write_text("print('demo')\n", encoding="utf-8")

    manager = SnapshotManager()
    asyncio.run(manager.create_snapshot(files=[str(source_file)], name="snapshot_old"))
    asyncio.run(manager.create_snapshot(files=[str(source_file)], name="snapshot_new"))

    snapshots = asyncio.run(manager.list_snapshots())

    assert snapshots
    assert snapshots[0]["name"] == "snapshot_new"
    assert snapshots[1]["name"] == "snapshot_old"
    assert snapshots[0]["path"].endswith(".zip")
    assert isinstance(snapshots[0]["size_mb"], float)


def test_restore_snapshot_rejects_path_outside_backup_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    manager = SnapshotManager()
    external_zip = tmp_path / "external.zip"
    external_zip.write_text("not a zip", encoding="utf-8")

    restored = asyncio.run(manager.restore_snapshot(str(external_zip), str(tmp_path / "restore")))

    assert restored is False


def test_restore_snapshot_returns_false_for_missing_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = SnapshotManager()

    restored = asyncio.run(manager.restore_snapshot(str(tmp_path / "backups" / "missing.zip"), str(tmp_path / "restore")))

    assert restored is False
