"""Create and safely restore Cherry runtime-data backups."""
from __future__ import annotations

import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath


def create_backup(data_dir, destination, include_keys=False, key_file=None):
    """Zip Cherry data. Secrets are excluded unless the caller explicitly opts in."""
    root = Path(data_dir).resolve()
    target = Path(destination).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    key = Path(key_file).resolve() if key_file else None
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in root.rglob("*"):
            if not path.is_file() or path.resolve() == target:
                continue
            archive.write(path, Path("ai_workspace_data") / path.relative_to(root))
        if include_keys and key and key.is_file():
            archive.write(key, "secrets/key.env")
    return target


def restore_backup(archive_path, data_dir, key_file=None, restore_keys=False):
    """Restore a Cherry ZIP transactionally, rejecting unsafe paths and preserving secrets by default."""
    archive_path = Path(archive_path)
    root = Path(data_dir).resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "r") as archive:
        members = archive.infolist()
        normalized = [PurePosixPath(item.filename.replace("\\", "/")) for item in members]
        if any(path.is_absolute() or ".." in path.parts or (path.parts and ":" in path.parts[0])
               for path in normalized):
            raise ValueError("Backup contains an unsafe file path.")
        expected = [p for p in normalized if p.parts and p.parts[0] == "ai_workspace_data"]
        if not expected:
            raise ValueError("This archive does not contain ai_workspace_data.")
        with tempfile.TemporaryDirectory(prefix="cherry-restore-") as temp:
            stage = Path(temp) / "data"
            for info, rel in zip(members, normalized):
                if not rel.parts or rel.parts[0] != "ai_workspace_data" or info.is_dir():
                    continue
                output = stage.joinpath(*rel.parts[1:])
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, output.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
            if not any(stage.iterdir()):
                raise ValueError("The backup contains no restorable data files.")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            safety = root.with_name(root.name + f".before-restore-{stamp}")
            if root.exists():
                shutil.copytree(root, safety)
            root.mkdir(parents=True, exist_ok=True)
            for item in stage.iterdir():
                dest = root / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, dest)
            if restore_keys and key_file:
                secret = next((i for i, p in zip(members, normalized) if p == PurePosixPath("secrets/key.env")), None)
                if secret:
                    key = Path(key_file)
                    if not key.exists():
                        key.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(secret) as src, key.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
            return safety if safety.exists() else None
