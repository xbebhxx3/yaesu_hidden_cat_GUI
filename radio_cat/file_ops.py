#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backup/restore and filesystem-oriented operations, separate from UI and CAT transport."""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from .core import (
    BACKUP_FORMAT, BACKUP_VERSION, CatError, HiddenCat,
    get_rig_value,
)



def load_json(path) -> dict:
    """Read a UTF-8 JSON file with a context-managed file handle."""
    with open(path, "r", encoding="utf-8") as fp:
        value = json.load(fp)
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def save_json(path, value: dict, *, indent: int = 2) -> None:
    """Atomically write a UTF-8 JSON object."""
    target = Path(path)
    temp = target.with_name(target.name + ".tmp")
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as fp:
            json.dump(value, fp, ensure_ascii=False, indent=indent)
            fp.write("\n")
        os.replace(temp, target)
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def load_bytes(path) -> bytes:
    with open(path, "rb") as fp:
        return fp.read()


def save_bytes(path, value: bytes) -> None:
    target = Path(path)
    temp = target.with_name(target.name + ".tmp")
    try:
        with open(temp, "wb") as fp:
            fp.write(value)
        os.replace(temp, target)
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except Exception:
            pass
        raise

def _iso_now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def make_backup(cat: HiddenCat, opts: dict, progress=None, cancel=None) -> dict:
    """按 opts 采集数据并返回备份字典。opts 键见 GUI。"""
    data = {}
    steps = []
    if opts.get("ve"):
        steps.append("ve")
    if opts.get("fd"):
        steps.append("fd")
    if opts.get("kt"):
        steps.append("kt")
    if opts.get("pe"):
        steps.append("pe")
    if opts.get("table"):
        steps.append("table")
    if opts.get("e0"):
        steps.append("e0")
    if opts.get("e8"):
        steps.append("e8")
    if opts.get("jp"):
        steps.append("jp")
    if opts.get("mem"):
        steps.append("mem")
    if opts.get("spard"):
        steps.append("spard")

    total = len(steps) or 1
    if not steps:
        data["backup"] = {"error": "No backup sections selected"}
    for si, s in enumerate(steps):
        if cancel is not None and cancel.is_set():
            raise CatError("已取消")
        if progress:
            progress(si, total, f"采集 {s} …")

        if s == "ve":
            try:
                r = cat.ve()
                data["ve"] = {"raw_hex": r.hex(),
                              "text": r.decode("latin-1")}
            except CatError as e:
                data["ve"] = {"error": str(e)}

        elif s == "fd":
            fd = {}
            for x in (0, 1, 2):
                try:
                    r = cat.fd_read(x)
                    fd[str(x)] = {"raw_hex": r.hex()}
                except CatError as e:
                    fd[str(x)] = {"error": str(e)}
            data["fd"] = fd

        elif s == "kt":
            try:
                data["kt"] = {"raw_hex": cat.kt_read().hex()}
            except CatError as e:
                data["kt"] = {"error": str(e)}

        elif s == "pe":
            pe = {}
            positions = get_rig_value(cat.profile, "pe_calibration", "selector_positions", None)
            if positions:
                ranges = [[int(v) for v in pos] for pos in positions]
            else:
                selectors = [int(v) for v in get_rig_value(cat.profile, "pe_calibration", "selectors", [])]
                ranges = [selectors, selectors, selectors]
            for s0 in ranges[0]:
                for s1 in ranges[1]:
                    for m in ranges[2]:
                        key = f"{s0}{s1}{m}"
                        try:
                            r = cat.pe_read(int(s0), int(s1), int(m))
                            pe[key] = {"raw_hex": r.hex()} if r else {"ok": False}
                        except CatError as e:
                            pe[key] = {"error": str(e)}
            data["pe"] = pe

        elif s == "table":
            try:
                tb = cat.read_settings_table(
                    progress=(lambda a, b: progress(si, total, f"设置表 {a}/{b}", a, b)
                              if progress else None),
                    cancel=cancel)
                data["settings_table"] = {
                    "base": (f"0x{get_rig_value(cat.profile, 'settings_table', 'base_addr'):04X}"
                             if get_rig_value(cat.profile, "settings_table", "base_addr") is not None else None),
                    "start_addr": (f"0x{get_rig_value(cat.profile, 'settings_table', 'start_addr'):04X}"
                                   if get_rig_value(cat.profile, "settings_table", "start_addr") is not None else None),
                    "end_addr": (f"0x{get_rig_value(cat.profile, 'settings_table', 'end_addr'):04X}"
                                 if get_rig_value(cat.profile, "settings_table", "end_addr") is not None else None),
                    "bytes": len(tb),
                    "hex": tb.hex(),
                }
            except CatError as e:
                data["settings_table"] = {"error": str(e)}

        elif s == "spard":
            try:
                r = cat.spa_rd()
                data["spard"] = {"raw_hex": r.hex(), "len": len(r)}
            except CatError as e:
                data["spard"] = {"error": str(e)}

        elif s == "e0":
            try:
                r = cat.e0()
                data["e0"] = {"len": len(r), "hex": r.hex()}
            except CatError as e:
                data["e0"] = {"error": str(e)}

        elif s == "e8":
            try:
                r = cat.e8()
                data["e8"] = {"len": len(r), "hex": r.hex()}
            except CatError as e:
                data["e8"] = {"error": str(e)}

        elif s == "jp":
            try:
                data["jp"] = {"raw_hex": cat.jp_read().hex()}
            except CatError as e:
                data["jp"] = {"error": str(e)}

        elif s == "mem":
            start = opts.get("mem_start", get_rig_value(cat.profile, "fram", "address_min", 0))
            end = opts.get("mem_end", get_rig_value(cat.profile, "fram", "address_max"))
            try:
                mb = cat.read_fram(
                    start, end, cancel=cancel, strict=True,
                    progress=(lambda a, b: progress(si, total, f"外部 FRAM {a}/{b}", a, b)
                              if progress else None))
                data["mem"] = {
                    "space": "external_fram",
                    "start": f"0x{start:04X}", "end": f"0x{end:04X}",
                    "bytes": len(mb), "hex": mb.hex(),
                }
            except Exception as e:
                data["mem"] = {"error": str(e)}

    errors = []
    def collect_errors(prefix, value):
        if isinstance(value, dict):
            if "error" in value:
                errors.append(f"{prefix}: {value['error']}")
            elif value.get("ok") is False:
                errors.append(f"{prefix}: device reported unsupported/negative response")
            else:
                for key, child in value.items():
                    collect_errors(f"{prefix}.{key}" if prefix else str(key), child)
    collect_errors("", data)
    return {
        "format": BACKUP_FORMAT,
        "format_version": BACKUP_VERSION,
        "created": _iso_now(),
        "model": {
            "id": cat.profile.get("id"),
            "name": cat.profile.get("name"),
            "profile_status": cat.profile.get("status"),
            "firmware": (data.get("ve", {}) or {}).get("text")
                        if isinstance(data.get("ve"), dict) else None,
        },
        "complete": not errors,
        "scope": opts.get("scope", "selected_sections"),
        "sections": list(steps),
        "errors": errors,
        "transport": {"port": cat.link.port, "baud": cat.link.baud},
        "data": data,
    }


