# -*- coding: utf-8 -*-
"""Application entry point and CLI."""
import argparse
import sys

from .core import *
from .file_ops import make_backup, restore_table, restore_fram, load_json, save_json
from .ui.main_window import launch_gui

def build_argparser():
    p = argparse.ArgumentParser(
        description="Yaesu Hedden CAT (GUI + CLI backup/restore)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-p", "--port", help="串口， /dev/ttyUSB0")
    p.add_argument("--model", help="profiles/ 中的机型配置 ID")
    p.add_argument("-b", "--baud", type=int, default=None)
    p.add_argument("--backup", metavar="JSON", help="无界面：完全备份到 JSON")
    p.add_argument("--with-spard", action="store_true",
                   help="备份时尝试 SPARD（通常需要服务模式；默认不采集）")
    p.add_argument("--no-mem", "--no-fram", dest="no_mem", action="store_true",
                   help="备份时跳过外部 FRAM")
    p.add_argument("--mem-start", "--fram-start", dest="mem_start", default=None,
                   help="外部 FRAM 起始地址（hex）")
    p.add_argument("--mem-end", "--fram-end", dest="mem_end", default=None,
                   help="外部 FRAM 结束地址（hex）；默认使用机型配置上限")
    p.add_argument("--restore", metavar="JSON", help="无界面：从 JSON 恢复(默认只恢复设置表)")
    p.add_argument("--restore-mem", "--restore-fram", dest="restore_fram",
                   action="store_true", help="恢复时同时写回外部 FRAM(极危险)")
    p.add_argument("--yes", action="store_true", help="跳过交互确认")
    return p


