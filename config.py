from __future__ import annotations

import os
import secrets
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict

from .util import PrinterPalError, atomic_write_json, read_json


DEFAULT_CONFIG_PATH = os.environ.get("PRINTERPAL_CONFIG", "/etc/printerpal/config.json")


def _default_config() -> Dict[str, Any]:
    return {
        "app": {
            "host": "0.0.0.0",
            "port": 80,
            "secret_key": secrets.token_hex(32),
            "max_upload_mb": 25,
        },
        "printing": {
            "default_printer": "",
            "preview_dpi": 150,
            "print_dpi": 200,
            "max_pdf_pages_process": 30,
            "default_copies": 1,
            "default_mode": "grayscale",  # raw|grayscale|bw|dither|outline
            "bw_threshold": 180,
        },
        "airprint": {
            "auto_enable": True,
        },
        "ui": {
            "default_dark_mode": False,
            "default_eink_mode": False,
        },
        "security": {
            "require_token": False,
            "token": "",
        },
    }


def _as_int(v: Any, *, min_v: int, max_v: int, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise PrinterPalError(f"{name} must be a number")
    iv = int(v)
    if iv < min_v or iv > max_v:
        raise PrinterPalError(f"{name} must be between {min_v} and {max_v}")
    return iv


def validate_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize config. Raises PrinterPalError on invalid data."""
    base = _default_config()
    merged = deepcopy(base)

    def merge(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
        for k, v in src.items():
            if k in dst and isinstance(dst[k], dict) and isinstance(v, dict):
                merge(dst[k], v)
            else:
                dst[k] = v

    merge(merged, cfg)

    # app
    merged["app"]["port"] = _as_int(merged["app"].get("port"), min_v=1, max_v=65535, name="app.port")
    merged["app"]["max_upload_mb"] = _as_int(
        merged["app"].get("max_upload_mb"), min_v=1, max_v=500, name="app.max_upload_mb"
    )
    if not isinstance(merged["app"].get("secret_key"), str) or len(merged["app"]["secret_key"]) < 16:
        raise PrinterPalError("app.secret_key must be a non-empty string")

    # printing
    merged["printing"]["preview_dpi"] = _as_int(
        merged["printing"].get("preview_dpi"), min_v=72, max_v=600, name="printing.preview_dpi"
    )
    merged["printing"]["print_dpi"] = _as_int(
        merged["printing"].get("print_dpi"), min_v=72, max_v=1200, name="printing.print_dpi"
    )
    merged["printing"]["max_pdf_pages_process"] = _as_int(
        merged["printing"].get("max_pdf_pages_process"), min_v=1, max_v=500, name="printing.max_pdf_pages_process"
    )
    merged["printing"]["default_copies"] = _as_int(
        merged["printing"].get("default_copies"), min_v=1, max_v=99, name="printing.default_copies"
    )
    merged["printing"]["bw_threshold"] = _as_int(
        merged["printing"].get("bw_threshold"), min_v=1, max_v=254, name="printing.bw_threshold"
    )

    mode = merged["printing"].get("default_mode")
    if mode not in {"raw", "grayscale", "bw", "dither", "outline"}:
        raise PrinterPalError("printing.default_mode must be one of raw|grayscale|bw|dither|outline")

    # ui
    for k in ("default_dark_mode", "default_eink_mode"):
        if not isinstance(merged["ui"].get(k), bool):
            raise PrinterPalError(f"ui.{k} must be boolean")

    # airprint
    if not isinstance(merged["airprint"].get("auto_enable"), bool):
        raise PrinterPalError("airprint.auto_enable must be boolean")

    # security
    if not isinstance(merged["security"].get("require_token"), bool):
        raise PrinterPalError("security.require_token must be boolean")
    if not isinstance(merged["security"].get("token"), str):
        raise PrinterPalError("security.token must be string")

    return merged


@dataclass
class ConfigStore:
    path: str = DEFAULT_CONFIG_PATH

    def load(self) -> Dict[str, Any]:
        if not os.path.exists(self.path):
            cfg = validate_config({})
            self.save(cfg)
            return cfg

        data = read_json(self.path)
        if not isinstance(data, dict):
            raise PrinterPalError("Config file must be a JSON object")
        return validate_config(data)

    def save(self, cfg: Dict[str, Any]) -> None:
        cfg2 = validate_config(cfg)
        atomic_write_json(self.path, cfg2)
