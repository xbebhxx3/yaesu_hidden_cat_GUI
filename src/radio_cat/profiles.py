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


def effective_ranges(ts: int, exp: bool, profile: dict | None = None,
                     mode_bits: int | None = None):
    power = profile.get("power", {}) if isinstance(profile, dict) else {}
    if mode_bits is not None:
        mode_tables = power.get("mode_tables", {})
        if isinstance(mode_tables, dict):
            table = mode_tables.get(str(mode_bits), mode_tables.get(mode_bits))
            if table:
                return table, f"mode=0x{int(mode_bits):02X}"
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
    power_cfg = item.get("power", {})
    if isinstance(power_cfg, dict) and power_cfg.get("ac_write") is not None:
        _validate_ac_write(power_cfg.get("ac_write"), power_cfg.get("levels", {}))


def _validate_ac_write(ac, levels) -> None:
    """Validate an optional power-limit (AC) write block without inventing values.

    items/targets 全空表示“未配置”（模板占位就是这个形态），直接放行；
    只要填了其中一项就必须成套：条目要有 label 与 FRAM 地址，目标值与条目等长。
    """
    if not isinstance(ac, dict):
        raise ValueError("power.ac_write must be an object")
    items = ac.get("items")
    targets = ac.get("targets")
    if not items and not targets:
        return
    if not isinstance(items, list) or not items:
        raise ValueError("power.ac_write.items must be a non-empty list")
    for entry in items:
        if not isinstance(entry, dict):
            raise ValueError("each power.ac_write item must be an object")
        if not str(entry.get("label", "")).strip():
            raise ValueError("power.ac_write item label is required")
        try:
            addr = int(str(entry.get("addr", "")), 16)
        except ValueError:
            raise ValueError("power.ac_write item addr must be a FRAM hex address")
        if not 0 <= addr <= 0xFFFF:
            raise ValueError("power.ac_write item addr must be within 0x0000-0xFFFF")
    if not isinstance(targets, dict) or not targets:
        raise ValueError("power.ac_write.targets must be a non-empty object")
    level_labels = {str(k) for k in levels} if isinstance(levels, dict) else set()
    for label, values in targets.items():
        if level_labels and str(label) not in level_labels:
            raise ValueError(f"power.ac_write target {label!r} has no matching power level")
        if not isinstance(values, list) or len(values) != len(items):
            raise ValueError("power.ac_write target list length must match items")
        for value in values:
            if not isinstance(value, int) or not 0 <= value <= 0xFF:
                raise ValueError(f"power.ac_write target {value!r} is out of the 0-255 range")


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
        validate_profile(item)   # 结构不合法的配置直接拒绝，不写盘
        pid = str(item.get("id", "")).strip()
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
            # CSV 模板的默认值 null 表示“未填”，按空值处理
            for key, value in list(row.items()):
                if str(value).strip().lower() == "null":
                    row[key] = ""
            try:
                row["n"] = int(row["code_dec"])
                row["item_addr"] = int(row["item_fram"], 16)
                row["apply_addr"] = int(row["apply_fram"], 16)
            except Exception:
                continue
            band_text = str(row.get("band_mhz", "")).strip()
            try:
                row["band_mhz_f"] = float(band_text) if band_text else None
            except ValueError:
                row["band_mhz_f"] = None
            # Empty labels are deliberately shown as missing evidence, never invented.
            if not str(row.get("label", "")).strip():
                row["label"] = "标签未提供"
            rows.append(row)
    return rows


