"""Ordered file discovery for workspace and project inputs."""
import os
import stat
from pathlib import Path


def iter_files(folder, skip_name):
    """Yield files in sorted path order, pruning skipped directories before opening them."""
    folder = Path(folder)
    try:
        with os.scandir(folder) as listing:
            entries = sorted(listing, key=lambda entry: os.path.normcase(entry.name))
    except OSError:
        return
    for entry in entries:
        if skip_name(entry.name):
            continue
        try:
            if entry.is_file():
                yield Path(entry.path)
            elif entry.is_dir(follow_symlinks=False):
                if os.name == "nt" and entry.stat(follow_symlinks=False).st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT:
                    continue
                yield from iter_files(entry.path, skip_name)
        except FileNotFoundError:
            continue
