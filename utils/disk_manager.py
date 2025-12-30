import os
import shutil
import threading
from pathlib import Path
from typing import Iterable, List, Optional


def _percent_used(path: Path) -> float:
    total, used, _ = shutil.disk_usage(str(path))
    if total <= 0:
        return 0.0
    return used / total * 100.0


def _collect_files(directory: Path) -> List[Path]:
    if not directory.exists():
        return []
    files: List[Path] = []
    for root, _, filenames in os.walk(directory):
        for name in filenames:
            files.append(Path(root) / name)
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0)
    return files


class DiskCleaner:
    """Periodically frees disk space by deleting old files."""

    def __init__(
        self,
        root: Path,
        directories: Iterable[Path],
        threshold: float = 65.0,
        target: float = 50.0,
        interval_seconds: int = 600,
    ):
        self.root = Path(root).resolve()
        self.directories = []
        seen = set()
        for entry in directories:
            if entry in (None, ''):
                continue
            path = Path(entry).expanduser()
            path = path.resolve()
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            self.directories.append(path)
        self.threshold = float(threshold)
        self.target = float(target)
        self.interval = max(60, int(interval_seconds))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name='DiskCleaner', daemon=True)
        self._thread.start()
        print(f'[disk-cleaner] started with threshold={self.threshold}% target={self.target}%')

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.clean_once()
            except Exception as exc:
                print(f'[disk-cleaner] error: {exc}')
            self._stop.wait(self.interval)

    def clean_once(self):
        usage = _percent_used(self.root)
        if usage < self.threshold:
            return
        print(f'[disk-cleaner] usage {usage:.1f}% exceeds threshold, freeing space...')
        for directory in self.directories:
            if usage <= self.target:
                break
            files = _collect_files(directory)
            for file_path in files:
                if usage <= self.target:
                    break
                try:
                    file_path.unlink(missing_ok=True)
                    print(f'[disk-cleaner] removed {file_path}')
                except Exception as exc:
                    print(f'[disk-cleaner] failed to delete {file_path}: {exc}')
                    continue
                usage = _percent_used(self.root)
        usage = _percent_used(self.root)
        if usage > self.target:
            print(f'[disk-cleaner] warning: usage still {usage:.1f}% even after cleanup')
        else:
            print(f'[disk-cleaner] cleanup complete, usage {usage:.1f}%')

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