def restore_table(cat: HiddenCat, table: bytes, progress=None, cancel=None,
                  verify=True) -> int:
    """按当前机型配置长度逐字节写回设置表(SPw)，返回写入字节数。"""
    start = get_rig_value(cat.profile, "settings_table", "start_addr")
    expected_len = get_rig_value(cat.profile, "settings_table", "length")
    if start is None or expected_len is None:
        raise CatError("当前机型未配置可恢复的设置表")
    if len(table) != int(expected_len):
        raise ValueError(f"设置表长度必须为 {expected_len} 字节，收到 {len(table)}")
    start = int(start)
    total = len(table)
    for i, addr in enumerate(range(start, start + total)):
        if cancel is not None and cancel.is_set():
            raise CatError("已取消")
        cat.spw_table(addr, table[i])
        if progress and (i % 8 == 0 or i == total - 1):
            progress(i + 1, total)
    if verify:
        back = bytearray()
        for addr in range(start, start + total):
            back.append(cat.spr_table(addr)[6])
        if bytes(back) != table:
            bad = [i for i in range(total) if back[i] != table[i]]
            raise CatError(f"写回校验失败，{len(bad)} 处不一致，例如偏移 {bad[:8]}")
    return total


def restore_fram(cat: HiddenCat, start: int, mem: bytes, progress=None, cancel=None,
                 verify: bool = True) -> int:
    """Write external FRAM and, by default, read it back for byte-for-byte verification."""
    if not mem:
        raise ValueError("FRAM 恢复数据不能为空")
    max_addr = int(get_rig_value(cat.profile, "fram", "address_max", -1))
    min_addr = int(get_rig_value(cat.profile, "fram", "address_min", 0))
    if not (min_addr <= start <= max_addr and start + len(mem) - 1 <= max_addr):
        raise ValueError("恢复范围超出当前机型 FRAM 地址范围")
    if len(mem) % 2 and start + len(mem) - 1 == max_addr:
        raise ValueError("奇数长度到达 FRAM 上限：无法安全成对恢复")
    total = (len(mem) + 1) // 2
    for k in range(total):
        if cancel is not None and cancel.is_set():
            raise CatError("已取消")
        i = k * 2
        d0 = mem[i]
        # 奇数长度时保留 addr+1 原值，避免把相邻字节写成 0xFF。
        d1 = mem[i + 1] if i + 1 < len(mem) else cat.spr(start + i + 1)[6]
        cat.spw(start + i, d0, d1)
        if progress and (k % 32 == 0 or k == total - 1):
            progress(k + 1, total)
    if verify:
        back = cat.read_fram(start, start + len(mem) - 1, cancel=cancel, strict=True)
        if back != mem:
            bad = [i for i, (expected, actual) in enumerate(zip(mem, back)) if expected != actual]
            raise CatError(f"FRAM 写回校验失败，{len(bad)} 处不一致，例如偏移 {bad[:8]}")
    return len(mem)


