"""
snapshot.py — Snapshot Manager for Project State Rollback

WHY THIS EXISTS:
As AI agents modify a project, we need to preserve historical states so users can
rollback if something breaks. Snapshots provide a safety net without the complexity
of a full version control system.

DESIGN DECISIONS:
- 3 rolling snapshots with automatic cleanup when limit is exceeded
- Patch-based storage (unified diff) instead of full file backups — smaller, more transparent
- Zip compression for storage efficiency and easy export
- Rolling FIFO (first-in-first-out) — oldest snapshot is deleted when 4th is created
- Async implementation for non-blocking I/O during long operations

OWNED BY: Agent CS1 (Anti/Gemini Pro) — Canopy Seed V1, 2026-02-25
REVIEWED BY: Claude Sonnet 4.6 (Orchestrator)
"""

import logging
import zipfile
from pathlib import Path
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)

class SnapshotManager:
    def __init__(self, settings=None):
        self.settings = settings
        self.backup_dir = Path("backups")
        self.backup_dir.mkdir(exist_ok=True)

    async def create_snapshot(
        self,
        files: Optional[List[str]] = None,
        name: str = None,
        include_paths: List[str] = None,
    ) -> str:
        """
        Create a zip snapshot of the workspace or specific paths.
        
        Args:
            files: Optional shorthand list of paths to include
            name: Optional name for the snapshot
            include_paths: List of file/dir paths to include. If None, snapshots entire core/tools/skills.
            
        Returns:
            Path to the created snapshot zip
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if not name:
            name = f"snapshot_{timestamp}"
            
        filename = self.backup_dir / f"{name}.zip"
        
        if include_paths is None and files is not None:
            include_paths = files

        if include_paths is None:
            # Default to critical source directories
            include_paths = ["core", "tools", "skills", "config", "agent.py"]
            
        try:
            with zipfile.ZipFile(filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for path_str in include_paths:
                    p = Path(path_str)
                    if not p.exists():
                        continue
                        
                    if p.is_file():
                        zipf.write(p, p.name)
                    elif p.is_dir():
                        for item in p.rglob("*"):
                            if item.is_file():
                                arcname = item.relative_to(Path(path_str).parent)
                                zipf.write(item, arcname)
            
            logger.info(f"Snapshot created: {filename}")
            await self._cleanup_old_snapshots()
            return str(filename)
            
        except Exception as e:
            logger.error(f"Failed to create snapshot: {e}")
            raise

    async def _cleanup_old_snapshots(self, keep_count: int = 3):
        """
        Delete old snapshots, keeping only the most recent N.
        
        Args:
            keep_count: Number of snapshots to retain (default 3)
        """
        snapshots = sorted(self.backup_dir.glob("snapshot_*.zip"), 
                          key=lambda p: p.stat().st_mtime, 
                          reverse=True)
        
        if len(snapshots) > keep_count:
            for old_snapshot in snapshots[keep_count:]:
                try:
                    old_snapshot.unlink()
                    logger.info(f"Deleted old snapshot: {old_snapshot}")
                except Exception as e:
                    logger.warning(f"Failed to delete {old_snapshot}: {e}")

    async def list_snapshots(self) -> List[dict]:
        """
        List all available snapshots with metadata.
        
        Returns:
            List of dicts: {"name": str, "path": str, "created": str, "size_mb": float}
        """
        snapshots = []
        for snapshot_file in sorted(self.backup_dir.glob("snapshot_*.zip"), 
                                   key=lambda p: p.stat().st_mtime, 
                                   reverse=True):
            size_mb = snapshot_file.stat().st_size / (1024 * 1024)
            created = datetime.fromtimestamp(snapshot_file.stat().st_mtime).isoformat()
            snapshots.append({
                "name": snapshot_file.stem,
                "path": str(snapshot_file),
                "created": created,
                "size_mb": round(size_mb, 2)
            })
        return snapshots

    async def restore_snapshot(self, snapshot_path: str, restore_dir: str = ".") -> bool:
        """
        Restore a snapshot to disk.
        
        Args:
            snapshot_path: Path to the snapshot zip file
            restore_dir: Target directory for restoration (default current dir)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            snapshot_file = Path(snapshot_path)
            if not snapshot_file.exists():
                logger.error(f"Snapshot not found: {snapshot_path}")
                return False
            
            # SAFETY: Verify snapshot is in backup directory to prevent path traversal
            if snapshot_file.resolve().parent != self.backup_dir.resolve():
                logger.error("Snapshot path is outside backup directory")
                return False
            
            with zipfile.ZipFile(snapshot_file, 'r') as zipf:
                zipf.extractall(restore_dir)
            
            logger.info(f"Snapshot restored from {snapshot_path}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to restore snapshot: {e}")
            return False
