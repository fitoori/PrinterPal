from __future__ import annotations

import os
from typing import Any, Dict

from .util import PrinterPalError, run_cmd


ROOT_HELPER = os.environ.get("PRINTERPAL_ROOT_HELPER", "/usr/local/sbin/printerpal-root")


def ensure_airprint_via_root_helper(timeout_s: float = 30.0) -> Dict[str, Any]:
    """Attempt to ensure printers are advertised over AirPrint.

    This relies on the `printerpal-root` helper being installed and allowed via sudo.
    """
    if not os.path.exists(ROOT_HELPER):
        raise PrinterPalError(f"Root helper not found at {ROOT_HELPER}")

    # Use sudo so the web app can run unprivileged under systemd.
    res = run_cmd(["sudo", "-n", ROOT_HELPER, "ensure-airprint"], timeout_s=timeout_s, check=True)
    out = (res.stdout or "").strip()
    return {"ok": True, "output": out}
