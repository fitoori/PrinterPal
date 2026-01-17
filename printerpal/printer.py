from __future__ import annotations

import os
import re
import shlex
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .util import CmdResult, CommandError, PrinterPalError, run_cmd


_LPSTAT_PRINTER_RE = re.compile(r"^printer\s+(?P<name>\S+)\s+(?P<state>idle|disabled|busy)\s+.*", re.I)
_CUPS_PRINTER_INFO_RE = re.compile(r"^Info\s+(?P<info>.+)$")
_CUPS_PRINTER_START_RE = re.compile(r"^<Printer\s+(?P<name>[^>]+)>")

_CUPS_PRINTER_CONF_PATHS = (
    "/etc/cups/printers.conf",
    "/etc/cups/printers.conf.O",
)


@dataclass(frozen=True)
class PrinterInfo:
    name: str
    state: str
    is_default: bool
    accepting: Optional[bool]
    display_name: Optional[str]


def _safe_timeout() -> float:
    # Pi + CUPS can be sluggish; keep timeouts reasonable.
    return 6.0


def cups_available() -> bool:
    try:
        run_cmd(["lpstat", "-r"], timeout_s=_safe_timeout(), check=False)
        return True
    except PrinterPalError:
        return False


def get_default_printer() -> str:
    try:
        res = run_cmd(["lpstat", "-d"], timeout_s=_safe_timeout(), check=False)
        # Example: "system default destination: HP_LaserJet"
        m = re.search(r"destination:\s*(\S+)", res.stdout)
        return m.group(1) if m else ""
    except PrinterPalError:
        return ""


def _load_cups_printer_info() -> Dict[str, str]:
    info_map: Dict[str, str] = {}
    for path in _CUPS_PRINTER_CONF_PATHS:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                current: Optional[str] = None
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    start = _CUPS_PRINTER_START_RE.match(line)
                    if start:
                        current = start.group("name").strip()
                        continue
                    if line.startswith("</Printer>"):
                        current = None
                        continue
                    if current:
                        info = _CUPS_PRINTER_INFO_RE.match(line)
                        if info:
                            label = info.group("info").strip().strip('"')
                            if label:
                                info_map[current] = label
        except OSError:
            continue
        if info_map:
            break
    return info_map


def get_default_printer_display() -> str:
    default = get_default_printer()
    if not default:
        return ""
    info_map = _load_cups_printer_info()
    return info_map.get(default, default)


def list_printers() -> List[PrinterInfo]:
    default = get_default_printer()
    info_map = _load_cups_printer_info()
    printers: List[PrinterInfo] = []

    res = run_cmd(["lpstat", "-p"], timeout_s=_safe_timeout(), check=False)
    for line in (res.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LPSTAT_PRINTER_RE.match(line)
        if not m:
            continue
        name = m.group("name")
        state = m.group("state").lower()
        accepting = None
        printers.append(
            PrinterInfo(
                name=name,
                state=state,
                is_default=(name == default),
                accepting=accepting,
                display_name=info_map.get(name),
            )
        )

    # Attempt to fill accepting info.
    acc = run_cmd(["lpstat", "-a"], timeout_s=_safe_timeout(), check=False)
    for line in (acc.stdout or "").splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        name = parts[0]
        accepting = True
        if "not" in parts and "accepting" in parts:
            accepting = False
        for p in printers:
            if p.name == name:
                printers[printers.index(p)] = PrinterInfo(
                    name=p.name,
                    state=p.state,
                    is_default=p.is_default,
                    accepting=accepting,
                    display_name=p.display_name,
                )
                break

    return printers


def queue_jobs() -> List[Dict[str, Any]]:
    res = run_cmd(["lpstat", "-o"], timeout_s=_safe_timeout(), check=False)
    jobs: List[Dict[str, Any]] = []
    # Example: "HP_LaserJet-12  user  1024  Mon 01 Jan 2026 10:00:00 AM"
    for line in (res.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        job_id = parts[0]
        user = parts[1]
        size = parts[2]
        jobs.append({"job_id": job_id, "user": user, "size": size, "raw": line})
    return jobs


def job_stats() -> Dict[str, Any]:
    stats: Dict[str, Any] = {}

    # Completed jobs count
    completed = run_cmd(["lpstat", "-W", "completed", "-o"], timeout_s=_safe_timeout(), check=False)
    completed_lines = [ln for ln in (completed.stdout or "").splitlines() if ln.strip()]

    # Active jobs
    active = queue_jobs()

    stats["completed_jobs"] = len(completed_lines)
    stats["active_jobs"] = len(active)
    stats["last_completed_raw"] = completed_lines[-1] if completed_lines else ""

    return stats


def scheduler_status() -> Dict[str, Any]:
    try:
        res = run_cmd(["lpstat", "-r"], timeout_s=_safe_timeout(), check=False)
        running = "running" in (res.stdout or "").lower()
        return {"cups_scheduler_running": running, "raw": res.stdout.strip()}
    except PrinterPalError as e:
        return {"cups_scheduler_running": False, "raw": "", "error": str(e)}


def printer_detail(name: str) -> Dict[str, Any]:
    if not name:
        return {}

    out = run_cmd(["lpstat", "-l", "-p", name], timeout_s=_safe_timeout(), check=False)
    txt = (out.stdout or "").strip()
    return {"name": name, "detail": txt}


def print_file(
    file_path: str,
    *,
    printer: str | None,
    copies: int,
    title: str,
    options: List[str] | None = None,
    timeout_s: float = 30.0,
) -> CmdResult:
    if not os.path.exists(file_path):
        raise PrinterPalError(f"File does not exist: {file_path}")

    if copies < 1 or copies > 99:
        raise PrinterPalError("copies must be between 1 and 99")

    argv: List[str] = ["lp", "-n", str(copies), "-t", title]
    if printer:
        argv += ["-d", printer]

    # Defensive: force monochrome if supported; harmless if ignored.
    argv += ["-o", "print-color-mode=monochrome", "-o", "ColorModel=Gray"]

    for opt in options or []:
        # Ensure no shell injection: options are passed as individual args.
        if not isinstance(opt, str) or not opt:
            continue
        argv += ["-o", opt]

    argv.append(file_path)

    try:
        return run_cmd(argv, timeout_s=timeout_s, check=True)
    except CommandError as e:
        raise PrinterPalError(
            f"Printing failed: {e.result.stderr.strip() or e.result.stdout.strip() or 'unknown error'}"
        ) from e


def cancel_job(job_id: str) -> None:
    if not job_id or not re.match(r"^[A-Za-z0-9_.-]+$", job_id):
        raise PrinterPalError("Invalid job id")
    run_cmd(["cancel", job_id], timeout_s=_safe_timeout(), check=True)
