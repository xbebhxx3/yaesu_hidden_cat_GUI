#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Yaesu Hedden CAT

A profile-driven serial CAT utility. Model-specific identities, response lengths,
memory maps, bit fields, capability flags, service tables and power tables are
loaded from external JSON/data files. The executable contains only shared
transport, framing and user-interface logic.

Requires Python 3 and pyserial. Tkinter is used for the desktop UI.
All write operations can affect radio configuration or calibration. Use only
profile entries supported by evidence for the connected model, and back up
before writing. Static firmware analysis is not equivalent to real-device
validation.
"""
from __future__ import annotations

import re
import threading
import time

try:
    import serial
    from serial.tools import list_ports
except Exception:  # pyserial 未安装
    serial = None
    list_ports = None

# ------------------------------------------------------------------ 通用应用常量
APP_TITLE = "Yaesu Hedden CAT"
BACKUP_FORMAT = "yaesu-full-backup"
BACKUP_VERSION = 1
MANUAL_PROFILE_NAME = "自定义（手动输入）"

from .profiles import (
    APP_DIR, PROFILE_DIR, PROFILES_PATH, get_rig_value, effective_ranges,
    pwr_bits_from_text, fmt_ranges, default_profiles, validate_profile,
    load_rig_profiles, save_rig_profiles, load_fram_regions, load_service_items,
)


def parse_hex_dump(text: str) -> list[tuple[int, bytes]]:
    """解析“AAAA: 00 11 22 ...”十六进制编辑框，返回 [(地址, 数据), ...]。

    允许行尾带 ASCII 注释；没有地址前缀的行会被拒绝，避免写错位置。
    """
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "//")):
            continue
        m = re.match(r"^(?:0x)?([0-9A-Fa-f]{1,4})\s*:\s*(.*)$", line)
        if not m:
            raise ValueError(f"无法解析行（需要 AAAA: 00 11 …）: {line}")
        addr = int(m.group(1), 16)
        body = m.group(2)
        # 截掉常见的行尾 ASCII 区（两个以上空格后接可打印文本）
        body = re.split(r"\s{2,}", body, maxsplit=1)[0]
        try:
            data = bytes.fromhex(body.replace(" ", ""))
        except ValueError as exc:
            raise ValueError(f"十六进制解析失败: {line}") from exc
        if not data:
            continue
        out.append((addr, data))
    return out

# ------------------------------------------------------------------ 异常
class CatError(Exception):
    pass

class CatNotOpen(CatError):
    pass

class CatTimeout(CatError):
    pass

# ------------------------------------------------------------------ 帧构造
def sp_frame(body: bytes, allow_terminator: bool = True) -> bytes:
    """构造 SP 二进制帧：
    默认允许 0x3B；需要严格排查时可显式传
    allow_terminator=False。
    """
    head = b"SP" + bytes(body)
    chk = sum(head) & 0xFF
    frame = head + bytes([chk]) + b";"
    if not allow_terminator and 0x3B in frame[:-1]:
        raise CatError("SP 帧参数或校验包含 0x3B (';')，已拒绝发送；请换地址/值")
    return frame


def hexs(b: bytes, sep: str = " ") -> str:
    return sep.join(f"{x:02X}" for x in b)


# ------------------------------------------------------- 按钮悬浮说明
# 键为按钮文字（唯一者）。重复文字（如多处“发送”）改为在创建处单独绑定。
BUTTON_TIPS = {
    # 全局
    "取消任务": "中断正在运行的长任务（范围导出 / 备份 / 恢复）。",
    # 连接
    "刷新": "重新枚举本机串口设备并刷新下拉列表。",
    "打开": "按所选端口与波特率打开串口；打开后按钮变为“关闭”，再按即断开。",
    "关闭": "断开当前串口连接。",
    # 快捷命令
    "VE 版本查询 (魔术串)": "发送 VE + 当前机型配置中的魔术串；回包长度由配置定义。只读。",
    "读取设备版本": "发送 VE + 当前机型配置中的魔术串；回包长度由配置定义。只读。",
    "ID 查询": "发送 ID; 读取机型 ID；回包长度与识别规则依当前机型配置。只读。",
    "读取设备识别信息": "发送 ID; 核对实机 CAT ID 是否与预设一致。只读。",
    "FD 读": "发送 FD00<x>; 读取通道 x 的 32 位频率/数据寄存器（回包长度依当前机型配置）。只读。",
    "读取维护数据": "发送 FD00<x>; 读取通道 x 的维护数据（回包长度依当前机型配置）。只读。",
    "FD 写 (8 hex)": "发送 FD0020<8hex>; 写入 32 位值。写操作。",
    "写入维护数据（高级）": "发送 FD0020<8hex>; 写入维护数据。写操作，需确认。",
    "KT 读余量": "发送 KT; 读取 CW 文本（键控存储器）缓冲状态（回包长度依当前机型配置）。只读。",
    "读取 CW 文本容量": "发送 KT; 读取 CW 文本（键控存储器）缓冲状态。只读。",
    "KT 写入文本": "先读 KT 剩余空间，再发送 KT1<文本>; 写入 CW 文本（固件允许 ASCII 0x20–0x5F 或 0x7D，字母转大写，≤30 字符）。写操作。",
    "写入 CW 文本": "先读 KT 剩余空间，再发送 KT1<文本>; 写入 CW 文本。写操作。",
    "KT 清空 (KT0)": "发送 KT0; 清空 CW 文本缓冲。写操作。",
    "清空 CW 文本": "发送 KT0; 清空 CW 文本缓冲。写操作。",
    "EK 触发": "发送 EK; 无参触发。通常无回包。",
    "EO 微调 (a,sign,b,ddd)": "发送 EO<a><sign><b><ddd>;（如 0+0000）做 32 位偏移/微调。写操作。",
    "高级频率偏移微调": "发送 EO<a><sign><b><ddd>;（如 0+0000）做 32 位偏移/微调。写操作。",
    "JP 读 (当前前缀)": "发送当前 JP 前缀（请检查当前机型配置），读取当前机型配置长度的启动/服务参数。只读。",
    "JP 写 (互补 8 hex)": "发送 JP 前缀+8hex；写入配置对/参数，前后两字须互补（v^w==0xFFFF）。写操作，风险较高。",
    "E0 读取": "发送 E0; 读取当前配置声明长度的诊断数据。只读。",
    "E8 读取": "发送 E8; 读取当前配置声明长度的转储。只读。",
    "SPARD 读取": "SP 帧 ARD：读取当前配置声明的设置表；通常需要先进入服务模式。只读。",
    "SPM TR": "SP 帧 MTR：读取仪表/温度类测量值（14 字节）。只读。",
    "SPM T2": "SP 帧 MT2：读取另一组测量值（回包长度依当前机型配置）。只读。",
    "SPN 读取当前项": "SP 帧 N FF FF：读取当前服务菜单项目编号。只读。",
    "SPAWE 写使能": "SP 帧 AWE：设置表写使能。写使能。",
    "SPACL 清除": "SP 帧 ACL：清除设置表。破坏性。",
    "SPAET": "SP 帧 AET：设置表相关操作，成功回 A; 否则 ?;。",
    "ZZ 服务模式 (会复位)": "发送 ZZ + 当前配置前缀 + 操作值；具体行为取决于机型，可能立即复位。",
    # FRAM / 设置表
    "读 2 字节": "用 SPR 读取当前机型配置地址范围内的 2 字节。只读。",
    "写 2 字节": "用 SPW 写 2 字节到外部 FRAM 地址（有确认框）。写操作。",
    "读 1 字节": "用 SPr 读取当前配置定义的设置表地址。只读。",
    "写 1 字节": "用 SPw 写 1 字节到设置表地址（有确认框）。写操作，影响校准。",
    "读内部状态": "用 SPr 特殊地址读取内部状态。只读。",
    "导出到文件": "用 SPR 逐 2 字节把 [起,止] 范围导出为 .bin。只读。",
    "我已备份相关 FRAM 区域": "高风险写入前的确认；请先保存并检查当前机型的 FRAM 备份。",
    # 机型 / 功率 / 服务菜单 / PE / FRAM 全表
    "应用魔术串": "把本页 VE / JP / ZZ 魔术串应用到当前串口会话。",
    "重新载入 JSON": "重新读取 cat/rig_profiles.json。",
    "保存当前配置到 JSON": "把本页机型与 VE/JP/ZZ 魔术串写回 rig_profiles.json；保留其它预设。",
    "手动输入 / 新建": "清空编辑框，手动输入任意机型魔术串；保存时新建自定义预设，不覆盖已有项。",
    "读取 ID": "发送 ID; 核对实机 CAT ID 是否与预设一致。",
    "读取 VE 版本": "发送 VE+魔术串，读取当前机型配置长度的版本串。",
    "JP 读取": "发送当前 JP 前缀（请检查当前机型配置），读取当前机型配置长度并解析互补字。",
    "JP 写入": "发送 JP 前缀+8hex；两个 16 位字必须互补；写入会改配置对/参数，风险高。",
    "JP 写入（未开放）": "当前机型存在未验证副作用或未确认安全写入值，写入保持禁用。",
    "JP 写入（不支持）": "当前机型配置未声明 JP 写入能力。",
    "JP 写入（未验证，禁用）": "当前机型 JP 写入存在未验证副作用或未做真机验证，写入保持禁用。",
    "读取配置": "读取当前配置定义的配置字节并按其位掩码解析。",
    "预览功率等级": "只计算功率等级位目标值，不写设备。",
    "写入功率等级": "写入功率等级位（保留其他位并回读）；当前机型配置了功率上限（AC）时，再用标准 CAT EX 写入各上限项并逐项回读。",
    "预览 TS/EXP": "只计算 TS/EXP 位目标值，不写设备，会显示参考频率范围。",
    "写入 TS/EXP": "按当前配置定义的 TS/EXP 位写入。",
    "进入服务模式 / 临时宽频（会复位）": "发送当前机型配置定义的服务模式命令；可能立即复位设备。",
    "读取区间": "按起止地址用 SPR 读取外部 FRAM 并显示为十六进制。",
    "写入编辑框": "解析编辑框中的 AAAA: ... 行并逐段写回 FRAM。",
    "读取全部 FRAM": "读取当前机型配置定义的全部外部 FRAM，耗时较长。",
    "备份完整 FRAM": "读取并备份当前机型配置声明的完整 FRAM 范围。",
    "导出区间 .bin": "把编辑框数据解析后保存为 .bin，不访问设备。",
    "导入 .bin 到编辑框": "把 .bin 文件显示到编辑框，可用“写入编辑框”写回。",
    "读取整表 (SPr 配置长度)": "用 SPr 按当前机型配置逐字节读取设置表。只读。",
    "读取设置表(配置长度)": "用 SPr 按当前机型配置逐字节读取设置表。只读。",
    "读取服务菜单索引/子码": "只读服务菜单项目号与子码（地址依机型配置）。",
    "服务模式入口（可能重启）": "发送当前机型配置定义的服务模式命令；可能立即复位设备。",
    "SPARD 整表": "用 SPARD 按当前机型配置读取整表；通常需要先进入服务模式。",
    "读取选中项": "读取当前服务菜单映射指定项目的校准字节。",
    "写入选中项校准值": "用 SPw 按当前机型映射写入项目值；可选写后 SPAET。",
    "SPN 选择/应用 N": "发送 SPN N，选择/应用服务项目并切换当前波段/模式（需服务模式）。",
    "SPAET 应用特殊项": "发送 SPAET；只有特殊表头/r3l=1/2 项会成功，普通项回 ?;。",
    "进入服务模式": "发送 ZZ<ZZ前缀>000; 进入服务模式；会立即软复位。",
    "读取选中": "读取当前 PE 选择子的原始回包。",
    "写入选中": "按 PE<选择子><符号><2hex> 写 PE；会改校准。",
    "读取 N/S (0344/0346)": "只读 服务菜单项目号与子码（地址依机型配置）。",
    "SPN 读当前 N": "发送 SPN FF FF，读取当前服务菜单项目编号；可能需要服务模式。",
    "SPN 写": "快捷发送 SPN N；会切换波段/模式，需要服务模式。",
    "SPSDR 读": "发送 SPSDR，读取 9 字节 SDR 数据帧。",
    # 校准 / PE
    "读取设置表": "用 SPr 按当前机型配置读取设置表。只读。",
    "写入设置表(危险)": "按当前机型配置的设置表长度逐字节用 SPw 写回；可回读校验。破坏性写操作。",
    "从备份文件载入…": "从 JSON 备份或裸 .bin 载入设置表到文本区。",
    "保存为 .bin…": "把当前设置表保存为裸 .bin；长度依当前机型配置。",
    "写回后校验": "写回设置表后自动回读逐字节比对，不一致则报错。",
    "显示十六进制": "开启时按“地址: 16 字节 hex”分行显示。",
    "读取全部 PE": "枚举当前机型配置的 PE 选择子逐个读取并列出回包。只读。",
    # 备份
    "① 备份所选项目 → JSON": "按勾选项采集数据并写入 JSON 备份文件。",
    "① 开始完全备份 → JSON": "按勾选项采集数据并写入 JSON 备份文件。",
    "② 载入备份并预览": "打开 JSON 备份、校验并预览；含设置表时自动载入校准页。",
    "③ 恢复设置表(危险)": "从已载入备份取设置表逐字节 SPw 写回并校验。破坏性。",
    "④ 恢复外部 FRAM(极危险)": "从已载入备份取外部 FRAM 数据逐 2 字节 SPW 写回（两次确认）。可能导致无法开机。",
    # 终端
    "构造→原始框": "按 body 计算校验并补 ; 生成完整 SP 帧，填入“原始字节”框。",
    "纯读": "不发送数据，仅读取指定字节数（观察设备自发帧）。",
    "清空记录": "清空终端页显示记录。",
}

BACKUP_ITEM_TIPS = {
    "ve": "采集版本 / ID 串。",
    "fd": "采集 FD000/001/002 三个通道。",
    "kt": "采集 CW 文本缓冲状态；部分机型（如 FTDX10）不支持。",
    "pe": "采集当前机型配置的 PE 读数（如已启用）。",
    "table": "用 SPr 采集当前机型配置的设置表（如该机型已启用）。",
    "spard": "采集 SPARD 整表回包；通常需要服务模式，默认不采集。" ,
    "e0": "采集当前配置定义长度的诊断帧。",
    "e8": "采集当前配置定义长度的转储。",
    "jp": "采集当前机型配置长度的 JP 参数。",
    "mem": "用 SPR 逐 2 字节采集外部 FRAM 地址空间（耗时，可缩小范围）。",
}


class ToolTip:
    """轻量级悬浮提示。"""

    def __init__(self, widget, text, delay=450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tip = None
        self._id = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        try:
            self._id = self.widget.after(self.delay, self._show)
        except Exception:
            self._id = None

    def _cancel(self):
        if self._id is not None:
            try:
                self.widget.after_cancel(self._id)
            except Exception:
                pass
            self._id = None

    def _show(self):
        if self.tip is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 18
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
            tw = __import__("tkinter").Toplevel(self.widget)
        except Exception:
            return
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        lbl = __import__("tkinter").Label(
            tw, text=self.text, justify="left", background="#ffffe0",
            relief="solid", borderwidth=1, font=("", 9))
        lbl.pack()
        self.tip = tw

    def _hide(self, _event=None):
        self._cancel()
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


class HoverToolTip:
    """按鼠标位置动态生成文本的悬浮提示（用于 FRAM 区域/字节）。"""

    def __init__(self, widget, text_fn, delay=420, wraplength=640):
        self.widget = widget
        self.text_fn = text_fn
        self.delay = delay
        self.wraplength = wraplength
        self.tip = None
        self.label = None
        self._id = None
        self._current = None
        self._x = 0
        self._y = 0
        widget.bind("<Motion>", self._motion, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<MouseWheel>", self._hide, add="+")

    def _motion(self, event):
        self._x, self._y = event.x_root, event.y_root
        try:
            text = self.text_fn(event)
        except Exception:
            text = None
        if not text:
            self._hide()
            return
        if text != self._current:
            self._current = text
            self._cancel()
            try:
                self._id = self.widget.after(self.delay, self._show)
            except Exception:
                self._id = None
        if self.tip is not None:
            self._place()
            if self.label is not None:
                try:
                    self.label.configure(text=text)
                except Exception:
                    pass

    def _cancel(self):
        if self._id is not None:
            try:
                self.widget.after_cancel(self._id)
            except Exception:
                pass
            self._id = None

    def _place(self):
        if self.tip is None:
            return
        try:
            self.tip.wm_geometry(f"+{self._x + 16}+{self._y + 14}")
        except Exception:
            pass

    def _show(self):
        if self.tip is not None or not self._current:
            return
        try:
            tk = __import__("tkinter")
            tw = tk.Toplevel(self.widget)
            tw.wm_overrideredirect(True)
            lbl = tk.Label(
                tw, text=self._current, justify="left", background="#ffffe0",
                relief="solid", borderwidth=1, wraplength=self.wraplength,
                font=("", 9))
            lbl.pack()
            self.tip = tw
            self.label = lbl
            self._place()
        except Exception:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None
            self.label = None

    def _hide(self, _event=None):
        self._cancel()
        self._current = None
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None
            self.label = None


# ============================================================ 串口链路
class SerialLink:
    def __init__(self):
        self.ser = None
        self.lock = threading.RLock()
        self.port = None
        self.baud = None
        self.stopbits = None
        self.bytesize = None
        self.parity = None
        self.flow_control_mode = None

    @property
    def is_open(self) -> bool:
        return self.ser is not None and getattr(self.ser, "is_open", False)

    def open(self, port: str, baud: int, stopbits: int, *,
             bytesize: int, parity: str, flow_control_mode: str):
        if serial is None:
            raise CatError("未安装 pyserial，请先 `pip install pyserial`")
        if stopbits not in (1, 2):
            raise ValueError("停止位必须是 1 或 2")
        if bytesize not in (5, 6, 7, 8):
            raise ValueError("数据位必须是 5、6、7 或 8")
        parity = str(parity).upper()
        if parity not in ("N", "E", "O", "M", "S"):
            raise ValueError("校验位必须为 N/E/O/M/S")
        if flow_control_mode is None:
            raise ValueError("流控模式必须由机型配置明确指定")
        mode = str(flow_control_mode).lower()
        if mode not in ("none", "rtscts", "xonxoff", "dsrdtr"):
            raise ValueError("不支持的流控模式")
        self.close()
        self.ser = serial.Serial(
            port=port, baudrate=baud, bytesize=bytesize, parity=parity,
            stopbits=stopbits, timeout=0.05,
            rtscts=(mode == "rtscts"), xonxoff=(mode == "xonxoff"),
            dsrdtr=(mode == "dsrdtr"))
        self.port, self.baud, self.stopbits = port, baud, stopbits
        self.bytesize, self.parity, self.flow_control_mode = bytesize, parity, mode

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    def write(self, data: bytes):
        if not self.is_open:
            raise CatNotOpen("串口未打开")
        with self.lock:
            self.ser.reset_input_buffer()
            self.ser.write(data)
            self.ser.flush()

    def _read_some(self, n: int) -> bytes:
        try:
            return self.ser.read(n)
        except Exception:
            return b""

    def read_exact(self, n: int, timeout: float) -> bytes:
        if not self.is_open:
            raise CatNotOpen("串口未打开")
        end = time.time() + timeout
        buf = bytearray()
        while len(buf) < n and time.time() < end:
            c = self._read_some(n - len(buf))
            if c:
                buf += c
        if len(buf) < n:
            raise CatTimeout(f"期望 {n} 字节，仅收到 {len(buf)}；已收: {hexs(bytes(buf))}")
        return bytes(buf)

    def read_until(self, term: bytes, timeout: float) -> bytes:
        end = time.time() + timeout
        buf = bytearray()
        while time.time() < end:
            c = self._read_some(1)
            if c:
                buf += c
                if c == term:
                    break
        return bytes(buf)

    def drain(self, seconds: float) -> bytes:
        end = time.time() + seconds
        buf = bytearray()
        while time.time() < end:
            c = self._read_some(256)
            if c:
                buf += c
        return bytes(buf)


# ============================================================ 协议封装
class HiddenCat:
    def __init__(self, link: SerialLink, log=None):
        self.link = link
        self.log = log or (lambda *a, **k: None)
        self.profile = {}
        self.ve_magic = ""
        self.jp_prefix = ""
        self.zz_prefix = ""

    def set_profile(self, profile: dict | None) -> None:
        """Apply an external model profile; no model defaults are embedded."""
        self.profile = dict(profile or {})
        self.ve_magic = str(self.profile.get("ve_magic", "")).strip().upper()
        # One externally configured model/service code is shared by JP and ZZ.
        # Command names/prefixes are generic protocol syntax, not per-model data.
        code = str(self.profile.get("service_code", "")).strip().upper()
        if code and (len(code) != 4 or any(c not in "0123456789ABCDEF" for c in code)):
            raise ValueError("service_code 必须是 4 位十六进制字符")
        self.jp_prefix = "JP" + code if code else ""
        self.zz_prefix = "PE" + code if code else ""

    def set_magic(self, ve_magic: str | None = None,
                  jp_prefix: str | None = None,
                  zz_prefix: str | None = None) -> None:
        """设置 VE / JP / ZZ 魔术串；长度/字符不合法时抛 ValueError。"""
        if ve_magic is not None:
            s = ve_magic.strip().upper()
            # 允许用户直接粘贴完整 VE 命令。
            if len(s) == 9 and s.startswith("VE"):
                s = s[2:]
            if len(s) != 7 or not s.isascii() or ";" in s:
                raise ValueError("VE 魔术串必须是 7 个 ASCII 字符（可粘贴完整 VE 命令）")
            self.ve_magic = s
        if jp_prefix is not None:
            p = jp_prefix.strip().upper()
            if len(p) != 6 or not p.isascii() or ";" in p:
                raise ValueError("JP 前缀必须是 6 个 ASCII 字符（请检查当前机型配置）")
            self.jp_prefix = p
        if zz_prefix is not None:
            z = zz_prefix.strip().upper()
            # 允许直接粘贴完整 ZZ 命令，内部只保存前缀。
            if len(z) == 8 and z.startswith("ZZ"):
                z = z[2:]
            if len(z) != 6 or not z.isascii() or ";" in z:
                raise ValueError("ZZ 前缀必须是 6 个 ASCII 字符（可粘贴完整 ZZ 命令）")
            self.zz_prefix = z

    # ---- 底层 ----
    def _t_for(self, nbytes: int, base: float = 0.6) -> float:
        baud = self.link.baud or 1
        return base + nbytes * 10.0 / baud

    def _ascii(self, cmd: str, expect=None, timeout=None):
        """expect=None→读到 ';'；expect=-1→读固定窗口；expect=int→读恰好 n 字节。"""
        self.link.write(cmd.encode("ascii") + b";")
        if expect == -1:
            return self.link.drain(timeout or 0.25)
        if expect is None:
            return self.link.read_until(b";", timeout or self._t_for(32, 0.5))
        return self.link.read_exact(expect, timeout or self._t_for(expect, 0.6))

    def _sp(self, body: bytes, expect, timeout=None, allow_terminator=False) -> bytes:
        self.link.write(sp_frame(body, allow_terminator=allow_terminator))
        if expect == -1:
            return self.link.drain(timeout or 0.25)
        if expect is None:
            return self.link.read_until(b";", timeout or self._t_for(32, 0.5))
        return self.link.read_exact(expect, timeout or self._t_for(expect, 0.6))

    @staticmethod
    def _ok(r: bytes) -> bool:
        return r[:2] == b"A;"

    @staticmethod
    def _err(r: bytes) -> bool:
        return r[:2] == b"?;"

    # ---- ID / VE ----
    def id_read(self) -> bytes:
        """Standard CAT ID query; response length is configured per model."""
        n = get_rig_value(self.profile, "responses", "id_length")
        return self._ascii("ID", expect=int(n) if isinstance(n, int) and n > 0 else None)

    def ve(self) -> bytes:
        """VE version query using the selected profile's magic string and response length."""
        n = get_rig_value(self.profile, "responses", "ve_length")
        if not self.ve_magic:
            raise CatError("当前机型配置缺少 VE 魔术串")
        if isinstance(n, int) and n > 0:
            r = self._ascii("VE" + self.ve_magic, expect=n,
                            timeout=self._t_for(n, 1.0))
        else:
            r = self._ascii("VE" + self.ve_magic, expect=None, timeout=2.0)
        return r

    # ---- FD ----
    def fd_read(self, x: int) -> bytes:
        if not get_rig_value(self.profile, "capabilities", "fd_read", False):
            raise CatError("当前机型配置未启用 FD 读取")
        if x not in (0, 1, 2):
            raise ValueError("FD 通道必须为 0/1/2")
        n = get_rig_value(self.profile, "responses", "fd_length")
        return self._ascii(f"FD00{x}", expect=int(n) if isinstance(n, int) and n > 0 else None)

    def fd_write(self, hex8: str) -> bytes:
        if not get_rig_value(self.profile, "capabilities", "fd_write", False):
            raise CatError("当前机型配置未声明支持 FD 命令")
        h = hex8.strip().upper()
        if len(h) != 8 or any(c not in "0123456789ABCDEF" for c in h):
            raise ValueError("FD 写需要 8 个十六进制字符")
        return self._ascii("FD0020" + h, expect=-1)

    # ---- KT ----
    def kt_read(self) -> bytes:
        if not get_rig_value(self.profile, "capabilities", "kt_read", False):
            raise CatError("当前机型配置未启用 KT 读取")
        n = get_rig_value(self.profile, "responses", "kt_length")
        return self._ascii("KT", expect=int(n) if isinstance(n, int) and n > 0 else None)

    def kt_clear(self) -> bytes:
        if not get_rig_value(self.profile, "capabilities", "kt_write", False):
            raise CatError("当前机型配置未启用 KT 写入/清空")
        return self._ascii("KT0", expect=-1)

    def kt_remaining(self) -> int:
        """读 KT 回包的“剩余空间”字段（2 位十六进制）。"""
        r = self.kt_read()
        if r[:2] != b"KT" or len(r) < 6:
            raise CatError(f"KT 回包异常: {hexs(r)}")
        try:
            return int(r[3:5].decode("ascii"), 16)
        except ValueError as exc:
            raise CatError(f"KT 剩余空间解析失败: {hexs(r)}") from exc

    def kt_write(self, text: str) -> bytes:
        if not text:
            raise ValueError("文本为空")
        if any(ord(ch) > 0x7F for ch in text):
            raise ValueError("KT 文本只接受 ASCII 字符")
        if ";" in text:
            raise ValueError("KT 文本不能包含 ';'（0x3B 会被当作帧尾）")
        text = text.upper()  # CAT 解析器默认把字母大写化
        if len(text) > 30:
            raise ValueError("文本超过 30 字符上限")
        for ch in text:
            code = ord(ch)
            if not (0x20 <= code <= 0x5F or code == 0x7D):
                raise ValueError(
                    f"含非法字符 {ch!r}（固件允许 0x20–0x5F 或 0x7D；字母会转大写）")
        remaining = self.kt_remaining()
        if len(text) > remaining:
            raise ValueError(
                f"文本 {len(text)} 字符，超过缓冲剩余 {remaining} 字符")
        return self._ascii("KT1" + text, expect=-1)

    # ---- EK ----
    def ek(self) -> bytes:
        if not get_rig_value(self.profile, "capabilities", "ek", False):
            raise CatError("当前机型配置未启用 EK")
        return self._ascii("EK", expect=-1)

    # ---- EO ----
    def eo(self, a: int, sign: str, b: int, ddd: int) -> bytes:
        if a not in (0, 1, 2) or b not in (0, 1, 2):
            raise ValueError("a/b 必须为 0/1/2")
        if sign not in "+-":
            raise ValueError("符号必须为 + 或 -")
        if not (0 <= ddd <= 999):
            raise ValueError("数值得在 000–999")
        return self._ascii(f"EO{a}{sign}{b}{ddd:03d}", expect=-1)

    # ---- PE ----
    def _pe_allowed_positions(self):
        """Return the three per-position selector sets for PE A/B/C."""
        positions = get_rig_value(self.profile, "pe_calibration", "selector_positions", None)
        if positions:
            return [{str(v) for v in pos} for pos in positions]
        selectors = {str(v) for v in get_rig_value(self.profile, "pe_calibration", "selectors", [])}
        return [selectors, selectors, selectors]

    def pe_read(self, s0: int, s1: int, m: int):
        if not get_rig_value(self.profile, "capabilities", "pe_calibration", False):
            raise CatError("当前机型配置未启用 PE 校准")
        allowed = self._pe_allowed_positions()
        if any(str(x) not in allowed[i] for i, x in enumerate((s0, s1, m))):
            raise ValueError("PE 选择子不在当前机型配置范围内")
        n = int(get_rig_value(self.profile, "responses", "pe_length", 0))
        if n <= 0:
            raise CatError("当前机型配置未提供 PE 回包长度")
        r = self._ascii(f"PE{s0}{s1}{m}", expect=n)
        if r[:2] == b"?;":
            return None
        return r

    def pe_write_raw(self, params: str) -> bytes:
        """Send a PE write only when the selected profile explicitly enables it.

        The value field is 3 characters: either sign + 2 hex (LEVEL) or 3 hex
        (FREQ/BWTH).  Selector positions are validated per A/B/C, so A=1 is not
        sent to models that only accept A=0/2.
        """
        if not get_rig_value(self.profile, "capabilities", "pe_calibration", False):
            raise CatError("当前机型配置未启用 PE 校准写入")
        p = params.strip().upper()
        if len(p) != 6:
            raise ValueError("PE 写参数必须为 6 字符：3 位选择子 + 3 位值")
        allowed = self._pe_allowed_positions()
        if any(c not in allowed[i] for i, c in enumerate(p[:3])):
            raise ValueError("PE 选择子不在当前机型配置范围内")
        value = p[3:]
        signed = len(value) == 3 and value[0] in "+-" and all(c in "0123456789ABCDEF" for c in value[1:])
        unsigned = len(value) == 3 and all(c in "0123456789ABCDEF" for c in value)
        if not (signed or unsigned):
            raise ValueError("PE 值必须为 +XX、-XX 或 XXX（3 字符）")
        return self._ascii("PE" + p, expect=-1)

    # ---- E0 / E8 ----
    def e0(self) -> bytes:
        n = get_rig_value(self.profile, "dumps", "e0_length")
        if not isinstance(n, int) or n <= 0:
            raise CatError("当前机型配置未提供已确认的 E0 转储长度")
        return self._ascii("E0", expect=n, timeout=self._t_for(n, 1.5))

    def e8(self) -> bytes:
        n = get_rig_value(self.profile, "dumps", "e8_length")
        if not isinstance(n, int) or n <= 0:
            raise CatError("当前机型配置未提供已确认的 E8 转储长度")
        return self._ascii("E8", expect=n, timeout=self._t_for(n, 3.0))

    # ---- EX（标准 CAT 菜单读写；用于功率上限 AC） ----
    @staticmethod
    def _norm_ex_addr(addr) -> str:
        a = str(addr).strip().upper()
        if not a or len(a) % 2 or any(c not in "0123456789ABCDEF" for c in a):
            raise ValueError("EX 菜单地址必须是偶数位十六进制字符")
        return a

    def ex_read(self, addr) -> int:
        """EX 菜单读取：发送 EX<地址>;，回包为 <地址回显><数值>;。"""
        a = self._norm_ex_addr(addr)
        r = self._ascii("EX" + a, expect=None, timeout=self._t_for(32, 0.6))
        if self._err(r):
            raise CatError(f"EX 读取被拒绝（{a}）")
        body = r.decode("ascii", "replace")
        prefix = "EX" + a
        if not body.startswith(prefix):
            raise CatError(f"EX 回包异常: {hexs(r)}")
        val = body[len(prefix):].rstrip(";").strip()
        if not val.isdigit():
            raise CatError(f"EX 回包数值解析失败: {body!r}")
        return int(val)

    def ex_set(self, addr, value: int, digits: int = 3) -> bytes:
        """EX 菜单写入：发送 EX<地址><数值>;，成功回 A;，失败回 ?;。

        EX 菜单项由固件按当前档位区间校验；超出当前功率等级上限时返回 ?;。
        """
        a = self._norm_ex_addr(addr)
        digits = int(digits)
        value = int(value)
        if not (0 <= value < 10 ** digits):
            raise ValueError(f"EX 数值超出 {digits} 位范围")
        r = self._ascii("EX" + a + f"{value:0{digits}d}", expect=2)
        if self._err(r):
            raise CatError(
                f"EX 写入被拒绝（{a}={value}）：可能超出设备当前档位上限；"
                "刚改过档位时需断电重启再写")
        if not self._ok(r):
            raise CatError(f"EX 写入未确认: {hexs(r)}")
        return r

    def ac_write_cfg(self) -> dict:
        """当前机型声明的功率上限（AC）写入配置；未启用时返回空字典。"""
        cfg = get_rig_value(self.profile, "power", "ac_write", {})
        return dict(cfg) if isinstance(cfg, dict) else {}

    def ac_targets(self, class_label: str) -> list[int]:
        """按功率等级标签返回各上限项的目标值（顺序与 items 一致）。"""
        targets = self.ac_write_cfg().get("targets", {}) or {}
        key = str(class_label)
        if key not in targets:
            raise CatError(f"当前机型未配置“{class_label}”的功率上限目标值")
        return [int(v) for v in targets[key]]

    def write_power_limits(self, class_label: str, progress=None) -> list[dict]:
        """按当前机型配置写入各功率上限项（标准 CAT EX），并逐项回读。

        返回 [{label, addr, target, readback, error}, ...]；即使某项被固件
        拒绝（如超出当前档位上限）也会继续写其余项，便于一次性看到结果。
        """
        cfg = self.ac_write_cfg()
        items = list(cfg.get("items", []) or [])
        if not items:
            raise CatError("当前机型未配置可写的功率上限项")
        values = self.ac_targets(class_label)
        if len(items) != len(values):
            raise CatError("功率上限项数与目标值数量不一致")
        digits = int(cfg.get("value_digits", 3))
        results = []
        for i, (item, value) in enumerate(zip(items, values)):
            addr = item.get("addr")
            entry = {"label": str(item.get("label", "")), "addr": str(addr),
                     "target": int(value), "readback": None, "error": None}
            try:
                self.ex_set(addr, int(value), digits)
            except CatError as exc:
                entry["error"] = str(exc)
            try:
                entry["readback"] = self.ex_read(addr)
            except CatError:
                entry["readback"] = None
            results.append(entry)
            if progress:
                progress(i + 1, len(items))
        return results

    # ---- JP ----
    def jp_read(self) -> bytes:
        """JP 读取：JP 前缀 + 空参数，回包长度由当前机型配置定义。"""
        n = get_rig_value(self.profile, "responses", "jp_length")
        if not self.jp_prefix:
            raise CatError("当前机型配置缺少 JP 前缀")
        return self._ascii(self.jp_prefix, expect=int(n) if isinstance(n, int) and n > 0 else None,
                           timeout=2.0 if not n else None)

    def jp_write(self, hex8: str) -> bytes:
        """Write JP only when explicitly enabled by the selected profile."""
        if not get_rig_value(self.profile, "capabilities", "jp_write", False):
            raise CatError("当前机型配置未启用 JP 写入")
        h = hex8.strip().upper()
        if len(h) != 8 or any(c not in "0123456789ABCDEF" for c in h):
            raise ValueError("JP 写需要 8 个十六进制字符")
        v = int(h[:4], 16)
        w = int(h[4:], 16)
        if (v ^ w) != 0xFFFF:
            raise ValueError(f"两字必须互补：0x{v:04X} ^ 0x{w:04X} != 0xFFFF")
        return self._ascii(self.jp_prefix + h, expect=-1)

    # ---- ZZ ----
    def zz(self, value: int) -> None:
        if not get_rig_value(self.profile, "capabilities", "service_mode", False):
            raise CatError("当前机型配置未启用服务模式命令")
        configured_value = get_rig_value(self.profile, "service_mode", "enter_value")
        if configured_value is None or int(value) != int(configured_value):
            raise CatError("只允许发送当前配置明确声明的服务模式入口值")
        if not self.zz_prefix:
            raise CatError("当前机型配置缺少 ZZ 前缀")
        self.link.write(f"ZZ{self.zz_prefix}{int(value):02X}".encode() + b";")
        time.sleep(0.3)

    # ---- SP 子协议 ----
    @staticmethod
    def _validate_sp_read(r: bytes, prefix: bytes, addr: int) -> bytes:
        """验证 SP 读回包的命令、地址、长度、校验和及结束符。"""
        expected_addr = addr.to_bytes(2, "big")
        if (len(r) != 9 or r[:3] != prefix or r[3:5] != expected_addr
                or r[7] != (sum(r[:7]) & 0xFF) or r[8:] != b";"):
            raise CatError(
                f"{prefix.decode('ascii')} 读回包校验失败 "
                f"(地址 0x{addr:04X}): {hexs(r)}")
        return r

    @staticmethod
    def _require_ack(r: bytes, command: str) -> bytes:
        """验证应返回 ACK 的 SP 写命令，避免把 NACK/异常回包当作成功。"""
        if r != b"A;":
            raise CatError(f"{command} 写入未确认: {hexs(r)}")
        return r

    def spw(self, addr: int, d0: int, d1: int) -> bytes:
        if not get_rig_value(self.profile, "capabilities", "fram_raw_write", False):
            raise CatError("当前机型配置未启用 FRAM 写入")
        min_addr = int(get_rig_value(self.profile, "fram", "address_min", 0))
        max_addr = int(get_rig_value(self.profile, "fram", "address_max", -1))
        if not (min_addr <= addr <= max_addr):
            raise ValueError(f"地址须在当前机型 FRAM 范围内 (0x{min_addr:04X}–0x{max_addr:04X})")
        r = self._sp(bytes([0x57, addr >> 8, addr & 0xFF, d0 & 0xFF, d1 & 0xFF]), 2)
        return self._require_ack(r, "SPW")

    def spr(self, addr: int) -> bytes:
        min_addr = int(get_rig_value(self.profile, "fram", "address_min", 0))
        max_addr = int(get_rig_value(self.profile, "fram", "address_max", -1))
        if not (min_addr <= addr <= max_addr):
            raise ValueError(f"地址须在当前机型 FRAM 范围内 (0x{min_addr:04X}–0x{max_addr:04X})")
        r = self._sp(bytes([0x52, addr >> 8, addr & 0xFF]), 9, allow_terminator=True)
        return self._validate_sp_read(r, b"SPR", addr)

    def spw_table(self, addr: int, data: int, pad: int = 0x00) -> bytes:
        if not get_rig_value(self.profile, "capabilities", "settings_table", False):
            raise CatError("当前机型配置未启用设置表写入")
        start = get_rig_value(self.profile, "settings_table", "start_addr")
        end = get_rig_value(self.profile, "settings_table", "end_addr")
        if start is None or end is None or not (int(start) <= addr <= int(end)):
            raise ValueError("当前机型未配置此设置表地址，或地址超出范围")
        r = self._sp(bytes([0x77, addr >> 8, addr & 0xFF, pad & 0xFF, data & 0xFF]), 2)
        return self._require_ack(r, "SPw")

    def spr_table(self, addr: int) -> bytes:
        start = get_rig_value(self.profile, "settings_table", "start_addr")
        end = get_rig_value(self.profile, "settings_table", "end_addr")
        if addr != 0xFFFF and (start is None or end is None or not (int(start) <= addr <= int(end))):
            raise ValueError("当前机型未配置此设置表地址，或地址超出范围")
        r = self._sp(bytes([0x72, addr >> 8, addr & 0xFF]), 9, allow_terminator=True)
        return self._validate_sp_read(r, b"SPr", addr)

    def spm(self, which: str) -> bytes:
        w = which.strip().upper()
        if w == "TR":
            if not get_rig_value(self.profile, "capabilities", "spm_tr", False):
                raise CatError("当前机型配置未声明支持 SPMTR")
            n = int(get_rig_value(self.profile, "responses", "spm_tr_length", 0))
            if n <= 0: raise CatError("当前机型配置未提供 SPM TR 回包长度")
            return self._sp(b"MTR", n, allow_terminator=True)
        if w == "T2" or w == "2":
            if not get_rig_value(self.profile, "capabilities", "spm_t2", False):
                raise CatError("当前机型配置未声明支持 SPMT2")
            n = int(get_rig_value(self.profile, "responses", "spm_t2_length", 0))
            if n <= 0: raise CatError("当前机型配置未提供 SPM T2 回包长度")
            return self._sp(b"MT2", n, allow_terminator=True)
        raise ValueError("SPM 参数为 TR 或 T2")

    def spn_read(self) -> bytes:
        n = int(get_rig_value(self.profile, "responses", "spn_length", 0))
        if n <= 0:
            raise CatError("当前机型配置未提供 SPN 回包长度")
        return self._sp(b"N\xff\xff", n, allow_terminator=True)

    def spn_write(self, value: int) -> bytes:
        if not get_rig_value(self.profile, "capabilities", "service_mode", False) or not get_rig_value(self.profile, "capabilities", "service_menu", False):
            raise CatError("当前机型配置未启用带项目映射的 SPN 写入")
        count = get_rig_value(self.profile, "service_menu", "item_count")
        if not isinstance(count, int) or count <= 0:
            raise CatError("当前机型配置未提供服务菜单项目数")
        if not (0 <= value < count):
            raise ValueError(f"SPN 写值须小于 {count}")
        return self._sp(bytes([0x4E, value >> 8, value & 0xFF]), 2)

    def spa_we(self) -> bytes:
        if not get_rig_value(self.profile, "capabilities", "spa_we", False):
            raise CatError("当前机型配置未声明支持 SPAWE")
        return self._sp(b"AWE", 2)

    def spa_rd(self) -> bytes:
        if not get_rig_value(self.profile, "capabilities", "spa_rd", False):
            raise CatError("当前机型配置未声明支持 SPARD")
        table_len = get_rig_value(self.profile, "settings_table", "length")
        if not isinstance(table_len, int) or table_len <= 0:
            raise CatError("当前机型配置未提供 SPARD 设置表长度")
        response_len = 5 + table_len + 2
        return self._sp(b"ARD", response_len, timeout=self._t_for(response_len, 1.2),
                        allow_terminator=True)

    def spa_cl(self) -> bytes:
        if not get_rig_value(self.profile, "capabilities", "spa_cl", False):
            raise CatError("当前机型配置未声明支持 SPACL")
        return self._sp(b"ACL", 2)

    def spa_et(self) -> bytes:
        if not get_rig_value(self.profile, "capabilities", "spa_et", False):
            raise CatError("当前机型配置未声明支持 SPAET")
        return self._sp(b"AET", 2)

    def spsdr(self, b0: int, b1: int) -> bytes:
        """SPSDR：读取 SDR 相关的 9 字节数据帧。"""
        if not get_rig_value(self.profile, "capabilities", "spsdr", False):
            raise CatError("当前机型配置未声明支持 SPSDR")
        return self._sp(bytes([0x53, 0x44, 0x52, b0 & 0xFF, b1 & 0xFF]), 9,
                        allow_terminator=True)

    # ---- 配置字节 / FRAM 批量 ----
    def read_config_pair(self) -> tuple[int, int, bytes]:
        """读取 配置字节对 配置对，返回 (b0, b1, 原始回包)。"""
        addr = get_rig_value(self.profile, "fram", "config_pair_addr")
        if addr is None:
            raise CatError("当前机型配置未提供配置对地址")
        r = self.spr(int(addr))
        if r[:3] != b"SPR" or len(r) < 9:
            raise CatError(f"SPR 配置读取异常: {hexs(r)}")
        return r[5], r[6], r

    def write_config_pair(self, b0: int, b1: int) -> bytes:
        """Write the configured byte pair; use a safe adjacent-byte fallback if framing collides."""
        if not (0 <= b0 <= 0xFF and 0 <= b1 <= 0xFF):
            raise ValueError("配置字节必须在 0x00–0xFF")
        addr = get_rig_value(self.profile, "fram", "config_pair_addr")
        if addr is None:
            raise CatError("当前机型配置未提供配置对地址")
        addr = int(addr)
        try:
            return self.spw(addr, b0, b1)
        except CatError as exc:
            if "0x3B" not in str(exc):
                raise
            # The fallback can only safely update the second byte. Never silently
            # skip a requested change to the first byte.
            current = self.spr(addr)
            if current[5] != b0:
                raise CatError("SP 帧包含结束符且配置对首字节也需修改；已拒绝不完整写入") from exc
            adjacent = self.spr(addr + 1)
            return self.spw(addr + 1, b1, adjacent[6])

    def write_fram(self, start: int, data: bytes, progress=None,
                   cancel=None, verify=False) -> int:
        """把 bytes 逐 2 字节写入外部 FRAM。

        奇数长度时与相邻字节配对写入，并保留未被请求的相邻字节原值；
        最后一字节落在 FRAM 上限时改为与前一个字节配对，避免越界。
        """
        min_addr = int(get_rig_value(self.profile, "fram", "address_min", 0))
        max_addr = int(get_rig_value(self.profile, "fram", "address_max", -1))
        if not (min_addr <= start <= max_addr):
            raise ValueError("FRAM 起始地址超出当前机型配置")
        if start + len(data) - 1 > max_addr:
            raise ValueError("写入范围超过当前机型 FRAM 上限")
        if not data:
            return 0
        total = (len(data) + 1) // 2
        for k in range(total):
            if cancel is not None and cancel.is_set():
                raise CatError("已取消")
            i = k * 2
            if i + 1 < len(data):
                self.spw(start + i, data[i], data[i + 1])
            elif start + i < max_addr:
                # 最后一个奇数字节：读取并保留其后的相邻字节。
                d1 = self.spr(start + i + 1)[5]
                self.spw(start + i, data[i], d1)
            elif i > 0:
                # 最后一个字节正好在 FRAM 上限：与前一个数据字节配对。
                self.spw(start + i - 1, data[i - 1], data[i])
            elif start > min_addr:
                # 只有上限处一个字节：保留前一个字节，写回一对。
                prev = self.spr(start - 1)[5]
                self.spw(start - 1, prev, data[i])
            else:
                raise ValueError("FRAM 上限处的单字节无法安全成对写入")
            if progress and (k % 32 == 0 or k == total - 1):
                progress(k + 1, total)
        if verify:
            back = self.read_fram(start, start + len(data) - 1, strict=True)
            if back != data:
                raise CatError("FRAM 写后回读不一致")
        return len(data)

    def spa_table(self) -> bytes:
        """SPARD table read: validate and return the profile-defined table bytes."""
        r = self.spa_rd()
        table_len = get_rig_value(self.profile, "settings_table", "length")
        if not isinstance(table_len, int) or table_len <= 0:
            raise CatError("当前机型未配置 SPARD 设置表长度")
        if len(r) < 5 + table_len + 2:
            raise CatError(f"SPARD 回包过短: {len(r)}")
        table = r[5:5 + table_len]
        chk = sum(r[:5 + table_len]) & 0xFF
        if r[5 + table_len] != chk:
            raise CatError(f"SPARD 校验失败: 期望 {chk:02X}，收到 {r[5 + table_len]:02X}")
        return table

    @staticmethod
    def parse_jp(r: bytes) -> dict:
        """解析 JP 回包（FTDX10 25 字节 / FT-891 15 字节），返回可读字段。"""
        out = {"raw_hex": r.hex(), "length": len(r)}
        # FTDX10 JP 回 25 字节（含 0x2F0 区 5 字节）；FT-891 回 15 字节。
        # 两种格式都以“6 字节前缀 + 4 hex v + 4 hex ~v”开头，故 >=14 即可解析。
        if len(r) >= 14:
            # JP 回包是 ASCII：当前前缀 + 参数字段 + ';'
            out["head"] = r[:6].decode("latin-1", "replace")
            vh = r[6:10].decode("ascii", "replace").upper()
            wh = r[10:14].decode("ascii", "replace").upper()
            out["v_hex"], out["w_hex"] = vh, wh
            try:
                out["v"] = int(vh, 16)
                out["w"] = int(wh, 16)
                out["complement"] = ((out["v"] ^ out["w"]) == 0xFFFF)
            except ValueError:
                out["complement"] = None
            out["extra_hex"] = r[14:24].decode("ascii", "replace").upper().rstrip(";")
        return out

    # ---- 组合读取 ----
    def read_fram(self, start: int, end: int, progress=None, cancel=None,
                  quiet=True, strict=False):
        """用 SPR 读 [start,end]（含）的外部 FRAM 地址空间，返回 bytes。

        strict=True 时任何一帧读失败都会抛错；备份/编辑器应使用严格模式，
        避免把读失败静默填成 0xFF 后误当成真实数据。
        """
        min_addr = int(get_rig_value(self.profile, "fram", "address_min", 0))
        max_addr = int(get_rig_value(self.profile, "fram", "address_max", -1))
        if not (min_addr <= start <= end <= max_addr):
            raise ValueError(f"FRAM 读取范围须在当前机型地址 0x{min_addr:04X}–0x{max_addr:04X} 内")
        buf = bytearray()
        total = (end - start + 1 + 1) // 2
        n = 0
        for addr in range(start, end + 1, 2):
            if cancel is not None and cancel.is_set():
                raise CatError("已取消")
            try:
                r = self.spr(addr)
                buf += r[5:7]
            except CatError:
                if strict:
                    raise
                buf += b"\xff\xff"          # 非严格模式：读失败以 0xFF 填充
            n += 1
            if progress and (n % 32 == 0 or n == total):
                progress(n, total)
        return bytes(buf[: end - start + 1])

    def read_settings_table(self, progress=None, cancel=None) -> bytes:
        buf = bytearray()
        start = get_rig_value(self.profile, "settings_table", "start_addr")
        end = get_rig_value(self.profile, "settings_table", "end_addr")
        if start is None or end is None:
            raise CatError("当前机型配置未提供设置表地址范围")
        start, end = int(start), int(end)
        total = end - start + 1
        for i, addr in enumerate(range(start, end + 1)):
            if cancel is not None and cancel.is_set():
                raise CatError("已取消")
            r = self.spr_table(addr)
            buf.append(r[6])
            if progress and (i % 16 == 0 or i == total - 1):
                progress(i + 1, total)
        return bytes(buf)


# ============================================================ 备份 / 恢复
# ============================================================ GUI
