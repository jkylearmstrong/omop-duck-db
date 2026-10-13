"""Dynamic system RAM governor and disk-spill manager for DuckDB connections."""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from omop_etl.paths import sanitize_sql_path

__all__ = ["ResourceConfig", "detect_system_ram", "pick_spill_directory", "configure_resources"]

_SPILL_SUBDIR = "omop_duckdb_spill"


@dataclass
class ResourceConfig:
    """What :func:`configure_resources` applied to the connection."""

    total_ram_bytes: int | None
    available_ram_bytes: int | None
    memory_limit: str
    temp_directory: str
    temp_free_bytes: int | None
    threads: int
    notes: list[str] = field(default_factory=list)


def _windows_ram() -> tuple[int, int] | None:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):  # type: ignore[attr-defined]
        return None
    return int(stat.ullTotalPhys), int(stat.ullAvailPhys)


def _linux_ram() -> tuple[int, int] | None:
    info = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            info[key] = int(rest.split()[0]) * 1024
    total = info.get("MemTotal")
    avail = info.get("MemAvailable", info.get("MemFree"))
    # honour cgroup v2 / v1 container limits
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(path).read_text().strip()
            if raw.isdigit() and total and int(raw) < total:
                total = int(raw)
                avail = min(avail or total, total)
        except OSError:
            pass
    return (total, avail) if total and avail else None


def _macos_ram() -> tuple[int, int] | None:
    total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip())
    avail = total
    try:
        vm = subprocess.check_output(["vm_stat"], text=True)
        page = 4096
        pages = {}
        for line in vm.splitlines():
            if "page size of" in line:
                page = int(line.split("page size of")[1].split()[0])
            elif ":" in line:
                k, _, v = line.partition(":")
                pages[k.strip()] = int(v.strip().rstrip("."))
        avail = (pages.get("Pages free", 0) + pages.get("Pages inactive", 0)
                 + pages.get("Pages speculative", 0)) * page or total
    except Exception:
        pass
    return total, avail


def detect_system_ram() -> tuple[int | None, int | None]:
    """Return ``(total_bytes, available_bytes)`` of physical RAM; ``(None, None)`` if undetectable."""
    try:
        import psutil  # optional

        vm = psutil.virtual_memory()
        return int(vm.total), int(vm.available)
    except Exception:
        pass
    try:
        if sys.platform.startswith("win"):
            got = _windows_ram()
        elif sys.platform == "darwin":
            got = _macos_ram()
        else:
            got = _linux_ram()
    except Exception:
        got = None
    return got if got else (None, None)


def _candidate_targets() -> list[Path]:
    """Spill directories to consider; nothing is created here.

    The system drive and POSIX ``/`` are never written at their root: they spill under the temp directory.
    """
    targets: list[Path] = []
    if sys.platform.startswith("win"):
        system_drive = os.environ.get("SystemDrive", "C:").upper()
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:/")
            if not drive.exists():
                continue
            if f"{letter}:" == system_drive:
                targets.append(Path(tempfile.gettempdir()) / _SPILL_SUBDIR)
            elif os.access(drive, os.W_OK):
                targets.append(drive / _SPILL_SUBDIR)
    else:
        try:
            import psutil

            for part in psutil.disk_partitions(all=False):
                mount = Path(part.mountpoint)
                if str(mount) != "/" and os.access(mount, os.W_OK):
                    targets.append(mount / _SPILL_SUBDIR)
        except Exception:
            pass
        targets += [Path("/tmp") / _SPILL_SUBDIR, Path("/var/tmp") / _SPILL_SUBDIR,
                    Path.home() / _SPILL_SUBDIR, Path.cwd() / _SPILL_SUBDIR]
    targets.append(Path(tempfile.gettempdir()) / _SPILL_SUBDIR)
    seen, uniq = set(), []
    for t in targets:
        if str(t) not in seen:
            seen.add(str(t))
            uniq.append(t)
    return uniq


def _nearest_existing(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path(tempfile.gettempdir())


def pick_spill_directory() -> tuple[Path, int | None]:
    """Choose a writable spill directory on the volume with the most free space.

    Only the chosen directory is created. Returns ``(directory, free_bytes)``; falls back to the system temp dir.
    """
    ranked: list[tuple[int, Path]] = []
    for target in _candidate_targets():
        try:
            ranked.append((shutil.disk_usage(_nearest_existing(target)).free, target))
        except OSError:
            continue
    for free, target in sorted(ranked, key=lambda r: r[0], reverse=True):
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".write_probe"
            probe.write_text("ok")
            probe.unlink()
            return target, free
        except OSError:
            continue
    fallback = Path(tempfile.gettempdir()) / _SPILL_SUBDIR
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback, None


def _default_threads(reserve: int = 1) -> int:
    try:
        cores = len(os.sched_getaffinity(0))  # respects container / affinity limits
    except AttributeError:
        cores = os.cpu_count() or 1
    return max(1, cores - reserve)


def configure_resources(
    con,
    memory_fraction: float = 0.75,
    temp_directory: str | os.PathLike | None = None,
    threads: int | None = None,
    *,
    use_available_ram: bool = True,
    reserve_threads: int = 1,
    max_temp_directory_size: str | None = None,
) -> ResourceConfig:
    """Size a DuckDB connection to the host: RAM limit, disk-spill location, and thread count.

    Args:
        con: DuckDB connection (modified in place).
        memory_fraction: Fraction of RAM DuckDB may use for ``memory_limit`` (default 0.75).
        temp_directory: Spill directory. Default: auto-pick the writable volume with the most free space.
        threads: Thread count. Default: usable cores minus ``reserve_threads`` (minimum 1).
        use_available_ram: Base the limit on currently *available* RAM (default) instead of total RAM.
        reserve_threads: Cores left to the OS when ``threads`` is not given.
        max_temp_directory_size: Optional cap on spill size, e.g. ``"200GB"``.

    Returns:
        ResourceConfig describing what was applied. If RAM cannot be detected, DuckDB's default
        ``memory_limit`` is left untouched and a note is recorded.
    """
    if not 0 < memory_fraction <= 1:
        raise ValueError("memory_fraction must be in (0, 1].")
    notes: list[str] = []

    total, avail = detect_system_ram()
    basis = avail if (use_available_ram and avail) else total
    if basis:
        mb = max(256, int(basis * memory_fraction / (1024 * 1024)))
        con.execute(f"SET memory_limit = '{mb}MB'")
        limit = f"{mb}MB"
    else:
        limit = str(con.execute("SELECT current_setting('memory_limit')").fetchone()[0])
        notes.append("Physical RAM could not be detected; left DuckDB's default memory_limit in place.")

    if temp_directory is None:
        spill, free = pick_spill_directory()
    else:
        spill = Path(temp_directory)
        spill.mkdir(parents=True, exist_ok=True)
        try:
            free = shutil.disk_usage(spill).free
        except OSError:
            free = None
    con.execute(f"SET temp_directory = '{sanitize_sql_path(spill)}'")
    if max_temp_directory_size:
        con.execute(f"SET max_temp_directory_size = '{max_temp_directory_size}'")

    n_threads = int(threads) if threads else _default_threads(reserve_threads)
    if n_threads < 1:
        raise ValueError("threads must be >= 1.")
    con.execute(f"SET threads = {n_threads}")

    return ResourceConfig(total, avail, limit, sanitize_sql_path(spill), free, n_threads, notes)
