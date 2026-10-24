#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""External model profile loading, validation and model-specific data files."""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PROFILE_DIR = APP_DIR / "profiles"
PROFILES_PATH = PROFILE_DIR


def get_rig_value(profile: dict | None, section: str, key: str, default=None):
    """Read an external profile value; missing/unknown model data never gets inferred."""
    if isinstance(profile, dict):
        value = profile.get(section, {})
        if isinstance(value, dict) and key in value and value[key] is not None:
            return value[key]
    return default


def effective_ranges(ts: int, exp: bool, profile: dict | None = None):
    power = profile.get("power", {}) if isinstance(profile, dict) else {}
    if exp:
        return power.get("exp_table", []), "EXP=1"
    tables = power.get("ts_tables", {})
    return tables.get(str(ts), tables.get(ts, [])), f"TS={ts}"


def pwr_bits_from_text(text: str, profile: dict | None = None) -> int:
    t = text.strip().upper()
    levels = get_rig_value(profile, "power", "levels", {})
    for label, bits in levels.items():
        if t == str(label).upper() or t.startswith(str(label).upper() + " (0X") or t == f"0X{int(bits):02X}":
            return int(bits)
    raise ValueError(f"Power level is not configured for this model: {text!r}")


def fmt_ranges(segments) -> str:
    """把 [(低, 高), ...] 格式化为 “低-高 / 低-高” 的 MHz 文本。"""
    return " / ".join(f"{lo:g}-{hi:g}" for lo, hi in segments)


def default_profiles() -> dict:
    """No embedded rig data: model profiles must come from external JSON files."""
    return {"format": "yaesu-hedden-cat-profiles", "version": 1, "profiles": []}


def validate_profile(item: dict) -> None:
    """Validate safe structural invariants without inventing missing model values."""
    pid = str(item.get("id", "")).strip()
    if not pid or not re.fullmatch(r"[A-Za-z0-9_-]+", pid):
        raise ValueError("profile id must contain only letters, digits, underscore or hyphen")
    if not str(item.get("name", "")).strip():
        raise ValueError("profile name is required")
    serial_cfg = item.get("serial", {})
    if not isinstance(serial_cfg, dict):
        raise ValueError("serial must be an object")
    stopbits = serial_cfg.get("stop_bits")
    if stopbits is not None and stopbits not in (1, 2):
        raise ValueError("serial.stop_bits must be 1, 2 or null")
    bits = serial_cfg.get("data_bits")
    if bits is not None and bits not in (5, 6, 7, 8):
        raise ValueError("serial.data_bits must be 5, 6, 7, 8 or null")
    parity = serial_cfg.get("parity")
    if parity is not None and str(parity).upper() not in ("N", "E", "O", "M", "S"):
        raise ValueError("serial.parity must be N/E/O/M/S or null")
    mode = serial_cfg.get("flow_control_mode")
    if mode is not None and mode not in ("none", "rtscts", "xonxoff", "dsrdtr"):
        raise ValueError("serial.flow_control_mode is invalid")
    baud = serial_cfg.get("baud_default")
    if baud is not None and (not isinstance(baud, int) or baud <= 0):
        raise ValueError("serial.baud_default must be a positive integer or null")
    fram = item.get("fram", {})
    if not isinstance(fram, dict):
        raise ValueError("fram must be an object")
    lo, hi = fram.get("address_min"), fram.get("address_max")
    if lo is not None and hi is not None and not (isinstance(lo, int) and isinstance(hi, int) and 0 <= lo <= hi <= 0xFFFF):
        raise ValueError("fram address range is invalid")
    fram_pair = fram.get("config_pair_addr")
    power_pair = item.get("power", {}).get("config_pair_addr") if isinstance(item.get("power"), dict) else None
    for label, value in (("fram.config_pair_addr", fram_pair),
                         ("power.config_pair_addr", power_pair)):
        if value is not None and (not isinstance(value, int) or not 0 <= value <= 0xFFFF):
            raise ValueError(f"{label} must be an integer 0..0xFFFF or null")
    if fram_pair is not None and power_pair is not None and fram_pair != power_pair:
        raise ValueError("power.config_pair_addr must match fram.config_pair_addr")
    for section, field in (("dumps", "e0_length"), ("dumps", "e8_length"),
                           ("settings_table", "length")):
        value = item.get(section, {}).get(field)
        if value is not None and (not isinstance(value, int) or value <= 0):
            raise ValueError(f"{section}.{field} must be a positive integer or null")
    for section in ("capabilities", "responses", "power", "settings_table", "dumps"):
        if section in item and not isinstance(item[section], dict):
            raise ValueError(f"{section} must be an object")


def load_rig_profiles() -> dict:
    """Load all independent model JSON files from profiles/*.json."""
    profiles = []
    if PROFILE_DIR.exists():
        for path in sorted(PROFILE_DIR.glob("*.json")):
            try:
                with path.open(encoding="utf-8") as fp:
                    item = json.load(fp)
                if not isinstance(item, dict):
                    raise ValueError("profile root must be an object")
                validate_profile(item)
                profiles.append(item)
            except (OSError, ValueError) as exc:
                print(f"Warning: model profile {path.name} could not be loaded: {exc}", file=sys.stderr)
    profiles.sort(key=lambda item: item.get("name", item.get("id", "")))
    return {"format": "yaesu-hedden-cat-profiles", "version": 1, "profiles": profiles}


def save_rig_profiles(obj: dict) -> None:
    """Save each profile as an independent JSON file; never overwrite other models."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    for item in obj.get("profiles", []):
        pid = str(item.get("id", "")).strip()
        if not pid or not re.fullmatch(r"[A-Za-z0-9_-]+", pid):
            raise ValueError(f"非法机型配置 id: {pid!r}")
        path = PROFILE_DIR / f"{pid}.json"
        with path.open("w", encoding="utf-8") as fp:
            json.dump(item, fp, ensure_ascii=False, indent=2)
            fp.write("\n")

def _profile_data_path(profile: dict | None, key: str):
    rel = get_rig_value(profile, "data_files", key)
    if not rel:
        return None
    path = (APP_DIR / rel).resolve()
    try:
        path.relative_to(APP_DIR.resolve())
    except ValueError:
        return None
    return path


def load_fram_regions(profile: dict | None = None) -> list[dict]:
    """Load optional model-specific FRAM labels from the selected profile."""
    path = _profile_data_path(profile, "fram_map")
    if not path or not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as fp:
        for row in csv.DictReader(fp):
            try:
                row["start_i"] = int(row["start"], 16)
                row["end_i"] = int(row["end"], 16)
                row["size_i"] = int(row["size"])
            except Exception:
                continue
            rows.append(row)
    return rows


def load_service_items(profile: dict | None = None) -> list[dict]:
    """Load optional model-specific service menu mapping."""
    path = _profile_data_path(profile, "service_items")
    if not path or not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as fp:
        for row in csv.DictReader(fp):
            try:
                row["n"] = int(row["code_dec"])
                row["item_addr"] = int(row["item_fram"], 16)
                row["apply_addr"] = int(row["apply_fram"], 16)
                band_text = str(row.get("band_mhz", "")).strip()
                row["band_mhz_f"] = float(band_text) if band_text else None
                # Empty labels are deliberately shown as missing evidence, never invented.
                if not str(row.get("label", "")).strip():
                    row["label"] = "标签未提供（未猜测）"
            except Exception:
                continue
            rows.append(row)
    return rows