def cli_main(argv=None) -> int:
    args = build_argparser().parse_args(argv)
    if args.backup or args.restore:
        if not args.port:
            print("错误: 需要 -p/--port", file=sys.stderr)
            return 2
        if serial is None:
            print("错误: 未安装 pyserial", file=sys.stderr)
            return 2
        profiles = load_rig_profiles().get("profiles", [])
        if args.model:
            profile = next((p for p in profiles if p.get("id") == args.model), None)
        elif len(profiles) == 1:
            profile = profiles[0]
        else:
            profile = None
        if not profile:
            choices = ", ".join(p.get("id", "") for p in profiles) or "(no profiles)"
            print(f"错误: 请使用 --model 指定机型配置。可用: {choices}", file=sys.stderr)
            return 2
        baud = args.baud if args.baud is not None else get_rig_value(profile, "serial", "baud_default")
        if baud is None:
            print("错误: 当前机型配置没有默认波特率，请使用 -b 指定与电台 CAT RATE 一致的值", file=sys.stderr)
            return 2
        stopbits = get_rig_value(profile, "serial", "stop_bits")
        if stopbits not in (1, 2):
            print("错误: 当前机型配置没有停止位，请完善配置或选择其他配置", file=sys.stderr)
            return 2
        serial_cfg = profile.get("serial", {})
        data_bits = serial_cfg.get("data_bits")
        parity = serial_cfg.get("parity")
        flow_mode = serial_cfg.get("flow_control_mode")
        if data_bits not in (5, 6, 7, 8) or parity not in ("N", "E", "O", "M", "S") or flow_mode not in ("none", "rtscts", "xonxoff", "dsrdtr"):
            print("错误: 当前机型配置未完整定义数据位/校验/流控", file=sys.stderr)
            return 2
        link = SerialLink()
        link.open(
            args.port, int(baud), stopbits=int(stopbits),
            bytesize=int(data_bits), parity=str(parity),
            flow_control_mode=str(flow_mode))
        cat = HiddenCat(link, log=lambda m: print(m))
        cat.set_profile(profile)
        try:
            if args.backup:
                mem_start = (int(args.mem_start, 16) if args.mem_start is not None
                             else get_rig_value(profile, "fram", "address_min"))
                mem_end = (int(args.mem_end, 16) if args.mem_end is not None
                           else get_rig_value(profile, "fram", "address_max"))
                if not args.no_mem and (mem_start is None or mem_end is None):
                    raise ValueError("当前机型配置未提供完整 FRAM 地址范围；请使用 --no-mem 或完善配置")
                caps = profile.get("capabilities", {})
                opts = dict(
                    ve=bool(profile.get("ve_magic")),
                    fd=bool(caps.get("fd_read")),
                    kt=bool(caps.get("kt_read")),
                    pe=bool(caps.get("pe_calibration")),
                    table=bool(caps.get("settings_table")),
                    spard=bool(args.with_spard and caps.get("spa_rd")),
                    e0=bool(get_rig_value(profile, "dumps", "e0_length")),
                    e8=bool(get_rig_value(profile, "dumps", "e8_length")),
                    jp=bool(profile.get("service_code")),
                    mem=not args.no_mem, mem_start=mem_start, mem_end=mem_end,
                    scope="profile_supported_sections")
                if not any(opts[k] for k in ("ve", "fd", "kt", "pe", "table", "spard", "e0", "e8", "jp", "mem")):
                    raise ValueError("当前机型配置没有可备份的已声明功能")
                obj = make_backup(cat, opts,
                                  progress=lambda i, n, m, *a: print(f"  [{i+1}/{n}] {m}"))
                save_json(args.backup, obj, indent=1)
                print(f"备份完成: {args.backup}; complete={obj.get('complete', False)}; sections={obj.get('sections', [])}")
                if not obj.get("complete", False):
                    print("警告：备份有错误，不能视为完整备份。", file=sys.stderr)
            if args.restore:
                detected_id = cat.id_read().decode("ascii", "replace").strip()
                expected_id = str(profile.get("cat_id", "")).strip().upper()
                if expected_id and expected_id not in detected_id.upper():
                    raise ValueError(f"设备 ID {detected_id} 与当前配置 ID {expected_id} 不匹配，已阻止恢复")
                if not expected_id and not args.yes:
                    typed = input(f"当前配置没有已确认 CAT ID；实测为 {detected_id}。输入机型 ID {profile.get('id')} 确认继续: ")
                    if typed.strip() != str(profile.get("id")):
                        print("已取消"); return 1
                obj = load_json(args.restore)
                saved_model = obj.get("model", {}).get("id")
                if not saved_model or saved_model != profile.get("id"):
                    raise ValueError(f"备份机型 {saved_model or '未知'} 与当前机型 {profile.get('id')} 不匹配")
                tb = obj.get("data", {}).get("settings_table", {})
                has_table = isinstance(tb, dict) and "hex" in tb
                if has_table and get_rig_value(profile, "capabilities", "settings_table", False):
                    expected_len = get_rig_value(profile, "settings_table", "length")
                    if expected_len is None:
                        raise ValueError("当前机型配置未提供设置表长度")
                    try:
                        table = bytes.fromhex(tb["hex"])
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"备份中的设置表 HEX 无效: {exc}") from exc
                    if len(table) != int(expected_len):
                        raise ValueError(
                            f"设置表长度不匹配：期望 {expected_len}，实际 {len(table)}")
                    if not args.yes:
                        ans = input(f"将写回 {expected_len} 字节设置表，输入 yes 继续: ")
                        if ans.strip().lower() != "yes":
                            print("已取消"); return 1
                    n = restore_table(cat, table, verify=True,
                                      progress=lambda a, b: print(f"  写 {a}/{b}"))
                    print(f"设置表恢复完成并通过读回校验: {n} 字节")
                elif not args.restore_mem:
                    raise ValueError("备份中没有当前机型可恢复的设置表；如只恢复外部 FRAM，请使用 --restore-mem")
                if args.restore_mem:
                    if not get_rig_value(profile, "capabilities", "fram_raw_write", False):
                        raise ValueError("当前机型配置未启用 FRAM 写入")
                    mm = obj.get("data", {}).get("mem", {})
                    if not isinstance(mm, dict) or "hex" not in mm:
                        raise ValueError("备份中没有外部 FRAM 数据")
                    try:
                        mem = bytes.fromhex(mm["hex"])
                        mem_start = int(mm["start"], 16)
                    except (TypeError, ValueError, KeyError) as exc:
                        raise ValueError(f"备份中的 FRAM 数据无效: {exc}") from exc
                    if not args.yes:
                        ans = input("将写回外部 FRAM(极危险)，输入 yes 继续: ")
                        if ans.strip().lower() != "yes":
                            print("已取消"); return 1
                    restore_fram(cat, mem_start, mem,
                                 progress=lambda a, b: print(f"  写 {a}/{b}"))
                    print("外部 FRAM 恢复完成并通过读回校验")
        finally:
            link.close()
        return 0

    # 默认启动 GUI
    try:
        launch_gui()
    except Exception as e:
        print(f"无法启动 GUI: {e}", file=sys.stderr)
        print("可改用无界面模式：--backup / --restore", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(cli_main())
