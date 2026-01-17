from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class CmdResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float


class PrinterPalError(Exception):
    """Base error for PrinterPal."""


class CommandError(PrinterPalError):
    """Raised when a shell command fails."""

    def __init__(
        self,
        message: str,
        *,
        result: CmdResult,
    ) -> None:
        super().__init__(message)
        self.result = result


def which(cmd: str) -> str | None:
    return shutil.which(cmd)


def run_cmd(
    argv: Sequence[str],
    *,
    timeout_s: float = 8.0,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> CmdResult:
    """Run a command with strict error handling and timeouts."""
    if not argv:
        raise ValueError("argv must not be empty")

    t0 = time.monotonic()
    try:
        cp = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ, **(env or {})),
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as e:
        raise PrinterPalError(f"Command not found: {argv[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise PrinterPalError(f"Command timed out after {timeout_s:.1f}s: {' '.join(argv)}") from e

    dt = time.monotonic() - t0
    res = CmdResult(
        argv=list(argv),
        returncode=int(cp.returncode),
        stdout=cp.stdout or "",
        stderr=cp.stderr or "",
        duration_s=float(dt),
    )

    if check and res.returncode != 0:
        raise CommandError(
            f"Command failed ({res.returncode}): {' '.join(res.argv)}",
            result=res,
        )

    return res


def atomic_write_json(path: str, obj: Any, *, mode: int = 0o640) -> None:
    """Atomically write JSON to disk."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def human_bytes(n: int) -> str:
    if n < 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    for u in units:
        if v < 1024.0 or u == units[-1]:
            if u == "B":
                return f"{int(v)} {u}"
            return f"{v:.1f} {u}"
        v /= 1024.0

    return f"{v:.1f} TB"
