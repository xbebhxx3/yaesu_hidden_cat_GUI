# -*- coding: utf-8 -*-
"""Tkinter UI for Yaesu Hedden CAT; presentation layer only."""
import datetime
import json
import queue
import re
import threading
import time

from ..core import *  # shared protocol API
from ..file_ops import make_backup, restore_table, restore_fram, load_json, save_json, load_bytes, save_bytes

def launch_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(APP_TITLE)
            self.geometry("1280x850")
            self.minsize(1040, 700)
            self._setup_style()

            self.link = SerialLink()
            self.cat = HiddenCat(self.link)
            self.logq = queue.Queue()
            self.uiq = queue.Queue()   # 后台线程 → 主线程的 UI 回调队列
            self.busy = False
            self.cancel = threading.Event()
            self.last_backup = None
            self.detected_device_id = "未查询"
            self.detected_version = "未查询"

            # Model-specific values and optional maps are loaded exclusively from external profiles.
            self.profile_data = load_rig_profiles()
            selected_profile = (self.profile_data["profiles"][0]
                                if self.profile_data.get("profiles") else {})
            self.current_profile_id = selected_profile.get("id", "")
            self.active_profile = dict(selected_profile)
            self.cat.set_profile(selected_profile)
            self.fram_regions = load_fram_regions(selected_profile)
            self.service_items = load_service_items(selected_profile)
            self.cal_table = b""       # 配置长度 设置表缓存（服务菜单 / 校准页共用）
            self.cfg_pair = None       # 配置字节对 最近一次读取值
            self.var_backup_ack = tk.BooleanVar(value=False)  # 写前“已备份”确认

            self._build_menu()
            self._build()
            self.after(80, self._poll)
            if serial is None:
                self.log("⚠ 未检测到 pyserial，请先 `pip install pyserial`")

        def _setup_style(self):
            """Use a clean, modern ttk theme while keeping native platform widgets."""
            style = ttk.Style(self)
            try:
                if "clam" in style.theme_names():
                    style.theme_use("clam")
            except Exception:
                pass
            bg = "#F3F6FA"
            panel = "#FFFFFF"
            ink = "#243247"
            accent = "#2864DC"
            self.configure(background=bg)
            style.configure(".", font=("Segoe UI", 10), foreground=ink)
            style.configure("TFrame", background=bg)
            style.configure("Card.TFrame", background=panel)
            style.configure("TLabel", background=bg, foreground=ink, padding=(1, 2))
            style.configure("Title.TLabel", font=("Segoe UI Semibold", 15), foreground="#152238")
            style.configure("Subtle.TLabel", foreground="#637188")
            style.configure("TButton", padding=(10, 6), relief="flat")
            style.map("TButton", background=[("active", "#E3EBF8")])
            style.configure("Accent.TButton", background=accent, foreground="#FFFFFF", padding=(12, 7))
            style.map("Accent.TButton", background=[("active", "#1E50B5"), ("disabled", "#B8C3D4")],
                      foreground=[("disabled", "#F5F7FA")])
            style.configure("TEntry", padding=5, fieldbackground="#FFFFFF")
            style.configure("TCombobox", padding=5, fieldbackground="#FFFFFF")
            style.configure("TNotebook", background=bg, borderwidth=0, tabmargins=(3, 5, 3, 0))
            style.configure("TNotebook.Tab", padding=(14, 8), font=("Segoe UI", 10))
            style.map("TNotebook.Tab", background=[("selected", panel), ("active", "#E6ECF5")],
                      foreground=[("selected", accent)])
            style.configure("TLabelframe", background=bg, bordercolor="#D9E1EC", relief="solid")
            style.configure("TLabelframe.Label", background=bg, foreground="#42536B", font=("Segoe UI Semibold", 10))
            style.configure("Treeview", rowheight=34, background="#FFFFFF", fieldbackground="#FFFFFF",
                            bordercolor="#D9E1EC", font=("Segoe UI", 9))
            style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9), padding=6)
            style.map("Treeview", background=[("selected", "#DCE8FC")], foreground=[("selected", ink)])

        # ---------------- 通用 ----------------
        def _profile_cfg(self):
            # Keep an ordinary Python dict so worker threads never query Tk widgets.
            profile = getattr(self, "active_profile", None)
            if isinstance(profile, dict):
                return profile
            return self._selected_profile() or {}

        def _cfg(self, section, key, default=None):
            return get_rig_value(self._profile_cfg(), section, key, default)

        def _cap(self, name):
            return bool(get_rig_value(self._profile_cfg(), "capabilities", name, False))

        def _power_label(self, bits):
            levels = self._cfg("power", "levels", {})
            mask = int(self._cfg("power", "power_mask", 0))
            return next((str(label) for label, value in levels.items()
                         if int(value) == (int(bits) & mask)), "未知功率等级")

        def _ts_index_from_ui(self):
            value = self.cmb_ts.get().strip()
            if " — " in value:
                value = value.split(" — ", 1)[0].strip()
            return int(value)

        def _ts_label(self, ts):
            """Return the configured label plus any known validity caveat."""
            labels = self._cfg("power", "ts_labels", {}) or {}
            label = labels.get(str(ts), f"频段组合 {ts}")
            invalid = self._cfg("power", "ts_invalid", []) or []
            if any(int(x) == int(ts) for x in invalid):
                return f"{label}（资料标为无效）"
            conditional = self._cfg("power", "ts_conditional", {}) or {}
            reason = conditional.get(str(ts))
            return f"{label}（{reason}）" if reason else label

        def _ts_ui_value(self, ts):
            return f"{ts} — {self._ts_label(ts)}"

        def _profile_memory(self, key, default=None):
            return get_rig_value(self._profile_cfg(), "fram", key, default)

        def ui(self, fn):
            """把 UI 更新排到主线程执行；tkinter 不允许后台线程直接操作控件。"""
            self.uiq.put(fn)

        def _q_append_text(self, widget, text: str, see: bool = True):
            """后台线程安全的文本追加。"""
            def do():
                widget.insert("end", text)
                if see:
                    widget.see("end")
            self.ui(do)

        def log(self, msg: str):
            self.logq.put(("log", msg))

        def status(self, msg: str):
            self.logq.put(("status", msg))

        def _poll(self):
            try:
                while True:
                    kind, msg = self.logq.get_nowait()
                    if kind == "log":
                        self.logbox.configure(state="normal")
                        ts = datetime.datetime.now().strftime("%H:%M:%S")
                        self.logbox.insert("end", f"[{ts}] {msg}\n")
                        self.logbox.see("end")
                        self.logbox.configure(state="disabled")
                    elif kind == "status":
                        self.var_status.set(msg)
                    elif kind == "busy":
                        self.busy = msg
                        self.btn_cancel.configure(state=("normal" if msg else "disabled"))
            except queue.Empty:
                pass
            try:
                while True:
                    fn = self.uiq.get_nowait()
                    try:
                        fn()
                    except Exception as e:
                        self.log(f"UI 回调错误: {e}")
            except queue.Empty:
                pass
            self.after(80, self._poll)

        def _set_busy(self, b, text=""):
            self.logq.put(("busy", b))
            if text:
                self.status(text)

        def bg(self, fn, done_msgs=None):
            """后台线程执行 fn(cat)；fn 内可用 self.log/self.status。"""
            if self.busy:
                messagebox.showinfo(APP_TITLE, "有任务正在执行，请稍候或点击取消。")
                return
            self.cancel.clear()
            self._set_busy(True, "运行中…")

            def runner():
                try:
                    fn(self.cat)
                except Exception as e:  # noqa
                    self.log(f"✗ 错误: {e}")
                finally:
                    self._set_busy(False, "就绪")
            threading.Thread(target=runner, daemon=True).start()

        def require_open(self) -> bool:
            if not self.link.is_open:
                messagebox.showwarning(APP_TITLE, "请先在“连接”页打开串口。")
                return False
            return True

        def on_cancel(self):
            self.cancel.set()
            self.log("… 已请求取消")

        # ---------------- 菜单 ----------------
        def _build_menu(self):
            m = tk.Menu(self)
            hm = tk.Menu(m, tearoff=0)
            hm.add_command(label="使用说明", command=self._help)
            hm.add_command(label="协议/安全提示", command=self._help_safety)
            hm.add_separator()
            hm.add_command(label="退出", command=self.destroy)
            m.add_cascade(label="帮助", menu=hm)
            self.config(menu=m)

        def _help(self):
            messagebox.showinfo(APP_TITLE, __doc__[:1500])

        def _help_safety(self):
            messagebox.showwarning(
                "安全提示",
                "· SP / PE / JP 会写校准或启动参数，可能改变发射指标或导致无法开机。\n"
                "· ZZ 会立即复位整机。\n"
                "· 写操作前请先备份当前机型相关 FRAM 区域，并确认可恢复。\n"
                "· SP 是二进制子协议：参数/校验为原始字节，且不能含 0x3B(';')。\n"
                "· 服务模式/复位命令行为依机型而异；仅使用当前配置明确标记已验证的命令。\n"
                "  不要把复位或初始化命令当作普通退出命令。\n"
                "· 本工具为静态逆向结论，未在真机验证；风险自负。")

        # ---------------- 布局 ----------------
        def _build(self):
            top = ttk.Frame(self, padding=(6, 4))
            top.pack(fill="x")
            self.var_status = tk.StringVar(value="就绪")
            ttk.Label(top, textvariable=self.var_status, foreground="#054").pack(side="left")
            self.btn_cancel = ttk.Button(top, text="取消任务", command=self.on_cancel,
                                         state="disabled")
            self.btn_cancel.pack(side="right")

            self.var_model_info = tk.StringVar(value="机型：未选择  |  设备：未连接")
            model_bar = ttk.Frame(self, padding=(8, 0, 8, 5))
            model_bar.pack(fill="x")
            ttk.Label(model_bar, textvariable=self.var_model_info, anchor="w").pack(fill="x")

            self.nb = ttk.Notebook(self)
            self.nb.pack(fill="both", expand=True, padx=6, pady=(0, 4))
            self._tab_connect()
            self._tab_profile()
            self._tab_quick()
            self._tab_power()
            self._tab_reg()
            self._tab_fram_full()
            self._tab_service()
            self._tab_cal()
            self._tab_pe()
            self._tab_backup()
            self._tab_term()
            self._refresh_model_banner()
            self._apply_model_tab_gating()

            lf = ttk.LabelFrame(self, text="运行日志")
            lf.pack(fill="both", padx=6, pady=(0, 6))
            self.logbox = scrolledtext.ScrolledText(lf, height=6, state="disabled",
                                                    font=("monospace", 9))
            self.logbox.pack(fill="both", expand=True)
            self._attach_tooltips()

        def _attach_tooltips(self):
            """给所有按钮/复选框绑定中文悬浮说明（重复文字单独绑定）。"""
            def walk(w):
                for c in w.winfo_children():
                    yield c
                    yield from walk(c)

            for w in walk(self):
                try:
                    txt = w.cget("text")
                except Exception:
                    continue
                tip = BUTTON_TIPS.get(txt)
                if tip is None:
                    for prefix, key in (
                            ("E0 读取", "E0 读取"),
                            ("E8 读取", "E8 读取"),
                            ("SPARD 读取", "SPARD 读取"),
                            ("JP 写入", "JP 写入（未开放）")):
                        if str(txt).startswith(prefix):
                            tip = BUTTON_TIPS.get(key)
                            break
                if tip:
                    ToolTip(w, tip)
            for attr, tip in (
                ("btn_pe_send", "发送校准参数写入命令（有确认框）。写操作可能改变校准值。"),
                ("btn_term_ascii", "发送 CAT 文本命令（自动补结束符）并显示回包。"),
                ("btn_term_hex", "高级功能：按原始字节发送协议帧。"),
            ):
                w = getattr(self, attr, None)
                if w is not None:
                    ToolTip(w, tip)

        def _mktext(self, parent, height=12):
            t = scrolledtext.ScrolledText(parent, height=height, font=("monospace", 9))
            return t

        def _tree(self, parent, cols, headings, height):
            """创建带纵向/横向滚动条的 Treeview，返回 (容器, tree)。

            headings 为 (列名, 表头, 宽度) 列表；滚动条保证长表/宽表完整可见。
            """
            box = ttk.Frame(parent)
            tree = ttk.Treeview(box, columns=cols, show="headings", height=height)
            for c, txt, width in headings:
                tree.heading(c, text=txt)
                tree.column(c, width=width, anchor="w")
            ybar = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
            xbar = ttk.Scrollbar(box, orient="horizontal", command=tree.xview)
            tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
            ybar.pack(side="right", fill="y")
            xbar.pack(side="bottom", fill="x")
            tree.pack(side="left", fill="both", expand=True)
            return box, tree

        # ---------------- Tab: 连接 ----------------
        def _tab_connect(self):
            f = ttk.Frame(self.nb, padding=10)
            self.nb.add(f, text="连接")
            row = ttk.Frame(f)
            row.pack(fill="x", pady=4)
            ttk.Label(row, text="端口:").pack(side="left")
            self.cmb_port = ttk.Combobox(row, width=28)
            self.cmb_port.pack(side="left", padx=4)
            ttk.Button(row, text="刷新", command=self.refresh_ports).pack(side="left")
            ttk.Label(row, text="波特率:").pack(side="left", padx=(12, 2))
            self.cmb_baud = ttk.Combobox(row, width=10, values=("4800", "9600", "19200", "38400", "57600", "115200"),
                                         state="readonly")
            initial_baud = self._cfg("serial", "baud_default")
            self.cmb_baud.set(str(initial_baud) if initial_baud in (4800, 9600, 19200, 38400, 57600, 115200) else "")
            self.cmb_baud.pack(side="left")
            ttk.Label(row, text="停止位:").pack(side="left", padx=(10, 2))
            self.cmb_stopbits = ttk.Combobox(row, width=4, values=("1", "2"), state="readonly")
            initial_stopbits = self._cfg("serial", "stop_bits")
            self.cmb_stopbits.set(str(initial_stopbits) if initial_stopbits in (1, 2) else "")
            self.cmb_stopbits.pack(side="left")
            self.btn_open = ttk.Button(row, text="打开", command=self.toggle_open)
            self.btn_open.pack(side="left", padx=10)

            info = ttk.Label(f, foreground="#555", justify="left", text=(
                "请选择电台对应的 CAT 串口；串口参数按当前机型配置。\n"
                "波特率必须与电台 CAT RATE 菜单一致。\n"
                "帧以 ';' 结束；命令能力取决于当前机型配置。"))
            info.pack(anchor="w", pady=8)
            self.refresh_ports()

        def refresh_ports(self):
            if list_ports is None:
                self.cmb_port["values"] = ()
                return
            ports = [p.device for p in list_ports.comports()]
            self.cmb_port["values"] = ports
            if ports and not self.cmb_port.get():
                self.cmb_port.set(ports[0])

        def toggle_open(self):
            if self.link.is_open:
                self.link.close()
                self.btn_open.configure(text="打开")
                self.status("已关闭")
                self._refresh_model_banner()
                self.log("串口已关闭")
            else:
                port = self.cmb_port.get().strip()
                if not port:
                    messagebox.showwarning(APP_TITLE, "请选择端口")
                    return
                try:
                    # Apply the selected external profile before probing the radio.
                    profile = self._profile_cfg()
                    self.cat.set_profile(profile)
                    if hasattr(self, "ent_prof_ve"):
                        ve = self.ent_prof_ve.get().strip()
                        code = self.ent_prof_service_code.get().strip().upper()
                        if code and (len(code) != 4 or any(c not in "0123456789ABCDEF" for c in code)):
                            raise ValueError("JP/ZZ 共用机型代码必须为 4 位十六进制字符")
                        self.cat.set_magic(ve or None, ("JP" + code) if code else None,
                                           ("PE" + code) if code else None)
                    if not self.cmb_baud.get().strip():
                        raise ValueError("当前机型未设置默认波特率，请先选择与电台 CAT RATE 一致的值")
                    if not self.cmb_stopbits.get().strip():
                        raise ValueError("当前机型未设置停止位，请按电台资料选择 1 或 2")
                    stopbits = int(self.cmb_stopbits.get())
                    serial_cfg = self._profile_cfg().get("serial", {})
                    data_bits = serial_cfg.get("data_bits")
                    parity = serial_cfg.get("parity")
                    flow_mode = serial_cfg.get("flow_control_mode")
                    if data_bits not in (5, 6, 7, 8) or parity not in ("N", "E", "O", "M", "S") or flow_mode not in ("none", "rtscts", "xonxoff", "dsrdtr"):
                        raise ValueError("当前机型配置未完整定义数据位/校验/流控，请先完善 profiles/*.json")
                    self.link.open(
                        port, int(self.cmb_baud.get()), stopbits=stopbits,
                        bytesize=int(data_bits), parity=str(parity),
                        flow_control_mode=str(flow_mode))
                    self.btn_open.configure(text="关闭")
                    self.status(f"已打开 {port} @ {self.link.baud} {self.link.bytesize}{self.link.parity}{self.link.stopbits} / {self.link.flow_control_mode}")
                    self.log(f"串口已打开: {port} @ {self.link.baud} {self.link.bytesize}{self.link.parity}{self.link.stopbits} / {self.link.flow_control_mode}")
                    self._refresh_model_banner()
                    self.bg(self._probe_connected_rig)
                except Exception as e:
                    messagebox.showerror(APP_TITLE, f"打开失败: {e}")

        def _probe_connected_rig(self, cat):
            """Read standard CAT ID and the selected model's VE version after connection."""
            try:
                rid = cat.id_read()
                id_text = rid.decode('latin-1', 'replace').strip() or hexs(rid)
                self.log(f"设备 ID: {id_text}")
                self.ui(lambda value=id_text: self._set_detected_identity(device_id=value))
            except Exception as exc:
                self.log(f"ID 查询未成功（不代表连接失败）: {exc}")
            try:
                r = cat.ve()
                version_text = r.decode('latin-1', 'replace').strip() or hexs(r)
                self.log(f"固件/VE 信息: {version_text}")
                self.ui(lambda value=version_text: self._set_detected_identity(version=value))
            except Exception as exc:
                self.log(f"VE 查询未成功；请核对机型魔术串和 CAT RATE: {exc}")

        def _set_detected_identity(self, device_id=None, version=None):
            if device_id is not None:
                self.detected_device_id = device_id
            if version is not None:
                self.detected_version = version
            self._refresh_model_banner()

        # ---------------- Tab: 快捷命令 ----------------
        def _tab_quick(self):
            """高级维护页：两个机型的隐藏维护命令集中在此，按能力开关启用/禁用。"""
            f = ttk.Frame(self.nb, padding=10)
            self.nb.add(f, text="高级维护")

            def group(title):
                box = ttk.LabelFrame(f, text=title)
                box.pack(fill="x", pady=3)
                row = ttk.Frame(box)
                row.pack(fill="x", padx=6, pady=4)
                return row

            def btn(parent, text, cmd, width=22):
                b = ttk.Button(parent, text=text, command=cmd, width=width)
                b.pack(side="left", padx=4)
                return b

            def lbl(parent, text, **kw):
                ttk.Label(parent, text=text, **kw).pack(side="left", padx=(8, 2))

            def entry(parent, width, initial=None):
                e = ttk.Entry(parent, width=width)
                if initial is not None:
                    e.insert(0, initial)
                e.pack(side="left", padx=4)
                return e

            # --- 设备标识 / 维护数据 ---
            row = group("设备标识 / 维护数据 (VE / ID / FD)")
            btn(row, "读取设备版本", self.do_ve)
            btn(row, "读取设备识别信息", self.do_id_read, 18)
            lbl(row, "维护数据通道:")
            self.cmb_fd = ttk.Combobox(row, width=4, values=("0", "1", "2"), state="readonly")
            self.cmb_fd.set("0")
            self.cmb_fd.pack(side="left", padx=4)
            self.btn_fd_read = btn(row, "读取维护数据", self.do_fd_read, 16)

            row = group("维护数据写入 (FD)")
            lbl(row, "FD 写值(8 hex):")
            self.ent_fd = entry(row, 12)
            self.btn_fd_write = btn(row, "写入维护数据（高级）", self.do_fd_write, 20)

            # --- CW 文本存储器 ---
            row = group("CW 文本存储器 (KT)")
            self.btn_kt_read = btn(row, "读取 CW 文本容量", self.do_kt_read, 22)
            lbl(row, "CW 文本:")
            self.ent_kt = entry(row, 28)
            self.btn_kt_write = btn(row, "写入 CW 文本", self.do_kt_write, 16)
            self.btn_kt_clear = btn(row, "清空 CW 文本", self.do_kt_clear, 16)

            # --- 频率偏移微调 / 维护触发 ---
            row = group("频率偏移微调 / 维护触发 (EO / EK)")
            lbl(row, "EO 参数:")
            self.ent_eo = entry(row, 10, "0+0000")
            self.btn_eo = btn(row, "高级频率偏移微调", self.do_eo, 22)
            lbl(row, "仅在明确了解偏移参数时使用", foreground="#555")
            self.btn_ek = btn(row, "EK 触发", self.do_ek, 12)

            # --- JP / 诊断转储 ---
            row = group("JP / 诊断转储 (E0 / E8)")
            btn(row, "JP 读 (当前前缀)", self.do_jp_read, 22)
            e0_len = self._cfg("dumps", "e0_length")
            e8_len = self._cfg("dumps", "e8_length")
            self.btn_e0 = btn(row, f"E0 读取 ({e0_len or '未配置'})", self.do_e0, 18)
            self.btn_e8 = btn(row, f"E8 读取 ({e8_len or '未配置'})", self.do_e8, 18)
            self.btn_jp_write = btn(row, "JP 写入（未开放）", self.do_jp_write, 22)
            self.btn_jp_write.configure(state="disabled")
            self.lbl_jp_write = ttk.Label(row, text="JP 写入未开放；原因见机型提示。", foreground="#555")
            self.lbl_jp_write.pack(side="left", padx=8)

            # --- SP 维护帧（只读类） ---
            row = group("SP 维护帧·只读 (SPARD / SPM / SPN)")
            table_len = self._cfg("settings_table", "length")
            self.btn_spard = btn(row, f"SPARD 读取 ({table_len or '未配置'}B)", self.do_spard, 22)
            self.btn_spm_tr = btn(row, "SPM TR", lambda: self.do_spm("TR"), 10)
            self.btn_spm_t2 = btn(row, "SPM T2", lambda: self.do_spm("T2"), 10)
            self.btn_spn_read = btn(row, "SPN 读取当前项", self.do_spn_read, 18)

            # --- SP 维护帧（写入类） ---
            row = group("SP 维护帧·写入 (SPAWE / SPACL / SPAET / SPN)")
            self.btn_spawe = btn(row, "SPAWE 写使能", self.do_spa_we, 14)
            self.btn_spacl = btn(row, "SPACL 清除", self.do_spa_cl, 12)
            self.btn_spaet = btn(row, "SPAET", self.do_spa_et, 10)
            lbl(row, "SPN 写 N:")
            self.ent_spn_write = entry(row, 8, "000")
            self.btn_spn_write = btn(row, "SPN 写", self.do_spn_write, 10)

            # --- SPSDR ---
            row = group("SPSDR 读取")
            lbl(row, "SPSDR b0/b1:")
            self.ent_spsdr0 = entry(row, 5, "00")
            self.ent_spsdr1 = entry(row, 5, "00")
            self.btn_spsdr = btn(row, "SPSDR 读", self.do_spsdr, 12)

            # --- 服务模式入口（由“功率 / 频段”页移入） ---
            box3 = ttk.LabelFrame(f, text="服务模式入口")
            box3.pack(fill="x", pady=3)
            ttk.Label(box3, foreground="#a00", justify="left", text=(
                "服务模式命令会按机型配置生成，可能立即复位设备。\n"
                "不要将未知 ZZ 操作码作为退出命令；写入或复位前应先备份并确认配置证据。"),
                wraplength=1050).pack(anchor="w", padx=8, pady=4)
            self.btn_zz = ttk.Button(box3, text="进入服务模式（会复位）",
                                     command=self.do_service_mode)
            self.btn_zz.pack(anchor="w", padx=8, pady=(0, 6))

            self.txt_quick = self._mktext(f, 12)
            self.txt_quick.pack(fill="both", expand=True, pady=(8, 0))

        # --- 快捷命令实现 ---
        def _q_show_quick(self, title, text):
            self._q_append_text(self.txt_quick, f"── {title} ──\n{text}\n")

        def _show_quick(self, title, text):
            self.txt_quick.insert("end", f"── {title} ──\n{text}\n")
            self.txt_quick.see("end")

        def do_ve(self):
            if not self.require_open(): return
            def job(cat):
                r = cat.ve()
                self.log(f"VE: {hexs(r)}")
                self.log(f"VE 文本: {r.decode('latin-1')}")
                self._q_show_quick("VE", f"{hexs(r)}\n{r.decode('latin-1')}")
            self.bg(job)

        def do_fd_read(self):
            if not self.require_open(): return
            x = int(self.cmb_fd.get())
            def job(cat):
                r = cat.fd_read(x)
                self._q_show_quick(f"FD 读 通道{x}", hexs(r))
            self.bg(job)

        def do_fd_write(self):
            if not self.require_open(): return
            h = self.ent_fd.get()
            def job(cat):
                r = cat.fd_write(h)
                self._q_show_quick("FD 写", f"({h}) 回包: {hexs(r) if r else '无'}")
            self.bg(job)

        def do_kt_read(self):
            if not self.require_open(): return
            def job(cat):
                r = cat.kt_read()
                self._q_show_quick("KT 读余量", hexs(r))
            self.bg(job)

        def do_kt_write(self):
            if not self.require_open(): return
            t = self.ent_kt.get()
            def job(cat):
                r = cat.kt_write(t)
                self._q_show_quick("KT 写", f"({t!r}) 回包: {hexs(r) if r else '无'}")
            self.bg(job)

        def do_kt_clear(self):
            if not self.require_open(): return
            def job(cat):
                r = cat.kt_clear()
                self._q_show_quick("KT 清空", f"回包: {hexs(r) if r else '无'}")
            self.bg(job)

        def do_ek(self):
            if not self.require_open(): return
            def job(cat):
                r = cat.ek()
                self._q_show_quick("EK", f"回包: {hexs(r) if r else '无'}")
            self.bg(job)

        def do_eo(self):
            if not self.require_open(): return
            s = self.ent_eo.get().strip()
            if len(s) != 6 or s[0] not in "012" or s[1] not in "+-" \
               or s[2] not in "012" or not s[3:].isdigit():
                messagebox.showwarning(APP_TITLE, "EO 参数形如 0+0123")
                return
            def job(cat):
                r = cat.eo(int(s[0]), s[1], int(s[2]), int(s[3:]))
                self._q_show_quick("EO", f"({s}) 回包: {hexs(r) if r else '无'}")
            self.bg(job)

        def do_jp_read(self):
            """读取 JP 参数（前缀由机型页决定），并在机型页显示解析结果。"""
            if not self.require_open(): return
            def job(cat):
                r = cat.jp_read()
                info = cat.parse_jp(r)
                self._q_show_quick("JP 读", hexs(r))
                self.log(f"JP 读: {hexs(r)}")
                if hasattr(self, "txt_prof_result"):
                    msg = (f"JP 读 ({cat.jp_prefix}) -> {hexs(r)}\n"
                           f"  v={info.get('v_hex', '?')} w={info.get('w_hex', '?')} "
                           f"互补={info.get('complement', '?')} extra={info.get('extra_hex', '?')}\n")
                    self._q_append_text(self.txt_prof_result, msg)
            self.bg(job)

        def do_jp_write(self):
            """JP 写入没有经过资料确认的安全值，保持禁用。"""
            note = self._profile_cfg().get(
                "jp_write_note", "JP 写入未开放；原因见机型配置。")
            messagebox.showwarning(APP_TITLE, f"JP 写入已禁用。\n{note}")

        def do_e0(self):
            if not self.require_open(): return
            def job(cat):
                r = cat.e0()
                self._q_show_quick("E0", f"{len(r)} 字节:\n{hexs(r)}")
            self.bg(job)

        def do_e8(self):
            if not self.require_open(): return
            def job(cat):
                self.status("正在读取 E8 转储…")
                r = cat.e8()
                self._q_show_quick("E8", f"{len(r)} 字节(前 256):\n{hexs(r[:256])}")
            self.bg(job)

        def do_spard(self):
            if not self.require_open(): return
            def job(cat):
                r = cat.spa_rd()
                self._q_show_quick("SPARD", f"{len(r)} 字节(前 256):\n{hexs(r[:256])}")
            self.bg(job)

        def do_spm(self, which):
            if not self.require_open(): return
            def job(cat):
                r = cat.spm(which)
                self._q_show_quick(f"SPM {which}", hexs(r))
            self.bg(job)

        def do_spn_read(self):
            if not self.require_open(): return
            def job(cat):
                r = cat.spn_read()
                self._q_show_quick("SPN 读", hexs(r))
            self.bg(job)

        def do_spn_write(self):
            """快捷页 SPN 写项目号；会切换波段/模式，需服务模式。"""
            if not self._cap("service_menu"):
                messagebox.showwarning(APP_TITLE, "当前机型配置未启用服务菜单操作。")
                return
            if not self.require_open(): return
            try:
                n = int(self.ent_spn_write.get())
                item_count = self._cfg("service_menu", "item_count")
                if item_count is None or not 0 <= n < int(item_count):
                    raise ValueError
            except Exception:
                messagebox.showwarning(APP_TITLE, "当前机型未配置 SPN 项目范围，或 N 超出范围"); return
            if not messagebox.askyesno(
                    APP_TITLE,
                    f"发送 SPN N={n:03d}？会切换当前波段/模式，需要服务模式门控。"):
                return
            def job(cat):
                r = cat.spn_write(n)
                self._q_show_quick("SPN 写", f"N={n:03d} 回包: {hexs(r) if r else '无'}")
            self.bg(job)

        def do_spsdr(self):
            """SPSDR：读取 b0/b1 对应的 9 字节数据帧。"""
            if not self.require_open(): return
            try:
                b0 = int(self.ent_spsdr0.get(), 16)
                b1 = int(self.ent_spsdr1.get(), 16)
                if not (0 <= b0 <= 0xFF and 0 <= b1 <= 0xFF):
                    raise ValueError
            except Exception:
                messagebox.showwarning(APP_TITLE, "SPSDR 的 b0/b1 必须是 0x00–0xFF"); return
            def job(cat):
                r = cat.spsdr(b0, b1)
                self._q_show_quick("SPSDR", f"b0={b0:02X} b1={b1:02X}\n{hexs(r)}")
            self.bg(job)

        def do_spa_we(self):
            if not self.require_open(): return
            def job(cat):
                self._q_show_quick("SPAWE", hexs(cat.spa_we()))
            self.bg(job)

        def do_spa_cl(self):
            if not self.require_open(): return
            if not self._require_backup_ack(): return
            if not messagebox.askyesno(
                    APP_TITLE,
                    "SPACL 会清除设置表（破坏性）。\n"
                    "请确认已备份当前机型相关 FRAM 区域。继续？"):
                return
            def job(cat):
                self._q_show_quick("SPACL", hexs(cat.spa_cl()))
            self.bg(job)

        def do_spa_et(self):
            if not self.require_open(): return
            if not self._require_backup_ack(): return
            if not messagebox.askyesno(
                    APP_TITLE,
                    "发送 SPAET 会触发设置表应用逻辑，可能改变校准行为。继续？"):
                return
            def job(cat):
                self._q_show_quick("SPAET", hexs(cat.spa_et()))
            self.bg(job)

        # ---------------- Tab: 功率 / 频段 ----------------
        def _tab_power(self):
            """功率/频段页：功率等级、TS/EXP。"""
            f = ttk.Frame(self.nb, padding=10)
            self.nb.add(f, text="功率 / 频段")

            # --- 配置字节 ---
            box = ttk.LabelFrame(f, text="功率与频率设置")
            box.pack(fill="x", pady=4)
            row = ttk.Frame(box); row.pack(fill="x", padx=6, pady=4)
            self.btn_cfg_read = ttk.Button(row, text="读取配置", command=self.do_cfg_read)
            self.btn_cfg_read.pack(side="left", padx=4)
            self.var_cfg_raw = tk.StringVar(value="尚未读取设备设置")
            ttk.Label(row, textvariable=self.var_cfg_raw).pack(side="left", padx=10)
            self.var_cfg_ts = tk.StringVar(value="TS：--")
            self.var_cfg_pwr = tk.StringVar(value="功率等级：--")
            self.var_cfg_exp = tk.StringVar(value="扩展频段：--")
            for v in (self.var_cfg_ts, self.var_cfg_pwr, self.var_cfg_exp):
                ttk.Label(row, textvariable=v, foreground="#054").pack(side="left", padx=8)

            row2 = ttk.Frame(box); row2.pack(fill="x", padx=6, pady=4)
            ttk.Label(row2, text="功率等级：").pack(side="left")
            levels = self._cfg("power", "levels", {})
            level_values = tuple(str(label) for label in levels.keys())
            self.cmb_pwr = ttk.Combobox(row2, width=20, state="readonly", values=level_values)
            if level_values:
                self.cmb_pwr.set(level_values[0])
            self.cmb_pwr.pack(side="left", padx=4)
            self.btn_pwr_preview = ttk.Button(row2, text="预览功率等级", command=self.do_pwr_preview)
            self.btn_pwr_preview.pack(side="left", padx=4)
            self.btn_pwr_write = ttk.Button(row2, text="写入功率等级", command=self.do_pwr_write)
            self.btn_pwr_write.pack(side="left", padx=4)
            ttk.Label(row2, text="本页写入功率等级与功率上限（AC）。",
                      foreground="#555").pack(side="left", padx=8)

            row3 = ttk.Frame(box); row3.pack(fill="x", padx=6, pady=4)
            ttk.Label(row3, text="频段组合：").pack(side="left")
            ts_tables = self._cfg("power", "ts_tables", {})
            ts_values = tuple(self._ts_ui_value(i) for i in sorted(int(k) for k in ts_tables))
            self.cmb_ts = ttk.Combobox(row3, width=28, state="readonly", values=ts_values)
            if ts_values:
                self.cmb_ts.set(ts_values[0])
            else:
                self.cmb_ts.set("该机型未配置频段组合")
                self.cmb_ts.configure(state="disabled")
            self.cmb_ts.pack(side="left", padx=4)
            self.var_exp = tk.BooleanVar(value=False)
            ts_enabled = self._cap("ts_exp_write")
            self.chk_exp = ttk.Checkbutton(row3, text="EXP 扩展频段",
                                           variable=self.var_exp, state=("normal" if ts_enabled else "disabled"))
            self.chk_exp.pack(side="left", padx=8)
            self.btn_ts_preview = ttk.Button(row3, text="预览 TS/EXP", command=self.do_ts_preview,
                                              state=("normal" if ts_enabled else "disabled"))
            self.btn_ts_preview.pack(side="left", padx=4)
            self.btn_ts_write = ttk.Button(row3, text="写入 TS/EXP", command=self.do_ts_write,
                                            state=("normal" if ts_enabled else "disabled"))
            self.btn_ts_write.pack(side="left", padx=4)

            self.var_range = tk.StringVar(value="频段范围：尚未读取")
            self.var_ts_note = tk.StringVar(value="")
            self.lbl_ts_note = ttk.Label(box, textvariable=self.var_ts_note, foreground="#a00",
                                         wraplength=980, justify="left")
            self.lbl_ts_note.pack(anchor="w", padx=8, pady=(0, 3))
            ttk.Label(box, textvariable=self.var_range, foreground="#555",
                      wraplength=980, justify="left").pack(anchor="w", padx=8, pady=(0, 3))
            self.hint_box = ttk.LabelFrame(box, text="常见业余频段参考（不代表本机开放范围）")
            self.hint_text_var = tk.StringVar(value="")
            self.hint_note_var = tk.StringVar(value="")
            ttk.Label(self.hint_box, textvariable=self.hint_text_var,
                      wraplength=980, justify="left").pack(anchor="w", padx=6, pady=4)
            ttk.Label(self.hint_box, textvariable=self.hint_note_var,
                      foreground="#777", wraplength=980).pack(anchor="w", padx=6, pady=(0, 4))

            self.txt_power = self._mktext(f, 8)
            self.txt_power.pack(fill="both", expand=True, pady=(4, 0))

        def _render_cfg(self, b0: int, b1: int, raw: bytes | None = None):
            """Render config bytes using the selected profile's bit layout."""
            if threading.current_thread() is not threading.main_thread():
                self.ui(lambda: self._render_cfg(b0, b1, raw))
                return
            levels = self._cfg("power", "levels", {})
            ts_mask = int(self._cfg("power", "ts_mask", 0))
            pwr_mask = int(self._cfg('power', 'power_mask', 0))
            exp_bit = int(self._cfg("power", "exp_bit", 0))
            aux_mask = int(self._cfg("power", "aux_mask", 0))
            ts = b1 & ts_mask
            bits = b1 & pwr_mask
            exp = bool(exp_bit and b1 & exp_bit)
            addr = int(self._cfg("power", "config_pair_addr",
                                 self._cfg("fram", "config_pair_addr", 0)) or 0)
            self.cfg_pair = (b0, b1)
            self.var_cfg_raw.set(f"设备设置已读取（配置对 0x{addr:04X}）")
            if ts_mask == 0:
                self.var_cfg_ts.set("TS：未配置")
            else:
                self.var_cfg_ts.set(f"TS：{self._ts_label(ts)}")
            level_name = next((str(label) for label, value in levels.items() if int(value) == bits), "未识别功率等级")
            self.var_cfg_pwr.set(f"功率等级：{level_name}")
            self.var_cfg_exp.set(f"扩展频段：{'开启' if exp else '关闭'}")
            if self.cmb_ts.cget("state") != "disabled":
                self.cmb_ts.set(self._ts_ui_value(ts))
            self.var_exp.set(exp)
            if level_name in levels:
                self.cmb_pwr.set(level_name)
            ranges, why = effective_ranges(ts, exp, self._profile_cfg())
            range_text = fmt_ranges(ranges) if ranges else "该机型尚未配置可显示的频率范围"
            note = self._cfg("power", "range_note", "")
            self.var_range.set(f"当前频率范围参考：{range_text} MHz。{note}".strip())

        def do_cfg_read(self):
            """读取 配置字节对，解析 PWR/TS/EXP；已启用时一并读回功率上限（AC）。"""
            if not self.require_open(): return
            def job(cat):
                b0, b1, r = cat.read_config_pair()
                self.log("已读取功率与频率设置")
                self._render_cfg(b0, b1, r)
                self._q_append_text(self.txt_power, "已读取设备功率与频率设置。\n")
                if self._cap("power_ac_write") and cat.ac_write_cfg().get("items"):
                    self._read_power_limits(cat)
            self.bg(job)

        def _read_power_limits(self, cat):
            """Best-effort read of the configured power-limit (AC) items."""
            items = cat.ac_write_cfg().get("items", []) or []
            parts = []
            for item in items:
                label = str(item.get("label", ""))
                try:
                    parts.append(f"{label}={cat.ex_read(item.get('addr'))}")
                except Exception as e:  # noqa: BLE001 - report per item
                    parts.append(f"{label}=读取失败({e})")
            if parts:
                self._q_append_text(self.txt_power, "当前功率上限（AC）：" + "   ".join(parts) + "\n")

        def _need_cfg(self) -> bool:
            """预览/写入前要求先读取配置对，避免覆盖未知的 TS/EXP/AUX。"""
            if self.cfg_pair is None:
                messagebox.showwarning(APP_TITLE, "请先点“读取配置”，再做预览/写入。")
                return False
            return True

        def do_pwr_preview(self):
            """只计算 PWR 位目标值，不写设备。"""
            if not self._need_cfg(): return
            try:
                bits = pwr_bits_from_text(self.cmb_pwr.get(), self._profile_cfg())
                b0, old = self.cfg_pair
                new = (old & ~int(self._cfg('power', 'power_mask', 0))) | bits
                label = self._power_label(bits)
                self.txt_power.insert("end", f"功率等级预览：将设置为 {label}；其他设备设置保持不变。\n")
                self.txt_power.see("end")
            except Exception as e:
                messagebox.showwarning(APP_TITLE, str(e))

        def do_pwr_write(self):
            """Write the power class and, when configured, the power limits (AC)."""
            if not self.require_open() or not self._need_cfg(): return
            if not self._cap("power_class_write"):
                messagebox.showwarning(APP_TITLE, "当前机型配置未启用功率等级写入")
                return
            try:
                bits = pwr_bits_from_text(self.cmb_pwr.get(), self._profile_cfg())
                label = self._power_label(bits)
                b0, old = self.cfg_pair
                new = (old & ~int(self._cfg('power', 'power_mask', 0))) | bits
            except Exception as e:
                messagebox.showwarning(APP_TITLE, str(e)); return
            ac_on = self._cap("power_ac_write") and bool(self.cat.ac_write_cfg().get("items"))
            if not self._require_backup_ack():
                return
            extra = "\n随后按当前机型配置写入功率上限（AC）。" if ac_on else ""
            if not messagebox.askyesno(
                    APP_TITLE,
                    f"确认将功率等级设置为“{label}”？\n"
                    f"只修改功率等级位，其他设置保持不变。{extra}写入后会自动回读确认；建议先备份。"):
                return
            def job(cat):
                cat.write_config_pair(b0, new)
                b0b, b1b, r = cat.read_config_pair()
                self.log(f"功率等级已写入并回读：{self._power_label(b1b)}")
                self._render_cfg(b0b, b1b, r)
                self._q_append_text(self.txt_power, f"功率等级已写入并验证：{self._power_label(b1b)}。\n")
                if ac_on:
                    self._write_power_limits(cat, label)
            self.bg(job)

        def _write_power_limits(self, cat, class_label):
            """Write the configured power limits (AC) and report per-item readback."""
            cfg = cat.ac_write_cfg()
            try:
                results = cat.write_power_limits(class_label)
            except Exception as e:  # noqa: BLE001 - surface any transport error
                self.log(f"✗ 功率上限（AC）写入失败：{e}")
                self._q_append_text(self.txt_power, f"功率上限（AC）写入失败：{e}\n")
                return
            lines = [f"功率上限（AC）→ {class_label}："]
            mismatched = 0
            for item in results:
                back, target = item.get("readback"), int(item["target"])
                if item.get("error"):
                    status = f"（写入被拒：{item['error']}）"
                    mismatched += 1
                elif back is None:
                    status = "（未回读）"
                    mismatched += 1
                elif int(back) != target:
                    status = f"（回读 {back} ≠ 目标 {target}）"
                    mismatched += 1
                else:
                    status = f"= {back}"
                lines.append(f"  {item['label']}：{target} {status}")
            if mismatched:
                lines.append("  部分项与目标不一致。EX 上限按设备当前档位夹取："
                             "若刚改了档位，请整机断电重启后再写入。")
            else:
                note = str(cfg.get("reboot_note", "")).strip()
                if note:
                    lines.append(f"  {note}")
            self.log(f"功率上限（AC）已写入：{class_label}（{len(results)} 项，"
                     f"{len(results) - mismatched} 项一致）")
            self._q_append_text(self.txt_power, "\n".join(lines) + "\n")

        def do_ts_preview(self):
            """只计算 TS/EXP 位目标值，不写设备。"""
            if not self._cap("ts_exp_write"):
                messagebox.showwarning(APP_TITLE, "当前机型未配置可写的 TS/EXP 表")
                return
            if not self._need_cfg(): return
            try:
                ts = self._ts_index_from_ui()
                exp = bool(self.var_exp.get())
                b0, old = self.cfg_pair
                ts_mask = int(self._cfg("power", "ts_mask", 0))
                exp_bit = int(self._cfg("power", "exp_bit", 0))
                new = (old & ~(ts_mask | exp_bit)) | (ts & ts_mask) | (exp_bit if exp else 0)
                ranges, why = effective_ranges(ts, exp, self._profile_cfg())
                label = self._ts_label(ts)
                range_text = fmt_ranges(ranges) if ranges else "未提供频率范围资料"
                self.txt_power.insert("end", f"频率范围预览：{label}；{range_text} MHz；扩展频段{'开启' if exp else '关闭'}。\n")
                self.txt_power.see("end")
            except Exception as e:
                messagebox.showwarning(APP_TITLE, str(e))

        def do_ts_write(self):
            """Write TS/EXP only when the selected profile explicitly supports it."""
            if not self.require_open() or not self._need_cfg(): return
            if not self._cap("ts_exp_write"):
                messagebox.showwarning(APP_TITLE, "当前机型未配置可写的 TS/EXP 表")
                return
            try:
                ts = self._ts_index_from_ui()
                exp = bool(self.var_exp.get())
                b0, old = self.cfg_pair
                ts_mask = int(self._cfg("power", "ts_mask", 0))
                exp_bit = int(self._cfg("power", "exp_bit", 0))
                new = (old & ~(ts_mask | exp_bit)) | (ts & ts_mask) | (exp_bit if exp else 0)
                ranges, why = effective_ranges(ts, exp, self._profile_cfg())
            except Exception as e:
                messagebox.showwarning(APP_TITLE, str(e)); return
            if not self._require_backup_ack():
                return
            if not messagebox.askyesno(
                    APP_TITLE,
                    f"确认切换到“{self._ts_label(ts)}”？\n"
                    f"目标频率范围：{fmt_ranges(ranges) if ranges else '配置文件未提供'} MHz。扩展频段{'开启' if exp else '关闭'}。\n"
                    f"{self._cfg('power', 'exp_note', '注意：EXP 为临时状态，设备复位或设置版本迁移时会按 JP1006/EXP 跳线状态重新覆盖。')}\n"
                    "写入后需要按机型要求重启设备；不会进入服务模式。"):
                return
            def job(cat):
                cat.write_config_pair(b0, new)
                b0b, b1b, r = cat.read_config_pair()
                self.log("频段组合设置已写入并回读验证")
                self._render_cfg(b0b, b1b, r)
                self._q_append_text(self.txt_power, "频段组合设置已写入并验证。\n")
            self.bg(job)

        def do_service_mode(self):
            """Profile-driven service-mode entry; no unconfigured opcode is sent."""
            if not self.require_open(): return
            if not self._cap("service_mode"):
                messagebox.showwarning(APP_TITLE, "当前机型配置未启用服务模式命令")
                return
            value = self._cfg("service_mode", "enter_value")
            if value is None:
                messagebox.showwarning(APP_TITLE, "当前机型配置未提供服务模式入口值")
                return
            zz = "PE" + str(self._profile_cfg().get("service_code", "")).strip().upper() if self._profile_cfg().get("service_code") else ""
            command = f"ZZ{zz}{int(value):02X};"
            description = self._cfg("service_mode", "enter_description", "服务模式操作可能复位设备。")
            warning = self._cfg("service_mode", "reset_warning", "其他操作值可能造成设备重置。")
            if not messagebox.askyesno(APP_TITLE, f"将发送 {command}\n{description}\n{warning}\n继续？"):
                return
            def job(cat):
                cat.zz(int(value))
                self.log(f"已发送 {command}")
                self._q_append_text(self.txt_quick, f"已发送 {command}\n")
            self.bg(job)

        # ---------------- Tab: 机型 / 魔术串 ----------------
        def _tab_profile(self):
            """选择外部机型配置并编辑该配置的身份参数。"""
            f = ttk.Frame(self.nb, padding=10)
            self.nb.add(f, text="机型 / 魔术串")

            top = ttk.Frame(f); top.pack(fill="x", pady=4)
            ttk.Label(top, text="机型预设:").pack(side="left")
            self.cmb_profile = ttk.Combobox(
                top, width=40, state="readonly",
                values=self._profile_names())
            self.cmb_profile.pack(side="left", padx=6)
            if self.cmb_profile["values"]:
                self.cmb_profile.set(self.cmb_profile["values"][0])
            self.cmb_profile.bind("<<ComboboxSelected>>", lambda _e: self.load_profile_form())
            self.var_prof_status = tk.StringVar(value="")
            ttk.Label(top, textvariable=self.var_prof_status, foreground="#a60").pack(side="left", padx=8)
            ttk.Button(top, text="重新载入 JSON", command=self.reload_profiles).pack(side="left", padx=4)
            ttk.Button(top, text="保存当前配置到 JSON", command=self.save_profile_form).pack(side="left", padx=4)
            ttk.Button(top, text="手动输入 / 新建", command=self.new_manual_profile).pack(side="left", padx=4)

            box = ttk.LabelFrame(f, text="魔术串 / 机型参数（可直接覆盖预设并保存）")
            box.pack(fill="x", pady=6)
            row = ttk.Frame(box); row.pack(fill="x", padx=6, pady=4)
            ttk.Label(row, text="显示名称:").grid(row=0, column=0, sticky="e")
            self.ent_prof_name = ttk.Entry(row, width=34)
            self.ent_prof_name.grid(row=0, column=1, sticky="w", padx=4)
            ttk.Label(row, text="CAT ID:").grid(row=0, column=2, sticky="e", padx=(12, 0))
            self.ent_prof_id = ttk.Entry(row, width=10)
            self.ent_prof_id.grid(row=0, column=3, sticky="w", padx=4)

            ttk.Label(row, text="VE 魔术串 (7 ASCII；可粘贴 VERAH...):").grid(
                row=1, column=0, sticky="e", pady=4)
            self.ent_prof_ve = ttk.Entry(row, width=18)
            self.ent_prof_ve.grid(row=1, column=1, sticky="w", padx=4)
            ttk.Label(row, text="JP / ZZ 共用机型代码 (4 位十六进制):").grid(row=1, column=2, sticky="e", padx=(12, 0))
            self.ent_prof_service_code = ttk.Entry(row, width=12)
            self.ent_prof_service_code.grid(row=1, column=3, sticky="w", padx=4)
            ttk.Label(row, text="JP 与 ZZ 使用同一机型代码；程序自动生成 JP{代码} 和 ZZPE{代码}。JP 写入数据值不是机型代码；存在未验证副作用或未确认安全值时，写入保持禁用。",
                      foreground="#a00", wraplength=850).grid(row=2, column=0, columnspan=4, sticky="w", pady=(4, 2))

            note = ttk.Label(f, foreground="#555", justify="left", text=(
                "所有机型参数均来自 profiles/*.json；程序不内置机型魔术串或地址表。\n"
                "配置状态与证据以当前机型 JSON 为准。空值/未验证字段不会自动推断。\n"
                "保存配置只更新所选机型文件；添加机型请复制配置模板并填写已确认字段。"))
            note.pack(anchor="w", pady=(0, 4))

            btns = ttk.Frame(f); btns.pack(fill="x", pady=4)
            ttk.Button(btns, text="应用魔术串", command=self.apply_profile_magic).pack(side="left", padx=4)
            ttk.Button(btns, text="读取 ID", command=self.do_id_read).pack(side="left", padx=4)
            ttk.Button(btns, text="读取 VE 版本", command=self.do_ve).pack(side="left", padx=4)
            ttk.Button(btns, text="JP 读取", command=self.do_jp_read).pack(side="left", padx=4)
            self.btn_prof_jp_write = ttk.Button(btns, text="JP 写入（未开放）", command=self.do_jp_write)
            self.btn_prof_jp_write.pack(side="left", padx=4)
            self.btn_prof_jp_write.configure(state="disabled")
            self.lbl_magic_state = ttk.Label(
                btns,
                text=f"当前: VE={self.cat.ve_magic} / JP={self.cat.jp_prefix} / ZZ={self.cat.zz_prefix}",
                foreground="#054")
            self.lbl_magic_state.pack(side="left", padx=12)

            ttk.Label(f, text="备注 / 实机观察:").pack(anchor="w", pady=(6, 0))
            self.txt_prof_notes = self._mktext(f, 5)
            self.txt_prof_notes.pack(fill="x", pady=(2, 4))
            ttk.Label(f, text="回包 / 解析:").pack(anchor="w")
            self.txt_prof_result = self._mktext(f, 8)
            self.txt_prof_result.pack(fill="both", expand=True, pady=(2, 0))

            self.load_profile_form()

        def _profile_names(self):
            """下拉框名称；最后一项是手动输入模式。"""
            names = [p.get("name", p.get("id", "?"))
                     for p in self.profile_data.get("profiles", [])]
            return names + [MANUAL_PROFILE_NAME]

        def _profile_by_id(self, pid):
            """按 id 查预设。"""
            for p in self.profile_data.get("profiles", []):
                if p.get("id") == pid:
                    return p
            return None

        def _selected_profile(self):
            """Return the selected external profile, or the first profile during startup."""
            if not hasattr(self, "cmb_profile"):
                profiles = self.profile_data.get("profiles", [])
                return profiles[0] if profiles else None
            name = self.cmb_profile.get()
            if name == MANUAL_PROFILE_NAME:
                return None
            for p in self.profile_data.get("profiles", []):
                if p.get("name", p.get("id")) == name:
                    return p
            return None

        def _refresh_model_banner(self):
            p = self._selected_profile() if hasattr(self, "cmb_profile") else None
            model = p.get("name", p.get("id", "自定义")) if p else "自定义 / 手动配置"
            status = p.get("status", "未验证") if p else "未验证"
            link = (f"串口：{self.link.port} @ {self.link.baud} {self.link.bytesize}{self.link.parity}{self.link.stopbits} / {self.link.flow_control_mode}" if self.link.is_open else "串口：未连接")
            identity = (f"设备 ID：{getattr(self, 'detected_device_id', '未查询')}"
                        f"  |  VE：{getattr(self, 'detected_version', '未查询')[:32]}")
            if hasattr(self, "var_model_info"):
                self.var_model_info.set(f"机型：{model}  |  {status}  |  {link}  |  {identity}")

        def _apply_model_tab_gating(self):
            """Enable only operations declared by the selected external profile."""
            if not hasattr(self, "nb"):
                return
            profile = self._profile_cfg()
            caps = profile.get("capabilities", {})
            tab_capability = {
                "高级维护": "quick_commands",
                "功率 / 频段": None,
                "FRAM / 表": "fram_raw_access",
                "FRAM 全表": "fram_full_read",
                "服务菜单": "service_menu",
                "校准 / PE": "settings_table",
                "PE 校准": "pe_calibration",
                "备份与恢复": None,
            }
            for tab_id in self.nb.tabs():
                label = self.nb.tab(tab_id, "text")
                cap = tab_capability.get(label)
                if label == "功率 / 频段":
                    enabled = any(bool(caps.get(k, False)) for k in (
                        "power_config_read", "power_class_write"))
                elif label == "备份与恢复":
                    enabled = bool(caps.get("full_backup", False) or caps.get("fram_full_read", False))
                else:
                    enabled = True if cap is None else bool(caps.get(cap, False))
                try:
                    self.nb.tab(tab_id, state=("normal" if enabled else "disabled"))
                except tk.TclError:
                    pass

            button_caps = [
                ("btn_fd_read", "fd_read"), ("btn_fd_write", "fd_write"),
                ("btn_kt_read", "kt_read"), ("btn_kt_write", "kt_write"),
                ("btn_kt_clear", "kt_write"), ("btn_eo", "eo"),
                ("btn_ek", "ek"), ("btn_spsdr", "spsdr"),
                ("btn_zz", "service_mode"), ("btn_spn_write", "service_menu"),
                ("btn_jp_write", "jp_write"), ("btn_spard", "spa_rd"),
                ("btn_spawe", "spa_we"), ("btn_spacl", "spa_cl"),
                ("btn_spaet", "spa_et"), ("btn_spn_read", "service_mode"),
                ("btn_fram_dump_write", "fram_raw_write"),
                ("btn_pe_all_cal", "pe_calibration"),
            ]
            for attr, cap_name in button_caps:
                if hasattr(self, attr):
                    getattr(self, attr).configure(state=("normal" if caps.get(cap_name, False) else "disabled"))
            # 输入框随对应命令能力禁用，避免出现无法使用的“空壳”输入框。
            entry_caps = [
                ("ent_fd", "fd_write"), ("ent_kt", "kt_write"),
                ("ent_eo", "eo"), ("ent_spn_write", "service_menu"),
                ("ent_spsdr0", "spsdr"), ("ent_spsdr1", "spsdr"),
            ]
            for attr, cap_name in entry_caps:
                if hasattr(self, attr):
                    getattr(self, attr).configure(state=("normal" if caps.get(cap_name, False) else "disabled"))
            if hasattr(self, "cmb_fd"):
                self.cmb_fd.configure(state=("readonly" if caps.get("fd_read", False) else "disabled"))
            response_buttons = [
                ("btn_e0", "dumps", "e0_length"),
                ("btn_e8", "dumps", "e8_length"),
                ("btn_spm_tr", "responses", "spm_tr_length"),
                ("btn_spm_t2", "responses", "spm_t2_length"),
                ("btn_spsdr", "responses", "spsdr_length"),
            ]
            for attr, section, key in response_buttons:
                if hasattr(self, attr):
                    value = get_rig_value(profile, section, key)
                    enabled = isinstance(value, int) and value > 0
                    getattr(self, attr).configure(state=("normal" if enabled else "disabled"))

            for attr, cap_name in (
                ("btn_cfg_read", "power_config_read"),
                ("btn_pwr_preview", "power_config_read"),
                ("btn_pwr_write", "power_class_write"),
                ("chk_exp", "ts_exp_write"),
                ("btn_ts_preview", "ts_exp_write"),
                ("btn_ts_write", "ts_exp_write"),
            ):
                if hasattr(self, attr):
                    getattr(self, attr).configure(state=("normal" if caps.get(cap_name, False) else "disabled"))
            if hasattr(self, "bk_widgets"):
                support = {
                    "ve": bool(profile.get("ve_magic")),
                    "fd": bool(caps.get("fd_read", False)),
                    "kt": bool(caps.get("kt_read", False)),
                    "pe": bool(caps.get("pe_calibration", False)),
                    "table": bool(caps.get("settings_table", False)),
                    "spard": bool(caps.get("spa_rd", False)),
                    "e0": bool(get_rig_value(profile, "dumps", "e0_length")),
                    "e8": bool(get_rig_value(profile, "dumps", "e8_length")),
                    "jp": bool(profile.get("service_code")),
                    "mem": bool(caps.get("fram_full_read", False)),
                }
                for key, supported in support.items():
                    if key in self.bk_widgets:
                        self.bk_widgets[key].configure(state=("normal" if supported else "disabled"))
                        if not supported:
                            self.bk[key].set(False)
            if hasattr(self, "btn_restore_table"):
                self.btn_restore_table.configure(state=("normal" if caps.get("settings_table", False) else "disabled"))
            if hasattr(self, "btn_restore_fram"):
                self.btn_restore_fram.configure(state=("normal" if caps.get("fram_raw_write", False) else "disabled"))
            if hasattr(self, "var_model_info"):
                self._refresh_model_banner()

        def _refresh_profile_dependent_widgets(self):
            """Refresh model-dependent labels, tables and ranges after profile changes."""
            profile = self._profile_cfg()
            if hasattr(self, "lbl_magic_state"):
                self.lbl_magic_state.configure(
                    text=f"当前: VE={self.cat.ve_magic} / JP={self.cat.jp_prefix} / ZZ={self.cat.zz_prefix}")
            levels = get_rig_value(profile, "power", "levels", {}) or {}
            if hasattr(self, "cmb_pwr"):
                values = tuple(str(label) for label in levels.keys())
                self.cmb_pwr.configure(values=values)
                self.cmb_pwr.set(values[0] if values else "")
            if hasattr(self, "cmb_ts"):
                ts_tables = get_rig_value(profile, "power", "ts_tables", {}) or {}
                values = tuple(self._ts_ui_value(i) for i in sorted(int(k) for k in ts_tables))
                self.cmb_ts.configure(values=values)
                if values:
                    self.cmb_ts.configure(state="readonly")
                    self.cmb_ts.set(values[0])
                else:
                    self.cmb_ts.configure(state="disabled")
                    self.cmb_ts.set("该机型未配置频段组合")
            if hasattr(self, "var_ts_note"):
                notes = []
                if get_rig_value(profile, "capabilities", "ts_exp_write", False):
                    exp_note = get_rig_value(profile, "power", "exp_note", "") or ""
                    ts_note = get_rig_value(profile, "power", "ts_note", "") or ""
                    if exp_note:
                        notes.append(exp_note)
                    if ts_note:
                        notes.append(ts_note)
                self.var_ts_note.set("\n".join(notes))
            if hasattr(self, "hint_box"):
                hints = get_rig_value(profile, "power", "band_frequency_hints", []) or []
                if hints:
                    self.hint_text_var.set("　 ·  ".join(
                        f"{item.get('name', '频段')}：{item.get('range_mhz', '')} MHz"
                        for item in hints))
                    self.hint_note_var.set(
                        get_rig_value(profile, "power", "band_frequency_hint_note", "") or "")
                    self.hint_box.pack(fill="x", padx=6, pady=(0, 6))
                else:
                    self.hint_text_var.set("")
                    self.hint_note_var.set("")
                    self.hint_box.pack_forget()
            if hasattr(self, "btn_e0"):
                self.btn_e0.configure(text=f"E0 读取 ({get_rig_value(profile, 'dumps', 'e0_length') or '未配置'})")
            if hasattr(self, "btn_e8"):
                self.btn_e8.configure(text=f"E8 读取 ({get_rig_value(profile, 'dumps', 'e8_length') or '未配置'})")
            if hasattr(self, "btn_spard"):
                self.btn_spard.configure(text=f"SPARD 读取 ({get_rig_value(profile, 'settings_table', 'length') or '未配置'}B)")
            if hasattr(self, "bk_widgets"):
                self.bk_widgets["table"].configure(
                    text=f"设置/校准表({get_rig_value(profile, 'settings_table', 'length') or '未配置'}B)")
                self.bk_widgets["e0"].configure(
                    text=f"E0 ({get_rig_value(profile, 'dumps', 'e0_length') or '未配置'}B)")
                self.bk_widgets["e8"].configure(
                    text=f"E8 ({get_rig_value(profile, 'dumps', 'e8_length') or '未配置'}B)")

            lo = get_rig_value(profile, "fram", "address_min")
            hi = get_rig_value(profile, "fram", "address_max")
            if lo is not None and hasattr(self, "ent_fram_s"):
                self.ent_fram_s.delete(0, "end"); self.ent_fram_s.insert(0, f"{int(lo):04X}")
                self.ent_fram_e.delete(0, "end")
                self.ent_fram_e.insert(0, f"{int(hi):04X}" if hi is not None else f"{int(lo):04X}")
            if lo is not None:
                range_hi = int(hi) if hi is not None else int(lo)
                if hasattr(self, "var_fram_addr_label"):
                    self.var_fram_addr_label.set(
                        f"外部 FRAM 地址 (SPW/SPR, 0x{int(lo):04X}-0x{range_hi:04X}):")
                if hasattr(self, "var_fram_range_label"):
                    self.var_fram_range_label.set(
                        f"地址为十六进制 · 当前机型完整范围 0x{int(lo):04X}–0x{range_hi:04X}"
                        f"（{max(0, range_hi - int(lo) + 1):,} 字节）")
            if lo is not None and hasattr(self, "ent_mem_s"):
                self.ent_mem_s.delete(0, "end"); self.ent_mem_s.insert(0, f"{int(lo):04X}")
                self.ent_mem_e.delete(0, "end")
                self.ent_mem_e.insert(0, f"{int(hi):04X}" if hi is not None else f"{int(lo):04X}")

            if hasattr(self, "fram_tree"):
                for iid in self.fram_tree.get_children():
                    self.fram_tree.delete(iid)
                labels = get_rig_value(profile, "fram", "region_labels", {}) or {}
                for row in self.fram_regions:
                    inclusive_end = max(row["start_i"], row["end_i"] - 1)
                    self.fram_tree.insert(
                        "", "end", iid=f"r{row['start_i']:04X}",
                        values=(row["start"], f"0x{inclusive_end:04X}", row["size"],
                                row.get("purpose") or labels.get(row["name"], row["name"]),
                                row.get("confidence", "")))
            if hasattr(self, "lbl_jp_write"):
                jp_status = profile.get("jp_write_status", "")
                jp_note = profile.get("jp_write_note", "") or "JP 写入未开放。"
                self.lbl_jp_write.configure(text=jp_note)
                self.btn_jp_write.configure(
                    text=("JP 写入（不支持）" if jp_status == "unsupported"
                          else "JP 写入（未验证，禁用）"))
                if hasattr(self, "btn_prof_jp_write"):
                    self.btn_prof_jp_write.configure(
                        text=("JP 写入（不支持）" if jp_status == "unsupported"
                              else "JP 写入（未验证，禁用）"),
                        state="disabled")
            if hasattr(self, "sm_tree"):
                for iid in self.sm_tree.get_children():
                    self.sm_tree.delete(iid)
                for item in self.service_items:
                    self.sm_tree.insert(
                        "", "end", iid=f"sm{item['n']}",
                        values=(f"{item['n']:03d} {item.get('code_hex', '')}",
                                item.get("label", ""), ("—" if item.get("band_mhz_f") is None else f"{item['band_mhz_f']:g}"),
                                item.get("item_fram", ""), item.get("apply_fram", ""), "", ""))
            if hasattr(self, "pe_tree"):
                for iid in self.pe_tree.get_children():
                    self.pe_tree.delete(iid)
                positions = get_rig_value(profile, "pe_calibration", "selector_positions", None)
                if positions:
                    pos = [[str(v) for v in p] for p in positions]
                else:
                    sel = [str(v) for v in (get_rig_value(profile, "pe_calibration", "selectors", []) or [])]
                    pos = [sel, sel, sel]
                for s0 in pos[0]:
                    for s1 in pos[1]:
                        for mode in pos[2]:
                            sel = f"{s0}{s1}{mode}"
                            self.pe_tree.insert("", "end", iid=f"pe{sel}",
                                                values=(sel, self._pe_desc(sel), "", ""))
            if hasattr(self, "ent_taddr"):
                table_start = get_rig_value(profile, "settings_table", "start_addr")
                self.ent_taddr.delete(0, "end")
                if table_start is not None:
                    self.ent_taddr.insert(0, f"{int(table_start):04X}")
            if hasattr(self, "var_cal_table"):
                self.var_cal_table.set(
                    f"设置/校准表 ({get_rig_value(profile, 'settings_table', 'length') or '未配置'} 字节):")
            if hasattr(self, "var_pe_raw_label"):
                self.var_pe_raw_label.set(
                    f"PE 参数 ({self._pe_addr_text()}; 选择子由配置定义) + 原始写:")
            if hasattr(self, "var_pe_note"):
                pe_note = (get_rig_value(profile, "pe_calibration", "note", "")
                           or "选择子/位域尚未完全解码，本页只做原始读写。")
                self.var_pe_note.set(
                    f"PE 参数地址：{self._pe_addr_text()}；{pe_note}\n"
                    "写入前必须记录当前回包；没有真机验证时不要批量改。值字段为 3 字符：LEVEL 用 +05/-03，"
                    "FREQ/BWTH 用 010/002；程序发送 PE<选择子><值>;。\n"
                    "PE 写会直接改校准并可能写总线，风险高；请先备份当前机型相关 FRAM 区域。")

            self.cfg_pair = None
            self.cal_table = b""
            self.last_backup = None
            if hasattr(self, "var_exp"):
                self.var_exp.set(False)
            if hasattr(self, "var_cfg_raw"):
                self.var_cfg_raw.set("尚未读取设备设置")
            if hasattr(self, "var_range"):
                self.var_range.set("频段范围: 未读取")
            if hasattr(self, "var_backup_ack"):
                self.var_backup_ack.set(False)
            for widget_name in ("txt_fram", "txt_cal"):
                widget = getattr(self, widget_name, None)
                if widget is not None:
                    widget.delete("1.0", "end")
                    widget.insert("1.0", "# 已切换机型配置；请重新读取数据后再编辑。\n")
            if hasattr(self, "txt_bk"):
                self.txt_bk.delete("1.0", "end")
                self.txt_bk.insert("1.0", "已切换机型配置；请重新载入该机型的备份。\n")

        def _new_custom_id(self):
            """生成不会与现有预设冲突的手动配置 id。"""
            stamp = time.strftime("%Y%m%d_%H%M%S")
            base = f"custom_{stamp}"
            ids = {p.get("id") for p in self.profile_data.get("profiles", [])}
            cid, n = base, 2
            while cid in ids:
                cid = f"{base}_{n}"
                n += 1
            return cid

        def _clear_profile_form(self):
            """清空编辑框，进入手动输入模式。"""
            for ent in (self.ent_prof_name, self.ent_prof_id, self.ent_prof_ve,
                        self.ent_prof_service_code):
                ent.delete(0, "end")
            self.ent_prof_name.insert(0, "自定义 / 手动输入")
            self.txt_prof_notes.delete("1.0", "end")
            self.txt_prof_notes.insert(
                "1.0", "手动输入模式：填写魔术串后点“应用魔术串”；点“保存当前配置到 JSON”会新建预设。")
            self.var_prof_status.set("手动输入 / 未验证")

        def new_manual_profile(self):
            """新建手动输入配置；不覆盖已有预设。"""
            self.current_profile_id = self._new_custom_id()
            self.cmb_profile.set(MANUAL_PROFILE_NAME)
            self.active_profile = {"id": self.current_profile_id, "name": "Custom / manual"}
            self.cat.set_profile(self.active_profile)
            self.fram_regions = []
            self.service_items = []
            self._clear_profile_form()
            self._refresh_profile_dependent_widgets()
            self._apply_model_tab_gating()
            self.log(f"已切换到手动输入模式（保存后 id={self.current_profile_id}）")

        def load_profile_form(self):
            """把当前下拉预设填入编辑框；选中“自定义”时清空。"""
            if self.cmb_profile.get() == MANUAL_PROFILE_NAME:
                self.new_manual_profile()
                return
            p = self._selected_profile()
            if not p:
                return
            self.current_profile_id = p.get("id", self.current_profile_id)
            self.active_profile = dict(p)
            self.cat.set_profile(p)
            self.fram_regions = load_fram_regions(p)
            self.service_items = load_service_items(p)
            if hasattr(self, "cmb_baud"):
                configured_baud = get_rig_value(p, "serial", "baud_default")
                self.cmb_baud.set(str(configured_baud) if configured_baud in (4800, 9600, 19200, 38400, 57600, 115200) else "")
                configured_stopbits = get_rig_value(p, "serial", "stop_bits")
                self.cmb_stopbits.set(str(configured_stopbits) if configured_stopbits in (1, 2) else "")
            for ent, key, default in (
                (self.ent_prof_name, "name", ""),
                (self.ent_prof_id, "cat_id", ""),
                (self.ent_prof_ve, "ve_magic", ""),
                (self.ent_prof_service_code, "service_code", ""),
            ):
                ent.delete(0, "end")
                ent.insert(0, str(p.get(key, default)))
            self.txt_prof_notes.delete("1.0", "end")
            self.txt_prof_notes.insert("1.0", p.get("notes", ""))
            status = p.get("status", "未标注证据状态")
            ms = p.get("magic_status", "")
            if ms and ms not in status:
                status = f"{status}；魔术串： {ms}"
            self.var_prof_status.set(status)
            self._refresh_profile_dependent_widgets()
            self._refresh_model_banner()
            self._apply_model_tab_gating()

        def reload_profiles(self):
            """从 rig_profiles.json 重新载入。"""
            self.profile_data = load_rig_profiles()
            names = self._profile_names()
            self.cmb_profile["values"] = names
            if names:
                self.cmb_profile.set(names[0])
            self.load_profile_form()
            self.log(f"已重新载入 {PROFILES_PATH.name}")

        def _form_profile(self) -> dict:
            """收集编辑框内容并做基本校验，返回可写 JSON 的预设字典。"""
            name = self.ent_prof_name.get().strip()
            cat_id = self.ent_prof_id.get().strip().upper()
            ve = self.ent_prof_ve.get().strip().upper()
            if len(ve) == 9 and ve.startswith("VE"):
                ve = ve[2:]
            service_code = self.ent_prof_service_code.get().strip().upper()
            if not name:
                raise ValueError("显示名称不能为空")
            if ve and (len(ve) != 7 or not ve.isascii() or ";" in ve):
                raise ValueError("VE 魔术串如有填写，必须是 7 个 ASCII 字符")
            if service_code and (len(service_code) != 4 or any(c not in "0123456789ABCDEF" for c in service_code)):
                raise ValueError("JP/ZZ 共用机型代码必须是 4 位十六进制字符，如 0010 或 0891")
            orig = self._selected_profile() or {}
            pid = self.current_profile_id or re.sub(r"\W+", "_", name.lower())
            magic_status = orig.get("magic_status") or "手动输入 / 未验证"
            magic_evidence = orig.get("magic_evidence") or "用户手动输入；未在固件/真机验证。"
            updated = copy.deepcopy(orig)
            updated.pop("jp_prefix", None)
            updated.pop("zz_prefix", None)
            updated.pop("jp_magic", None)
            updated.update({
                "id": pid,
                "name": name,
                "cat_id": cat_id,
                "ve_magic": ve,
                "ve_full": ("VE" + ve) if ve else "",
                "service_code": service_code,
                "status": self.var_prof_status.get().strip() or "未标注证据状态",
                "notes": self.txt_prof_notes.get("1.0", "end").strip(),
                "magic_status": magic_status,
                "magic_evidence": magic_evidence,
            })
            return updated

        def save_profile_form(self):
            """把机型与魔术串一起写回 rig_profiles.json。"""
            try:
                p = self._form_profile()
                profiles = self.profile_data.setdefault("profiles", [])
                for i, old in enumerate(profiles):
                    if old.get("id") == p["id"]:
                        profiles[i] = p
                        break
                else:
                    profiles.append(p)
                save_rig_profiles(self.profile_data)
                self.cmb_profile["values"] = self._profile_names()
                self.cmb_profile.set(p["name"])
                self.current_profile_id = p["id"]
                self.active_profile = dict(p)
                self.cat.set_profile(p)
                self._refresh_profile_dependent_widgets()
                self._refresh_model_banner()
                self._apply_model_tab_gating()
                self.log(f"已保存独立机型配置 -> profiles/{p['id']}.json: {p['name']}")
            except Exception as e:
                messagebox.showerror(APP_TITLE, f"保存失败: {e}")

        def apply_profile_magic(self):
            """校验并把 VE/JP/ZZ 魔术串应用到当前 HiddenCat 实例。"""
            try:
                ve = self.ent_prof_ve.get().strip()
                code = self.ent_prof_service_code.get().strip().upper()
                if code and (len(code) != 4 or any(c not in "0123456789ABCDEF" for c in code)):
                    raise ValueError("JP/ZZ 共用机型代码必须为 4 位十六进制字符")
                profile = dict(self._profile_cfg())
                profile.pop("jp_prefix", None)
                profile.pop("zz_prefix", None)
                profile.pop("jp_magic", None)
                profile.update({"ve_magic": ve, "service_code": code})
                self.active_profile = profile
                self.cat.set_profile(profile)
                self.cat.set_magic(ve or None, ("JP" + code) if code else None, ("PE" + code) if code else None)
                self._refresh_profile_dependent_widgets()
                self._apply_model_tab_gating()
                self.lbl_magic_state.configure(
                    text=f"当前: VE={self.cat.ve_magic} / JP={self.cat.jp_prefix} / ZZ={self.cat.zz_prefix}")
                self.log(f"已应用魔术串: VE={self.cat.ve_magic} / JP={self.cat.jp_prefix} / ZZ={self.cat.zz_prefix}")
            except Exception as e:
                messagebox.showwarning(APP_TITLE, str(e))

        def do_id_read(self):
            """标准 ID; 查询，用于确认下拉预设是否匹配实机。"""
            if not self.require_open(): return
            want = self.ent_prof_id.get().strip().upper()
            def job(cat):
                r = cat.id_read()
                txt = r.decode("latin-1", "replace")
                self.log(f"ID: {hexs(r)} / {txt}")
                warn = ""
                if want and want not in txt.upper():
                    warn = f"  ⚠ 与预设 CAT ID {want} 不一致，请确认机型/魔术串"
                    self.log(warn)
                self._q_append_text(self.txt_prof_result, f"ID -> {hexs(r)}  {txt}{warn}\n")
            self.bg(job)

        # ---------------- Tab: 外部 FRAM / 设置表 ----------------
        def _tab_reg(self):
            f = ttk.Frame(self.nb, padding=10)
            self.nb.add(f, text="FRAM / 表")
            g = ttk.Frame(f); g.pack(fill="x")

            self.var_fram_addr_label = tk.StringVar(
                value=f"外部 FRAM 地址 (SPW/SPR, 0x{int(self._profile_memory('address_min', 0)):04X}-0x{int(self._profile_memory('address_max', 0)):04X}):")
            ttk.Label(g, textvariable=self.var_fram_addr_label).grid(
                row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))
            ttk.Label(g, text="地址(hex):").grid(row=1, column=0, sticky="e")
            self.ent_addr = ttk.Entry(g, width=10); self.ent_addr.insert(0, "0100")
            self.ent_addr.grid(row=1, column=1, sticky="w")
            ttk.Label(g, text="数据(2字节 hex):").grid(row=1, column=2, sticky="e")
            self.ent_data = ttk.Entry(g, width=12); self.ent_data.insert(0, "0000")
            self.ent_data.grid(row=1, column=3, sticky="w")
            ttk.Button(g, text="读 2 字节", command=self.do_reg_read).grid(row=1, column=4, padx=6)
            ttk.Button(g, text="写 2 字节", command=self.do_reg_write).grid(row=1, column=5, padx=6)

            ttk.Separator(f, orient="horizontal").pack(fill="x", pady=8)

            ttk.Label(f, text="设置/校准表 (地址范围由当前机型配置提供):").pack(anchor="w")
            g2 = ttk.Frame(f); g2.pack(fill="x", pady=4)
            ttk.Label(g2, text="表地址(hex):").pack(side="left")
            self.ent_taddr = ttk.Entry(g2, width=10)
            table_start = self._cfg("settings_table", "start_addr")
            if table_start is not None:
                self.ent_taddr.insert(0, f"{int(table_start):04X}")
            self.ent_taddr.pack(side="left", padx=4)
            ttk.Label(g2, text="数据(1字节 hex):").pack(side="left")
            self.ent_tdata = ttk.Entry(g2, width=8); self.ent_tdata.insert(0, "00")
            self.ent_tdata.pack(side="left", padx=4)
            ttk.Button(g2, text="读 1 字节", command=self.do_tab_read).pack(side="left", padx=6)
            ttk.Button(g2, text="写 1 字节", command=self.do_tab_write).pack(side="left", padx=6)
            ttk.Button(g2, text="读内部状态", command=self.do_2001).pack(side="left", padx=6)

            ttk.Separator(f, orient="horizontal").pack(fill="x", pady=8)

            ttk.Label(f, text="范围导出 (SPR 逐 2 字节):").pack(anchor="w")
            g3 = ttk.Frame(f); g3.pack(fill="x", pady=4)
            ttk.Label(g3, text="起(hex):").pack(side="left")
            self.ent_rng_s = ttk.Entry(g3, width=8); self.ent_rng_s.insert(0, "0000"); self.ent_rng_s.pack(side="left", padx=4)
            ttk.Label(g3, text="止(hex):").pack(side="left")
            self.ent_rng_e = ttk.Entry(g3, width=8); self.ent_rng_e.insert(0, "00FF"); self.ent_rng_e.pack(side="left", padx=4)
            ttk.Button(g3, text="导出到文件", command=self.do_range_export).pack(side="left", padx=6)

            self.txt_reg = self._mktext(f, 16)
            self.txt_reg.pack(fill="both", expand=True, pady=(8, 0))

        def _pv(self, s, name):
            try:
                return int(s.strip(), 16)
            except ValueError:
                raise ValueError(f"{name} 不是合法十六进制: {s!r}")

        def do_reg_read(self):
            if not self.require_open(): return
            try: addr = self._pv(self.ent_addr.get(), "地址")
            except ValueError as e:
                messagebox.showwarning(APP_TITLE, str(e)); return
            def job(cat):
                r = cat.spr(addr)
                self._q_append_text(self.txt_reg, f"SPR {addr:04X} -> {r[5]:02X} {r[6]:02X}   ({hexs(r)})\n")
            self.bg(job)

        def do_reg_write(self):
            if not self.require_open(): return
            try:
                addr = self._pv(self.ent_addr.get(), "地址")
                d = bytes.fromhex(self.ent_data.get().replace(" ", ""))
                assert len(d) == 2
            except Exception as e:
                messagebox.showwarning(APP_TITLE, f"参数错误: {e}"); return
            if not self._require_backup_ack():
                return
            if not messagebox.askyesno(APP_TITLE, f"确认写外部 FRAM 地址 0x{addr:04X} = {d.hex()}？"):
                return
            def job(cat):
                r = cat.spw(addr, d[0], d[1])
                self._q_append_text(self.txt_reg, f"SPW {addr:04X} <- {d.hex()}  回包 {hexs(r)}\n")
            self.bg(job)

        def do_tab_read(self):
            if not self.require_open(): return
            try: addr = self._pv(self.ent_taddr.get(), "表地址")
            except ValueError as e:
                messagebox.showwarning(APP_TITLE, str(e)); return
            def job(cat):
                r = cat.spr_table(addr)
                self._q_append_text(self.txt_reg, f"SPr {addr:04X} -> {r[6]:02X}   ({hexs(r)})\n")
            self.bg(job)

        def do_tab_write(self):
            if not self.require_open(): return
            try:
                addr = self._pv(self.ent_taddr.get(), "表地址")
                d = int(self.ent_tdata.get().strip(), 16)
                assert 0 <= d <= 0xFF
            except Exception as e:
                messagebox.showwarning(APP_TITLE, f"参数错误: {e}"); return
            if not self._require_backup_ack():
                return
            if not messagebox.askyesno(APP_TITLE,
                                       f"确认写设置表 0x{addr:04X} = 0x{d:02X}？可能影响校准。"):
                return
            def job(cat):
                r = cat.spw_table(addr, d)
                self._q_append_text(self.txt_reg, f"SPw {addr:04X} <- {d:02X}  回包 {hexs(r)}\n")
            self.bg(job)

        def do_2001(self):
            if not self.require_open(): return
            def job(cat):
                r = cat.spr_table(0xFFFF)
                self._q_append_text(self.txt_reg, f"SPr FFFF -> {r[6]:02X}   ({hexs(r)})\n")
            self.bg(job)

        def do_range_export(self):
            if not self.require_open(): return
            try:
                s = self._pv(self.ent_rng_s.get(), "起始")
                e = self._pv(self.ent_rng_e.get(), "结束")
            except ValueError as ex:
                messagebox.showwarning(APP_TITLE, str(ex)); return
            if e < s:
                messagebox.showwarning(APP_TITLE, "结束地址小于起始"); return
            path = filedialog.asksaveasfilename(defaultextension=".bin",
                                                filetypes=[("二进制", "*.bin"), ("全部", "*.*")])
            if not path:
                return
            def job(cat):
                total = (e - s + 1 + 1) // 2
                def prog(a, b):
                    self.status(f"导出 {a}/{b}  …")
                data = cat.read_fram(s, e, progress=prog, cancel=self.cancel, strict=True)
                save_bytes(path, data)
                self._q_append_text(self.txt_reg, f"已导出 0x{s:04X}-0x{e:04X} ({len(data)}B) -> {path}\n")
                self.log(f"范围导出完成: {path}")
            self.bg(job)

        # ---------------- Tab: 完整 FRAM 编辑器 ----------------
        def _tab_fram_full(self):
            """FRAM 全表页：区域说明 + 十六进制编辑 + 整片/区间读写。"""
            f = ttk.Frame(self.nb, padding=10)
            self.nb.add(f, text="FRAM 全表")

            top = ttk.Frame(f); top.pack(fill="x", pady=3)
            ttk.Button(top, text="读取区间", command=self.do_fram_read_range).pack(side="left", padx=3)
            self.btn_fram_dump_write = ttk.Button(top, text="写入编辑框", command=self.do_fram_write_dump)
            self.btn_fram_dump_write.pack(side="left", padx=3)
            ttk.Button(top, text="读取全部 FRAM", command=self.do_fram_read_all).pack(side="left", padx=3)
            ttk.Button(top, text="备份完整 FRAM", command=self.do_quick_backup).pack(side="left", padx=3)
            ttk.Button(top, text="导出区间 .bin", command=self.do_fram_export_bin).pack(side="left", padx=3)
            ttk.Checkbutton(top, text="我已备份相关 FRAM 区域", variable=self.var_backup_ack).pack(side="left", padx=8)
            ttk.Button(top, text="导入 .bin 到编辑框", command=self.do_fram_import_bin).pack(side="left", padx=3)

            r = ttk.Frame(f); r.pack(fill="x", pady=3)
            ttk.Label(r, text="起(hex):").pack(side="left")
            self.ent_fram_s = ttk.Entry(r, width=8); self.ent_fram_s.insert(0, f"{int(self._profile_memory('address_min', 0)):04X}")
            self.ent_fram_s.pack(side="left", padx=4)
            ttk.Label(r, text="止(hex,含):").pack(side="left")
            self.ent_fram_e = ttk.Entry(r, width=8); self.ent_fram_e.insert(0, f"{int(self._profile_memory('address_max', 0)):04X}")
            self.ent_fram_e.pack(side="left", padx=4)
            range_start = int(self._profile_memory("address_min", 0))
            range_end = int(self._profile_memory("address_max", 0))
            range_size = max(0, range_end - range_start + 1)
            self.var_fram_range_label = tk.StringVar(
                value=f"地址为十六进制 · 当前机型完整范围 0x{range_start:04X}–0x{range_end:04X}（{range_size:,} 字节）")
            ttk.Label(r, textvariable=self.var_fram_range_label,
                      style="Subtle.TLabel").pack(side="left", padx=10)

            paned = ttk.PanedWindow(f, orient="horizontal")
            paned.pack(fill="both", expand=True, pady=4)

            left = ttk.Frame(paned)
            ttk.Label(left, text="FRAM 区域（中文说明）").pack(anchor="w")
            cols = ("start", "end", "size", "name", "conf")
            tree_box, self.fram_tree = self._tree(left, cols, (
                    ("start", "起", 62), ("end", "止(含)", 62), ("size", "大小", 52),
                    ("name", "区域 / 中文说明", 300), ("conf", "置信", 56)), 13)
            tree_box.pack(fill="both", expand=True)
            self.fram_tree.bind("<<TreeviewSelect>>", self.on_fram_region_select)
            zh = self.profile_data.get("fram_regions_zh", {})
            for row in self.fram_regions:
                inclusive_end = max(row["start_i"], row["end_i"] - 1)
                self.fram_tree.insert(
                    "", "end", iid=f"r{row['start_i']:04X}",
                    values=(row["start"], f"0x{inclusive_end:04X}", row["size"],
                            row.get("purpose") or zh.get(row["name"], row["name"]),
                            row.get("confidence", "")))
            ttk.Label(left, text="选中区域可快速定位；写入前请先备份并核对。",
                      foreground="#A13D38").pack(anchor="w", pady=(5, 0))
            paned.add(left, weight=2)

            right = ttk.Frame(paned)
            ttk.Label(right, text="FRAM 数据 · 16 字节/行 · 可滚动查看完整内容").pack(anchor="w")
            self.txt_fram = self._mktext(right, 20)
            self.txt_fram.pack(fill="both", expand=True)
            self.txt_fram.insert("1.0", "# 选择“读取全部 FRAM”可读取当前机型完整范围。\n"
                                        "# 数据格式：0000: 12 34 56 ...；写入前请备份。\n")
            paned.add(right, weight=3)

            # 悬停：区域树显示区域说明/已知值；十六进制编辑框显示光标所在字节。
            self.fram_tree_tip = HoverToolTip(self.fram_tree, self._fram_tree_tip)
            self.fram_text_tip = HoverToolTip(self.txt_fram, self._fram_text_tip)

        def _fram_region_at(self, addr: int):
            """按地址查找 FRAM 区域；CSV 的 end 为半开区间上界。"""
            for row in self.fram_regions:
                if row["start_i"] <= addr < row["end_i"]:
                    return row
            return None

        def _fram_tip_text(self, row, addr: int | None = None) -> str:
            """生成 FRAM 区域的悬停说明：作用 + 已知可用数值 + 证据。"""
            start = int(row.get("start_i", 0))
            end = max(start, int(row.get("end_i", start + 1)) - 1)
            name = row.get("purpose") or row.get("name") or "—"
            values = row.get("values") or "—（未确认）"
            evidence = row.get("evidence") or "—"
            confidence = row.get("confidence") or "unknown"
            head = (f"0x{start:04X}–0x{end:04X}"
                    f"（{row.get('size', end - start + 1)}B）· {name}")
            if addr is not None:
                head = f"0x{addr:04X} · {head}"
            return (f"{head}\n"
                    f"已知可用数值/作用：{values}\n"
                    f"证据：{evidence}\n"
                    f"置信：{confidence}")

        def _fram_tree_tip(self, event):
            iid = self.fram_tree.identify_row(event.y)
            if not iid:
                return None
            for row in self.fram_regions:
                if f"r{row['start_i']:04X}" == iid:
                    return self._fram_tip_text(row)
            return None

        @staticmethod
        def _fram_addr_from_line(line_text: str, col: int):
            """把一行十六进制文本的列号换算成 FRAM 地址；不是数据行则返回 None。"""
            m = re.match(r"^([0-9A-Fa-f]{4}):\s*(.*)$", line_text)
            if not m:
                return None
            base = int(m.group(1), 16)
            rest = m.group(2)
            prefix = m.start(2)
            if col < prefix or col >= prefix + len(rest):
                return None
            rel = col - prefix
            # _render_fram 固定格式：47 字符十六进制区 + 两空格 + ASCII 区。
            hex_width = 47
            hex_part = rest[:hex_width]
            data_len = (sum(1 for ch in hex_part if ch in "0123456789abcdefABCDEF") + 1) // 2
            if rel < hex_width:
                digits = sum(1 for ch in rest[:rel] if ch in "0123456789abcdefABCDEF")
                byte_idx = digits // 2
                return base + byte_idx if byte_idx < data_len else None
            if rel >= hex_width + 2:
                byte_idx = rel - (hex_width + 2)
                return base + byte_idx if byte_idx < data_len else None
            return None

        def _fram_text_tip(self, event):
            """把编辑框中的鼠标位置换算成 FRAM 地址并生成提示。"""
            try:
                idx = self.txt_fram.index(f"@{event.x},{event.y}")
            except Exception:
                return None
            line_s, col_s = idx.split(".")
            line, col = int(line_s), int(col_s)
            try:
                line_text = self.txt_fram.get(f"{line}.0", f"{line}.end")
            except Exception:
                return None
            addr = self._fram_addr_from_line(line_text, col)
            if addr is None:
                return None
            row = self._fram_region_at(addr)
            if not row:
                return None
            return self._fram_tip_text(row, addr)

        def on_fram_region_select(self, _event=None):
            """把区域树选中行填入起止地址（止为包含式）。"""
            sel = self.fram_tree.selection()
            if not sel:
                return
            iid = sel[0]
            for row in self.fram_regions:
                if f"r{row['start_i']:04X}" == iid:
                    inclusive_end = max(row["start_i"], row["end_i"] - 1)
                    self.ent_fram_s.delete(0, "end"); self.ent_fram_s.insert(0, f"{row['start_i']:04X}")
                    self.ent_fram_e.delete(0, "end"); self.ent_fram_e.insert(0, f"{inclusive_end:04X}")
                    return

        def _require_backup_ack(self) -> bool:
            """Require a verified device identity and an explicit backup acknowledgement."""
            profile = self._profile_cfg()
            expected = str(profile.get("cat_id", "")).strip().upper()
            detected = str(getattr(self, "detected_device_id", "")).strip().upper()
            if not detected or detected == "未查询":
                messagebox.showwarning(APP_TITLE, "请先连接设备并读取 CAT ID，再执行写入。")
                return False
            if expected:
                if expected not in detected:
                    messagebox.showerror(APP_TITLE,
                        f"设备 ID ({detected}) 与当前配置要求 ({expected}) 不匹配，已阻止写入。")
                    return False
            elif not messagebox.askyesno(
                    APP_TITLE,
                    f"当前机型配置没有已确认的 CAT ID。实测 ID 为 {detected}。\n"
                    "请自行核对电台型号/固件后，是否继续该写入？"):
                return False
            if not self.var_backup_ack.get():
                messagebox.showwarning(
                    APP_TITLE,
                    "请先备份当前机型的相关 FRAM 区域，确认备份文件可读取后，勾选备份确认再写入。")
                return False
            return True

        def _fram_range(self) -> tuple[int, int]:
            s = int(self.ent_fram_s.get().strip(), 16)
            e = int(self.ent_fram_e.get().strip(), 16)
            max_addr = int(self._profile_memory("address_max", -1))
            min_addr = int(self._profile_memory("address_min", 0))
            if not min_addr <= s <= e <= max_addr:
                raise ValueError(f"要求 0x{min_addr:04X} <= 起 <= 止 <= 0x{max_addr:04X}")
            return s, e

        def _render_fram(self, data: bytes, start: int):
            """把 FRAM 数据渲染为 16 字节/行的带地址十六进制。"""
            if threading.current_thread() is not threading.main_thread():
                self.ui(lambda: self._render_fram(data, start))
                return
            self.txt_fram.delete("1.0", "end")
            for off in range(0, len(data), 16):
                chunk = data[off:off + 16]
                asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                self.txt_fram.insert("end", f"{start + off:04X}: {hexs(chunk):<47}  {asc}\n")

        def do_fram_read_range(self):
            """按起止地址用 SPR 读取并显示。"""
            if not self.require_open(): return
            try:
                s, e = self._fram_range()
            except Exception as ex:
                messagebox.showwarning(APP_TITLE, str(ex)); return
            def job(cat):
                def prog(a, b): self.status(f"读取 FRAM {a}/{b} …")
                data = cat.read_fram(s, e, progress=prog, cancel=self.cancel)
                self._render_fram(data, s)
                self.log(f"FRAM 读取完成: 0x{s:04X}-0x{e:04X} ({len(data)}B)")
            self.bg(job)

        def do_fram_read_all(self):
            """Read the full external memory range defined by the current profile."""
            if not self.require_open(): return
            if not messagebox.askyesno(APP_TITLE,
                                       f"将逐 2 字节读取完整 FRAM：0x{int(self._profile_memory('address_min', 0)):04X}–0x{int(self._profile_memory('address_max', 0)):04X}（{int(self._profile_memory('address_max', 0))-int(self._profile_memory('address_min', 0))+1:,} 字节）。\n\n读取可能耗时较长，且需要电台持续响应。继续？"):
                return
            self.ent_fram_s.delete(0, "end"); self.ent_fram_s.insert(0, f"{int(self._profile_memory('address_min', 0)):04X}")
            self.ent_fram_e.delete(0, "end"); self.ent_fram_e.insert(0, f"{int(self._profile_memory('address_max', 0)):04X}")
            self.do_fram_read_range()

        def do_fram_write_dump(self):
            """解析编辑框中的每一行，并按行地址写回 FRAM。"""
            if not self.require_open(): return
            try:
                chunks = parse_hex_dump(self.txt_fram.get("1.0", "end"))
            except Exception as ex:
                messagebox.showwarning(APP_TITLE, f"解析失败: {ex}"); return
            if not chunks:
                messagebox.showwarning(APP_TITLE, "编辑框里没有可写数据"); return
            if not self._require_backup_ack():
                return
            total = sum(len(d) for _, d in chunks)
            if not messagebox.askyesno(
                    APP_TITLE,
                    f"确认写回 {len(chunks)} 行、共 {total} 字节？\n"
                    "这是外部 FRAM 写操作；写错可能导致配置/校准异常。\n"
                    "建议先“读取全部 FRAM”并保存备份。"):
                return
            def job(cat):
                done = 0
                for addr, data in chunks:
                    cat.write_fram(addr, data)
                    done += len(data)
                self.log(f"FRAM 编辑框写回完成: {done} 字节")
            self.bg(job)

        def do_quick_backup(self):
            """Back up the full external FRAM range declared by the selected profile."""
            if not self.require_open(): return
            path = filedialog.asksaveasfilename(
                defaultextension=".bin",
                initialfile="fram_full_backup.bin",
                filetypes=[("二进制", "*.bin"), ("全部", "*.*")])
            if not path:
                return
            def job(cat):
                def prog(a, b): self.status(f"备份 FRAM {a}/{b} …")
                start = int(self._profile_memory("address_min", 0))
                end = int(self._profile_memory("address_max", -1))
                data = cat.read_fram(start, end, progress=prog, cancel=self.cancel, strict=True)
                save_bytes(path, data)
                self.log(f"已备份完整 FRAM [{start:04X}–{end:04X}] ({len(data)}B) -> {path}")
            self.bg(job)

        def do_fram_export_bin(self):
            """把编辑框内容按 16 进制解析后保存为 .bin（不访问设备）。"""
            try:
                chunks = parse_hex_dump(self.txt_fram.get("1.0", "end"))
            except Exception as ex:
                messagebox.showwarning(APP_TITLE, f"解析失败: {ex}"); return
            if not chunks:
                messagebox.showwarning(APP_TITLE, "没有可导出的数据"); return
            base = min(a for a, _ in chunks)
            end = max(a + len(d) for a, d in chunks)
            buf = bytearray([0xFF] * (end - base))
            for a, d in chunks:
                buf[a - base:a - base + len(d)] = d
            path = filedialog.asksaveasfilename(defaultextension=".bin")
            if not path:
                return
            save_bytes(path, bytes(buf))
            self.log(f"已导出编辑框数据: {path}（{len(buf)}B，基址 0x{base:04X}）")

        def do_fram_import_bin(self):
            """把 .bin 载入编辑框，起始地址取当前“起”。"""
            path = filedialog.askopenfilename(filetypes=[("二进制", "*.bin"), ("全部", "*.*")])
            if not path:
                return
            try:
                data = load_bytes(path)
                start = int(self.ent_fram_s.get().strip(), 16)
                max_addr = int(self._profile_memory("address_max", -1))
                if start + len(data) - 1 > max_addr:
                    raise ValueError("文件超出当前机型 FRAM 地址范围")
                self._render_fram(data, start)
                self.ent_fram_e.delete(0, "end")
                self.ent_fram_e.insert(0, f"{start + len(data) - 1:04X}")
                self.log(f"已导入 {path} 到编辑框（{len(data)}B @ 0x{start:04X}）")
            except Exception as ex:
                messagebox.showerror(APP_TITLE, f"导入失败: {ex}")

        # ---------------- Tab: 服务菜单 ----------------
        def _tab_service(self):
            """服务菜单：读取 配置长度 表、按项改校准值、SPN 选择/应用。"""
            f = ttk.Frame(self.nb, padding=10)
            self.nb.add(f, text="服务菜单")

            top = ttk.Frame(f); top.pack(fill="x", pady=3)
            ttk.Button(top, text="读取整表 (SPr 配置长度)", command=self.do_sm_read_all).pack(side="left", padx=3)
            ttk.Button(top, text="SPARD 整表", command=self.do_sm_spard).pack(side="left", padx=3)
            ttk.Button(top, text="读取选中项", command=self.do_sm_read_selected).pack(side="left", padx=3)
            ttk.Button(top, text="写入选中项校准值", command=self.do_sm_write_selected).pack(side="left", padx=3)
            ttk.Button(top, text="SPN 选择/应用 N", command=self.do_sm_spn).pack(side="left", padx=3)
            ttk.Button(top, text="SPAET 应用特殊项", command=self.do_sm_spaet).pack(side="left", padx=3)
            ttk.Button(top, text="SPAWE 写使能", command=self.do_sm_spawe).pack(side="left", padx=3)
            ttk.Button(top, text="服务模式入口（可能重启）", command=self.do_service_mode).pack(side="left", padx=3)
            ttk.Button(top, text="读取服务菜单索引/子码", command=self.do_sm_read_ns).pack(side="left", padx=3)
            ttk.Button(top, text="SPN 读当前 N", command=self.do_sm_spn_read).pack(side="left", padx=3)
            self.var_sm_ns = tk.StringVar(value="N/S: 未读取")
            ttk.Label(top, textvariable=self.var_sm_ns, foreground="#054").pack(side="left", padx=8)
            ttk.Checkbutton(top, text="我已备份相关 FRAM 区域",
                            variable=self.var_backup_ack).pack(side="left", padx=8)

            # Service menu list schema: index / label / band / address / apply target / values
            cols = ("n", "label", "band", "item", "apply", "cur", "new")
            tree_box, self.sm_tree = self._tree(f, cols, (
                    ("n", "N", 55), ("label", "标签（固件 ASCII）", 330),
                    ("band", "频段 MHz", 82), ("item", "存储 FRAM", 90),
                    ("apply", "应用目标", 90), ("cur", "当前", 55), ("new", "新值", 55)), 14)
            tree_box.pack(fill="both", expand=True, pady=3)
            self.sm_tree.bind("<<TreeviewSelect>>", self.on_sm_select)
            for it in self.service_items:
                self.sm_tree.insert(
                    "", "end", iid=f"sm{it['n']}",
                    values=(f"{it['n']:03d} {it.get('code_hex', '')}",
                            it.get("label", ""), ("—" if it.get("band_mhz_f") is None else f"{it['band_mhz_f']:g}"),
                            it.get("item_fram", ""), it.get("apply_fram", ""), "", ""))

            sel = ttk.LabelFrame(f, text="选中项 / 编辑（校准值就是 配置长度 表中该 N 的字节）")
            sel.pack(fill="x", pady=3)
            r1 = ttk.Frame(sel); r1.pack(fill="x", padx=6, pady=3)
            ttk.Label(r1, text="N:").pack(side="left")
            self.ent_sm_n = ttk.Entry(r1, width=6); self.ent_sm_n.pack(side="left", padx=4)
            ttk.Label(r1, text="标签:").pack(side="left")
            self.ent_sm_label = ttk.Entry(r1, width=32); self.ent_sm_label.pack(side="left", padx=4)
            ttk.Label(r1, text="频段:").pack(side="left")
            self.ent_sm_band = ttk.Entry(r1, width=8); self.ent_sm_band.pack(side="left", padx=4)
            ttk.Label(r1, text="存储:").pack(side="left")
            self.ent_sm_addr = ttk.Entry(r1, width=8); self.ent_sm_addr.pack(side="left", padx=4)
            ttk.Label(r1, text="应用:").pack(side="left")
            self.ent_sm_apply = ttk.Entry(r1, width=8); self.ent_sm_apply.pack(side="left", padx=4)
            ttk.Label(r1, text="类型:").pack(side="left")
            self.ent_sm_kind = ttk.Entry(r1, width=14); self.ent_sm_kind.pack(side="left", padx=4)

            r2 = ttk.Frame(sel); r2.pack(fill="x", padx=6, pady=3)
            ttk.Label(r2, text="当前值(hex/dec):").pack(side="left")
            self.ent_sm_cur = ttk.Entry(r2, width=10); self.ent_sm_cur.pack(side="left", padx=4)
            ttk.Label(r2, text="新值(0-255):").pack(side="left")
            self.ent_sm_new = ttk.Entry(r2, width=10); self.ent_sm_new.pack(side="left", padx=4)
            self.var_sm_apply = tk.BooleanVar(value=False)
            ttk.Checkbutton(r2, text="写后 SPAET 应用（仅特殊项）",
                            variable=self.var_sm_apply).pack(side="left", padx=8)

            note = ttk.Label(f, foreground="#a00", justify="left", wraplength=1050, text=(
                "菜单索引、标签、地址、门控与应用规则应来自当前机型的外部映射文件。\n"
                "SPN 会选择/应用项目并可能切换波段或模式，不是只读命令。\n"
                "SPN/SPA* 可能需要服务模式；SPAET 的行为取决于机型，普通项目可能返回 ?;。"
                "r3l=3 的项目每 3 项共享 1 个应用目标，写源值不等于直接改共享目标。\n"
                "复位/服务命令行为依机型而异；不要使用未经确认的操作码。"
                "写前至少备份当前机型相关 FRAM 区域。"))
            note.pack(anchor="w", pady=4)

        def on_sm_select(self, _event=None):
            """把服务菜单树选中行填入编辑框。"""
            sel = self.sm_tree.selection()
            if not sel:
                return
            for it in self.service_items:
                if f"sm{it['n']}" != sel[0]:
                    continue
                self.ent_sm_n.delete(0, "end"); self.ent_sm_n.insert(0, str(it["n"]))
                self.ent_sm_label.delete(0, "end"); self.ent_sm_label.insert(0, it.get("label", ""))
                self.ent_sm_band.delete(0, "end"); self.ent_sm_band.insert(0, ("—" if it.get("band_mhz_f") is None else f"{it['band_mhz_f']:g}"))
                self.ent_sm_addr.delete(0, "end"); self.ent_sm_addr.insert(0, it.get("item_fram", ""))
                self.ent_sm_apply.delete(0, "end"); self.ent_sm_apply.insert(0, it.get("apply_fram", ""))
                kind = it.get("apply_kind", "")
                self.ent_sm_kind.delete(0, "end")
                self.ent_sm_kind.insert(0, f"{kind}（{self._cfg('service_menu', 'apply_kind_labels', {}).get(kind, '未配置')}）")
                idx = it["item_addr"] - int(self._cfg("settings_table", "start_addr", 0) or 0)
                self.ent_sm_cur.delete(0, "end")
                self.ent_sm_new.delete(0, "end")
                if 0 <= idx < len(self.cal_table):
                    v = self.cal_table[idx]
                    self.ent_sm_cur.insert(0, f"{v:02X} ({v})")
                    self.ent_sm_new.insert(0, f"{v:02X}")
                return

        def _sm_parse_index(self, text: str) -> int:
            """Accept service index as decimal or explicit 0x-prefixed hex."""
            t = text.strip()
            if not t:
                raise ValueError("请输入服务菜单项目 N（十进制或 0x 十六进制）")
            return int(t, 0) if t.lower().startswith("0x") else int(t, 10)

        def _sm_parse_value(self, text: str) -> int:
            """接受十进制、0xNN、NNh 或含 A–F 的纯 hex 的 0–255 值。"""
            t = text.strip().lower()
            if not t:
                raise ValueError("值不能为空；请输入 0–255、0xNN 或 NNh")
            if t.startswith("0x"):
                v = int(t, 16)
            elif t.endswith("h"):
                v = int(t[:-1], 16)
            else:
                try:
                    v = int(t, 10)
                except ValueError:
                    # 树里显示的是 "0A"/"7F" 这类裸 hex；无字母时仍按十进制。
                    v = int(t, 16)
            if not 0 <= v <= 0xFF:
                raise ValueError("值必须 0–255")
            return v

        def _sm_refresh_row(self, n: int, value: int | None = None):
            """刷新树中某个 N 的当前值/新值。"""
            if threading.current_thread() is not threading.main_thread():
                self.ui(lambda: self._sm_refresh_row(n, value))
                return
            iid = f"sm{n}"
            if not self.sm_tree.exists(iid):
                return
            vals = list(self.sm_tree.item(iid, "values"))
            if value is not None:
                vals[5] = f"{value:02X}"
                vals[6] = f"{value:02X}"
            self.sm_tree.item(iid, values=vals)

        def _sm_render_table(self, tb: bytes):
            """把 配置长度 表填入 294 行服务菜单的“当前值”列。"""
            if threading.current_thread() is not threading.main_thread():
                self.ui(lambda: self._sm_render_table(tb))
                return
            self.cal_table = tb
            for it in self.service_items:
                idx = it["item_addr"] - int(self._cfg("settings_table", "start_addr", 0) or 0)
                if 0 <= idx < len(tb):
                    self._sm_refresh_row(it["n"], tb[idx])
            self.log(f"服务菜单表已更新: {len(tb)}B")

        def do_sm_read_all(self):
            """用 SPr 逐字节读取 配置长度 设置表并填入服务菜单。"""
            if not self.require_open(): return
            def job(cat):
                def prog(a, b): self.status(f"读取服务菜单表 {a}/{b} …")
                tb = cat.read_settings_table(progress=prog, cancel=self.cancel)
                self._sm_render_table(tb)
                self._render_cal(tb)
            self.bg(job)

        def do_sm_spard(self):
            """用 SPARD 一次读取 配置长度（需服务模式门控）。"""
            if not self.require_open(): return
            def job(cat):
                tb = cat.spa_table()
                self._sm_render_table(tb)
                self._render_cal(tb)
                self.log("SPARD 整表读取成功（配置长度）")
            self.bg(job)

        def do_sm_read_selected(self):
            """读取当前选中 N 的校准字节。"""
            if not self.require_open(): return
            try:
                n = self._sm_parse_index(self.ent_sm_n.get())
                it = next(x for x in self.service_items if x["n"] == n)
            except Exception:
                messagebox.showwarning(APP_TITLE, "请先选择一个有效的服务菜单项"); return
            def job(cat):
                r = cat.spr_table(it["item_addr"])
                def show():
                    self.ent_sm_cur.delete(0, "end")
                    self.ent_sm_cur.insert(0, f"{r[6]:02X} ({r[6]})")
                self.ui(show)
                self._sm_refresh_row(n, r[6])
                self.log(f"服务菜单 N={n:03d} 读取: {r[6]:02X}")
            self.bg(job)

        def do_sm_write_selected(self):
            """写当前 N 的 配置长度 表字节；可选写后 SPAET。"""
            if not self.require_open(): return
            try:
                n = self._sm_parse_index(self.ent_sm_n.get())
                it = next(x for x in self.service_items if x["n"] == n)
                v = self._sm_parse_value(self.ent_sm_new.get())
            except Exception as e:
                messagebox.showwarning(APP_TITLE, f"参数错误: {e}"); return
            if not self._require_backup_ack():
                return
            apply_after = bool(self.var_sm_apply.get())
            if not messagebox.askyesno(
                    APP_TITLE,
                    f"确认写服务菜单 N={n:03d}（{it.get('label', '')}）？\n"
                    f"配置长度 表地址 0x{it['item_addr']:04X} = 0x{v:02X}\n"
                    "这会改变校准源值；特殊项还需 SPAET/SPN 才会应用。继续？"):
                return
            def job(cat):
                cat.spw_table(it["item_addr"], v)
                idx = it["item_addr"] - int(self._cfg("settings_table", "start_addr", 0) or 0)
                if 0 <= idx < len(self.cal_table):
                    self.cal_table = self.cal_table[:idx] + bytes([v]) + self.cal_table[idx + 1:]
                self._sm_refresh_row(n, v)
                self.log(f"服务菜单 N={n:03d} 已写 0x{v:02X}")
                if apply_after:
                    r = cat.spa_et()
                    self.log(f"SPAET: {hexs(r) if r else '无回包'}")
            self.bg(job)

        def do_sm_read_ns(self):
            """Read the service-menu index and subcode at addresses configured for this model."""
            if not self.require_open(): return
            def job(cat):
                base = self._cfg("service_menu", "index_addr")
                subcode = self._cfg("service_menu", "subcode_addr")
                if base is None or subcode is None:
                    raise CatError("当前机型配置未提供服务菜单索引/子码地址")
                base, subcode = int(base), int(subcode)
                r = cat.spr(base)          # 0x0344–0x0345，u16 大端
                rs = cat.spr(subcode)
                n = (r[5] << 8) | r[6]
                sub = rs[5]
                self.ui(lambda: self.var_sm_ns.set(f"N=0x{n:03X} ({n}) / S=0x{sub:02X} ({sub})"))
                self.log(f"服务菜单 N/S: N=0x{n:03X} S=0x{sub:02X}")
            self.bg(job)

        def do_sm_spn_read(self):
            """SPN FF FF：读取当前服务菜单项目编号（可能需要服务模式）。"""
            if not self.require_open(): return
            def job(cat):
                r = cat.spn_read()
                self.log(f"SPN 读: {hexs(r)}")
                if (len(r) == 7 and r[:3] == b"SPN" and r[-1:] == b";"
                        and r[5] == (sum(r[:5]) & 0xFF)):
                    n = (r[3] << 8) | r[4]
                    self.ui(lambda: (self.ent_sm_n.delete(0, "end"), self.ent_sm_n.insert(0, str(n))))
                    self.log(f"当前服务菜单项目 N=0x{n:03X} ({n})")
                self._q_append_text(self.txt_power, f"SPN 读: {hexs(r)}\n")
            self.bg(job)

        def do_sm_spn(self):
            """发送 SPN 选择/应用项目 N（会切换波段/模式）。"""
            if not self._cap("service_menu"):
                messagebox.showwarning(APP_TITLE, "当前机型配置未启用服务菜单操作。")
                return
            if not self.require_open(): return
            try:
                n = self._sm_parse_index(self.ent_sm_n.get())
                item_count = self._cfg("service_menu", "item_count")
                if item_count is None or not 0 <= n < int(item_count):
                    raise ValueError
            except Exception:
                messagebox.showwarning(APP_TITLE, "当前机型未配置 SPN 项目范围，或 N 超出范围"); return
            if not messagebox.askyesno(
                    APP_TITLE,
                    f"发送 SPN N={n:03d}？\n"
                    "它会选择/应用服务项目，并切换当前波段/模式；需要服务模式门控。\n"
                    "这不是只读命令。"):
                return
            def job(cat):
                r = cat.spn_write(n)
                self.log(f"SPN {n:03d}: {hexs(r) if r else '无回包'}")
            self.bg(job)

        def do_sm_spaet(self):
            """对当前特殊服务项发送 SPAET；普通项会回 ?;。"""
            if not self.require_open(): return
            if not messagebox.askyesno(APP_TITLE, "确认发送 SPAET？会触发设置表应用逻辑。"):
                return
            def job(cat):
                r = cat.spa_et()
                self.log(f"SPAET: {hexs(r) if r else '无回包'}")
            self.bg(job)

        def do_sm_spawe(self):
            """发送 SPAWE 写使能。"""
            if not self.require_open(): return
            def job(cat):
                r = cat.spa_we()
                self.log(f"SPAWE: {hexs(r) if r else '无回包'}")
            self.bg(job)

        def _pe_addr_text(self):
            """Return the configured PE address range, or an explicit unconfigured marker."""
            pe_start = self._cfg("pe_calibration", "address_start")
            pe_end = self._cfg("pe_calibration", "address_end")
            if pe_start is None or pe_end is None:
                return "地址未配置"
            return f"0x{int(pe_start):04X}–0x{int(pe_end):04X}"

        def _pe_positions(self):
            """Return A/B/C selector choices; prefer per-position lists."""
            pos = self._cfg("pe_calibration", "selector_positions", None)
            if pos:
                return [[str(v) for v in p] for p in pos]
            sel = [str(v) for v in self._cfg("pe_calibration", "selectors", [])]
            return [sel, sel, sel]

        # ---------------- Tab: 校准 ----------------
        def _tab_cal(self):
            f = ttk.Frame(self.nb, padding=10)
            self.nb.add(f, text="校准 / PE")
            top = ttk.Frame(f); top.pack(fill="x")
            ttk.Button(top, text="读取设置表(配置长度)", command=self.do_cal_read).pack(side="left", padx=4)
            ttk.Button(top, text="写入设置表(危险)", command=self.do_cal_write).pack(side="left", padx=4)
            ttk.Button(top, text="从备份文件载入…", command=self.do_cal_load).pack(side="left", padx=4)
            ttk.Button(top, text="保存为 .bin…", command=self.do_cal_save_bin).pack(side="left", padx=4)

            mid = ttk.Frame(f); mid.pack(fill="x", pady=6)
            self.var_cal_verify = tk.BooleanVar(value=True)
            ttk.Checkbutton(mid, text="写回后校验", variable=self.var_cal_verify).pack(side="left")
            self.var_cal_show_hex = tk.BooleanVar(value=True)
            ttk.Checkbutton(mid, text="显示十六进制", variable=self.var_cal_show_hex).pack(side="left", padx=10)
            ttk.Checkbutton(mid, text="我已备份相关 FRAM 区域",
                            variable=self.var_backup_ack).pack(side="left", padx=10)
            ttk.Label(mid, text="（表基址、地址范围与长度均来自当前机型配置）",
                      foreground="#666").pack(side="left")

            self.var_cal_table = tk.StringVar(
                value=f"设置/校准表 ({self._cfg('settings_table', 'length') or '未配置'} 字节):")
            ttk.Label(f, textvariable=self.var_cal_table).pack(anchor="w")
            self.txt_cal = self._mktext(f, 14)
            self.txt_cal.pack(fill="both", expand=True)

            ttk.Separator(f, orient="horizontal").pack(fill="x", pady=6)
            self.var_pe_raw_label = tk.StringVar(
                value=f"PE 参数 ({self._pe_addr_text()}; 选择子由配置定义) + 原始写:")
            ttk.Label(f, textvariable=self.var_pe_raw_label).pack(anchor="w")
            g = ttk.Frame(f); g.pack(fill="x", pady=4)
            self.btn_pe_all_cal = ttk.Button(g, text="读取全部 PE", command=self.do_pe_all)
            self.btn_pe_all_cal.pack(side="left", padx=4)
            ttk.Label(g, text="原始写 PE(6字符: 3选+3值):").pack(side="left", padx=(12, 2))
            self.ent_pe_raw = ttk.Entry(g, width=10)
            self.ent_pe_raw.pack(side="left")
            self.btn_pe_send = ttk.Button(g, text="发送", command=self.do_pe_write_raw)
            self.btn_pe_send.pack(side="left", padx=6)
            self.txt_pe = self._mktext(f, 10)
            self.txt_pe.pack(fill="both", expand=True, pady=(4, 0))

        def do_cal_read(self):
            if not self.require_open(): return
            def job(cat):
                def prog(a, b):
                    self.status(f"读取设置表 {a}/{b} …")
                tb = cat.read_settings_table(progress=prog, cancel=self.cancel)
                self.cal_table = tb
                self._render_cal(tb)
                self.log(f"设置表已读取: {len(tb)} 字节")
            self.bg(job)

        def _render_cal(self, tb: bytes):
            if threading.current_thread() is not threading.main_thread():
                self.ui(lambda: self._render_cal(tb))
                return
            self.txt_cal.delete("1.0", "end")
            if self.var_cal_show_hex.get():
                for off in range(0, len(tb), 16):
                    chunk = tb[off:off + 16]
                    addr = int(self._cfg("settings_table", "start_addr", 0) or 0) + off
                    self.txt_cal.insert("end", f"0x{addr:04X}: {hexs(chunk)}\n")
            else:
                self.txt_cal.insert("end", hexs(tb, sep=""))

        def do_cal_write(self):
            if not self.require_open(): return
            if not hasattr(self, "cal_table") or not self.cal_table:
                messagebox.showwarning(APP_TITLE, "请先读取或载入设置表")
                return
            if len(self.cal_table) != int(self._cfg('settings_table', 'length', 0) or 0):
                messagebox.showerror(APP_TITLE,
                                     f"设置表长度 {len(self.cal_table)} != {int(self._cfg('settings_table', 'length', 0) or 0)}，拒绝写入")
                return
            if not self._require_backup_ack():
                return
            if not messagebox.askyesno(APP_TITLE,
                                       f"确认把当前 {self._cfg('settings_table', 'length')} 字节设置表写回设备？\n这会改校准数据，风险极高。"):
                return
            verify = bool(self.var_cal_verify.get())
            def job(cat):
                def prog(a, b):
                    self.status(f"写回设置表 {a}/{b} …")
                n = restore_table(cat, self.cal_table, progress=prog,
                                  cancel=self.cancel, verify=verify)
                self.log(f"设置表写回完成: {n} 字节，校验={'通过' if verify else '跳过'}")
            self.bg(job)

        def do_cal_load(self):
            path = filedialog.askopenfilename(filetypes=[("JSON/二进制", "*.json *.bin"), ("全部", "*.*")])
            if not path:
                return
            try:
                if path.lower().endswith(".json"):
                    obj = load_json(path)
                    tb = bytes.fromhex(obj["data"]["settings_table"]["hex"])
                else:
                    tb = load_bytes(path)
                if len(tb) != int(self._cfg('settings_table', 'length', 0) or 0):
                    if not messagebox.askyesno(APP_TITLE,
                                               f"长度 {len(tb)} != {int(self._cfg('settings_table', 'length', 0) or 0)}，仍要载入？"):
                        return
                self.cal_table = tb
                self._render_cal(tb)
                self.log(f"已载入设置表: {path}（{len(tb)}B）")
            except Exception as e:
                messagebox.showerror(APP_TITLE, f"载入失败: {e}")

        def do_cal_save_bin(self):
            if not hasattr(self, "cal_table") or not self.cal_table:
                messagebox.showwarning(APP_TITLE, "请先读取设置表"); return
            path = filedialog.asksaveasfilename(defaultextension=".bin")
            if not path:
                return
            save_bytes(path, self.cal_table)
            self.log(f"已保存设置表: {path}")

        def do_pe_all(self):
            if not self._cap("pe_calibration"):
                messagebox.showwarning(APP_TITLE, "当前机型配置未启用 PE 校准操作。")
                return
            if not self.require_open(): return
            def job(cat):
                lines = []
                ok = 0
                positions = self._pe_positions()
                total = len(positions[0]) * len(positions[1]) * len(positions[2])
                for s0 in positions[0]:
                    for s1 in positions[1]:
                        for m in positions[2]:
                            try:
                                r = cat.pe_read(int(s0), int(s1), int(m))
                            except CatError as e:
                                lines.append(f"PE{s0}{s1}{m}: 错误 {e}\n")
                                continue
                            if r is None:
                                lines.append(f"PE{s0}{s1}{m}: ?;\n")
                            else:
                                ok += 1
                                lines.append(f"PE{s0}{s1}{m}: {r.decode('latin-1')}   ({hexs(r)})\n")
                lines.append(f"\n有效 {ok}/{total}\n")
                def show():
                    self.txt_pe.delete("1.0", "end")
                    self.txt_pe.insert("end", "".join(lines))
                    self.txt_pe.see("end")
                self.ui(show)
                self.log(f"PE 读取完成: 有效 {ok}/{total}")
            self.bg(job)

        def do_pe_write_raw(self):
            if not self._cap("pe_calibration"):
                messagebox.showwarning(APP_TITLE, "当前机型配置未启用 PE 校准写入。")
                return
            if not self.require_open(): return
            if not self._require_backup_ack(): return
            p = self.ent_pe_raw.get().strip().upper()
            positions = self._pe_positions()
            if len(p) != 6 or any(c not in positions[i] for i, c in enumerate(p[:3])):
                messagebox.showwarning(APP_TITLE, "PE 选择子不符合当前机型配置")
                return
            value = p[3:]
            signed = len(value) == 3 and value[0] in "+-" and all(c in "0123456789ABCDEF" for c in value[1:])
            unsigned = len(value) == 3 and all(c in "0123456789ABCDEF" for c in value)
            if not (signed or unsigned):
                messagebox.showwarning(APP_TITLE,
                                       "PE 写格式：PE<3位选择子><3字符值>，例如 PE000+0A 或 PE002010")
                return
            if not messagebox.askyesno(APP_TITLE, f"确认发送 PE{p} ？会写校准。"):
                return
            def job(cat):
                r = cat.pe_write_raw(p)
                self._q_append_text(self.txt_pe, f"PE{p} -> {hexs(r) if r else '无回包'}\n")
            self.bg(job)

        # ---------------- Tab: PE 校准 ----------------
        def _tab_pe(self):
            """PE 校准页：按当前机型配置的选择子生成表格，读取原始回包并按 3 字符值写入。"""
            f = ttk.Frame(self.nb, padding=10)
            self.nb.add(f, text="PE 校准")

            top = ttk.Frame(f); top.pack(fill="x", pady=3)
            ttk.Button(top, text="读取全部 PE", command=self.do_pe_read_all2).pack(side="left", padx=3)
            ttk.Button(top, text="读取选中", command=self.do_pe_read_one).pack(side="left", padx=3)
            ttk.Button(top, text="写入选中", command=self.do_pe_write_one).pack(side="left", padx=3)
            ttk.Checkbutton(top, text="我已备份相关 FRAM 区域",
                            variable=self.var_backup_ack).pack(side="left", padx=8)

            cols = ("sel", "desc", "resp", "cur")
            tree_box, self.pe_tree = self._tree(f, cols, (
                    ("sel", "选择子", 70), ("desc", "中文说明", 300),
                    ("resp", "原始回包", 330), ("cur", "当前值", 100)), 13)
            tree_box.pack(fill="both", expand=True, pady=3)
            self.pe_tree.bind("<<TreeviewSelect>>", self.on_pe_select)
            pe_pos = self._pe_positions()
            for s0 in pe_pos[0]:
                for s1 in pe_pos[1]:
                    for m in pe_pos[2]:
                        sel = f"{s0}{s1}{m}"
                        self.pe_tree.insert("", "end", iid=f"pe{sel}", values=(
                            sel, self._pe_desc(sel), "", ""))

            box = ttk.LabelFrame(f, text="PE 写入（格式：PE<选择子><3 字符值>；+05 / -03 / 010）")
            box.pack(fill="x", pady=3)
            r = ttk.Frame(box); r.pack(fill="x", padx=6, pady=4)
            ttk.Label(r, text="选择子:").pack(side="left")
            self.ent_pe_sel = ttk.Entry(r, width=6); self.ent_pe_sel.pack(side="left", padx=4)
            ttk.Label(r, text="值(3 字符):").pack(side="left")
            self.ent_pe_val = ttk.Entry(r, width=8); self.ent_pe_val.pack(side="left", padx=4)
            ttk.Label(r, text="当前回包:").pack(side="left")
            self.ent_pe_cur2 = ttk.Entry(r, width=34); self.ent_pe_cur2.pack(side="left", padx=4)

            pe_note = (self._cfg("pe_calibration", "note", "")
                       or "选择子/位域尚未完全解码，本页只做原始读写。")
            self.var_pe_note = tk.StringVar(value=(
                f"PE 参数地址：{self._pe_addr_text()}；{pe_note}\n"
                "写入前必须记录当前回包；没有真机验证时不要批量改。值字段为 3 字符："
                "LEVEL 用 +05/-03，FREQ/BWTH 用 010/002；程序发送 PE<选择子><值>;。\n"
                "PE 写会直接改校准并可能写总线，风险高；请先备份当前机型相关 FRAM 区域。"))
            ttk.Label(f, textvariable=self.var_pe_note, foreground="#a00",
                      justify="left", wraplength=1050).pack(anchor="w", pady=4)

        @staticmethod
        def _pe_desc(sel: str) -> str:
            """Translate A/B/C into the confirmed EQ/P-EQ parameter names."""
            s0, s1, m = sel[0], sel[1], sel[2]
            group = {"0": "EQ", "2": "P-EQ"}.get(s0, f"组{s0}")
            band = {"0": "1", "1": "2", "2": "3"}.get(s1, s1)
            kind = {"0": "FREQ", "1": "LEVEL", "2": "BWTH"}.get(m, f"模式{m}")
            return f"{group}{band} {kind}"

        def on_pe_select(self, _event=None):
            """选中 PE 行时填入选择子和当前回包。"""
            sel = self.pe_tree.selection()
            if not sel:
                return
            selstr = sel[0][2:]
            self.ent_pe_sel.delete(0, "end"); self.ent_pe_sel.insert(0, selstr)
            vals = self.pe_tree.item(sel[0], "values")
            self.ent_pe_cur2.delete(0, "end")
            self.ent_pe_cur2.insert(0, vals[2] if len(vals) > 2 else "")
            self.ent_pe_val.delete(0, "end")
            self.ent_pe_val.insert(0, vals[3] if len(vals) > 3 else "")

        def _pe_fill_row(self, sel: str, text: str):
            """刷新某组 PE 的回包/当前值列。"""
            if threading.current_thread() is not threading.main_thread():
                self.ui(lambda: self._pe_fill_row(sel, text))
                return
            iid = f"pe{sel}"
            if not self.pe_tree.exists(iid):
                return
            desc = self._pe_desc(sel)
            vals = list(self.pe_tree.item(iid, "values"))
            vals[2] = text
            # PE 读回包格式：PE<3选><3字符值>; 例如 PE000+0A; / PE002010;
            cur = ""
            m = re.search(r"PE\d{3}([+-][0-9A-Fa-f]{2}|[0-9A-Fa-f]{3});", text)
            if m:
                cur = m.group(1).upper()
            vals[3] = cur
            self.pe_tree.item(iid, values=vals)

        def do_pe_read_all2(self):
            """逐个读取配置中定义的 PE 选择子，填表。"""
            if not self._cap("pe_calibration"):
                messagebox.showwarning(APP_TITLE, "当前机型配置未启用 PE 校准操作。")
                return
            if not self.require_open(): return
            def job(cat):
                ok = 0
                positions = self._pe_positions()
                total = len(positions[0]) * len(positions[1]) * len(positions[2])
                for s0 in positions[0]:
                    for s1 in positions[1]:
                        for m in positions[2]:
                            sel = f"{s0}{s1}{m}"
                            try:
                                r = cat.pe_read(int(s0), int(s1), int(m))
                            except CatError as e:
                                self._pe_fill_row(sel, f"错误: {e}")
                                continue
                            if r is None:
                                self._pe_fill_row(sel, "?;")
                            else:
                                ok += 1
                                self._pe_fill_row(sel, r.decode("latin-1", "replace")
                                                  + "  " + hexs(r))
                self.log(f"PE 读取完成: 有效 {ok}/{total}")
            self.bg(job)

        def do_pe_read_one(self):
            """读取当前选择子的 PE。"""
            if not self._cap("pe_calibration"):
                messagebox.showwarning(APP_TITLE, "当前机型配置未启用 PE 校准操作。")
                return
            if not self.require_open(): return
            sel = self.ent_pe_sel.get().strip()
            positions = self._pe_positions()
            if len(sel) != 3 or any(c not in positions[i] for i, c in enumerate(sel)):
                messagebox.showwarning(APP_TITLE, "选择子不符合当前机型配置"); return
            def job(cat):
                r = cat.pe_read(int(sel[0]), int(sel[1]), int(sel[2]))
                text = "?;" if r is None else r.decode("latin-1", "replace") + "  " + hexs(r)
                self._pe_fill_row(sel, text)
                def show():
                    self.ent_pe_cur2.delete(0, "end"); self.ent_pe_cur2.insert(0, text)
                self.ui(show)
                self.log(f"PE{sel}: {text}")
            self.bg(job)

        def do_pe_write_one(self):
            """按选择子 + 3 字符值写 PE（+05/-03/010）。"""
            if not self._cap("pe_calibration"):
                messagebox.showwarning(APP_TITLE, "当前机型配置未启用 PE 校准写入。")
                return
            if not self.require_open(): return
            sel = self.ent_pe_sel.get().strip()
            val = self.ent_pe_val.get().strip().upper()
            positions = self._pe_positions()
            if len(sel) != 3 or any(c not in positions[i] for i, c in enumerate(sel)):
                messagebox.showwarning(APP_TITLE, "选择子不符合当前机型配置"); return
            signed = len(val) == 3 and val[0] in "+-" and all(c in "0123456789ABCDEF" for c in val[1:])
            unsigned = len(val) == 3 and all(c in "0123456789ABCDEF" for c in val)
            if not (signed or unsigned):
                messagebox.showwarning(APP_TITLE, "值必须为 +XX、-XX 或 XXX（3 字符）"); return
            payload = sel + val
            if not self._require_backup_ack(): return
            if not messagebox.askyesno(APP_TITLE, f"确认发送 PE{payload}; ？会写校准。"):
                return
            def job(cat):
                r = cat.pe_write_raw(payload)
                self.log(f"PE{payload}: {hexs(r) if r else '无回包'}")
            self.bg(job)

        # ---------------- Tab: 备份 ----------------
        def _tab_backup(self):
            f = ttk.Frame(self.nb, padding=10)
            self.nb.add(f, text="备份与恢复")
            ttk.Label(f, text="选择当前机型配置支持的项目；未确认的项目会自动禁用。",
                      font=("", 10, "bold")).pack(anchor="w")

            g = ttk.Frame(f); g.pack(fill="x", pady=6)
            self.bk = {}
            self.bk_widgets = {}
            def ck(key, text, row, col, default=False):
                v = tk.BooleanVar(value=default)
                cb = ttk.Checkbutton(g, text=text, variable=v)
                cb.grid(row=row, column=col, sticky="w", padx=6)
                if key in BACKUP_ITEM_TIPS:
                    ToolTip(cb, BACKUP_ITEM_TIPS[key])
                self.bk[key] = v
                self.bk_widgets[key] = cb
            ck("ve", "VE 版本", 0, 0, bool(self._profile_cfg().get("ve_magic")))
            ck("fd", "FD 通道", 0, 1, bool(self._cap("fd_read")))
            ck("kt", "KT 状态", 0, 2, bool(self._cap("kt_read")))
            ck("pe", "PE 校准", 0, 3, bool(self._cap("pe_calibration")))
            ck("table", f"设置/校准表({self._cfg('settings_table', 'length') or '未配置'}B)", 1, 0, bool(self._cap("settings_table")))
            ck("spard", "SPARD 原始(需服务模式)", 1, 1, False)
            ck("e0", f"E0 ({self._cfg('dumps', 'e0_length') or '未配置'}B)", 1, 2, bool(self._cfg("dumps", "e0_length")))
            ck("e8", f"E8 ({self._cfg('dumps', 'e8_length') or '未配置'}B)", 1, 3, bool(self._cfg("dumps", "e8_length")))
            ck("jp", "JP 寄存器", 2, 0, bool(self._profile_cfg().get("service_code")))
            ck("mem", "外部 FRAM", 2, 1, bool(self._cap("fram_full_read")))

            rng = ttk.Frame(f); rng.pack(fill="x", pady=(0, 4))
            ttk.Label(rng, text="外部 FRAM 范围:").pack(side="left")
            self.ent_mem_s = ttk.Entry(rng, width=8)
            self.ent_mem_s.insert(0, f"{int(self._profile_memory('address_min', 0)):04X}")
            self.ent_mem_s.pack(side="left", padx=4)
            ttk.Label(rng, text="–").pack(side="left")
            self.ent_mem_e = ttk.Entry(rng, width=8)
            self.ent_mem_e.insert(0, f"{int(self._profile_memory('address_max', 0)):04X}")
            self.ent_mem_e.pack(side="left", padx=4)
            ttk.Label(rng, text="（读取耗时取决于地址范围与波特率）",
                      foreground="#a60").pack(side="left", padx=8)

            btns = ttk.Frame(f); btns.pack(fill="x", pady=6)
            ttk.Button(btns, text="① 备份所选项目 → JSON", command=self.do_backup).pack(side="left", padx=4)
            ttk.Button(btns, text="② 载入备份并预览", command=self.do_backup_load).pack(side="left", padx=4)
            self.btn_restore_table = ttk.Button(btns, text="③ 恢复设置表(危险)", command=self.do_restore_table)
            self.btn_restore_table.pack(side="left", padx=4)
            self.btn_restore_fram = ttk.Button(btns, text="④ 恢复外部 FRAM(极危险)", command=self.do_restore_fram)
            self.btn_restore_fram.pack(side="left", padx=4)

            self.txt_bk = self._mktext(f, 18)
            self.txt_bk.pack(fill="both", expand=True)

        def do_backup(self):
            if not self.require_open(): return
            path = filedialog.asksaveasfilename(defaultextension=".json",
                                                initialfile="yaesu_hedden_cat_backup.json",
                                                filetypes=[("JSON", "*.json")])
            if not path:
                return
            opts = {k: v.get() for k, v in self.bk.items()}
            opts["scope"] = "profile_supported_sections"
            try:
                opts["mem_start"] = int(self.ent_mem_s.get().strip(), 16)
                opts["mem_end"] = int(self.ent_mem_e.get().strip(), 16)
            except ValueError:
                messagebox.showwarning(APP_TITLE, "外部 FRAM 范围需为十六进制"); return

            def job(cat):
                def prog(i, n, msg, *a):
                    tail = f" {a[0]}/{a[1]}" if a else ""
                    self.status(f"{msg}{tail}  [{i + 1}/{n}]")
                obj = make_backup(cat, opts, progress=prog, cancel=self.cancel)
                save_json(path, obj, indent=1)
                self.last_backup = obj
                self._show_backup(obj)
                if obj.get("complete", False):
                    self.log(f"备份完成且无错误: {path}")
                else:
                    self.log(f"备份不完整：{len(obj.get('errors', []))} 项错误；请查看结果后不要当作完整备份: {path}")
            self.bg(job)

        def _show_backup(self, obj):
            if threading.current_thread() is not threading.main_thread():
                self.ui(lambda: self._show_backup(obj))
                return
            self.txt_bk.delete("1.0", "end")
            d = obj.get("data", {})
            self.txt_bk.insert("end", json.dumps(obj.get("model", {}), ensure_ascii=False) + "\n")
            self.txt_bk.insert("end", json.dumps(obj.get("transport", {}), ensure_ascii=False) + "\n")
            self.txt_bk.insert("end", f"created: {obj.get('created')}  complete: {obj.get('complete', 'legacy')}\n")
            self.txt_bk.insert("end", f"scope: {obj.get('scope', 'legacy')}  sections: {', '.join(obj.get('sections', []))}\n\n")
            for error in obj.get("errors", []):
                self.txt_bk.insert("end", f"ERROR: {error}\n")
            for k, v in d.items():
                if isinstance(v, dict) and "hex" in v:
                    self.txt_bk.insert("end", f"{k}: {v.get('bytes', v.get('len', len(v['hex']) // 2))} 字节\n")
                elif isinstance(v, dict) and "raw_hex" in v:
                    self.txt_bk.insert("end", f"{k}: {v['raw_hex']}\n")
                elif isinstance(v, dict) and "error" in v:
                    self.txt_bk.insert("end", f"{k}: 错误 {v['error']}\n")
                else:
                    self.txt_bk.insert("end", f"{k}: {type(v).__name__}\n")
            self.txt_bk.see("end")

        def _backup_model_matches(self):
            if not self.last_backup:
                return False
            saved = self.last_backup.get("model", {}).get("id")
            current = (self._profile_cfg() or {}).get("id")
            if not saved or not current or saved != current:
                messagebox.showerror(APP_TITLE,
                                     f"备份机型 ({saved or '未知'}) 与当前配置 ({current or '未选择'}) 不匹配，禁止恢复。")
                return False
            return True

        def do_backup_load(self):
            path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
            if not path:
                return
            try:
                obj = load_json(path)
                if obj.get("format") != BACKUP_FORMAT:
                    if not messagebox.askyesno(APP_TITLE, "格式标记不匹配，仍要载入？"):
                        return
                saved_model = obj.get("model", {}).get("id")
                current_model = (self._profile_cfg() or {}).get("id")
                if saved_model and current_model and saved_model != current_model:
                    messagebox.showwarning(APP_TITLE,
                                           f"该备份属于 {saved_model}，当前配置为 {current_model}。仅允许查看，不允许恢复。")
                self.last_backup = obj
                self._show_backup(obj)
                st = obj.get("data", {}).get("settings_table", {})
                if "hex" in st:
                    self.cal_table = bytes.fromhex(st["hex"])
                    self._render_cal(self.cal_table)
                self.log(f"已载入备份: {path}")
            except Exception as e:
                messagebox.showerror(APP_TITLE, f"载入失败: {e}")

        def do_restore_table(self):
            if not self.require_open(): return
            if not self.last_backup:
                messagebox.showwarning(APP_TITLE, "请先载入备份文件"); return
            if not self._backup_model_matches():
                return
            st = self.last_backup.get("data", {}).get("settings_table", {})
            if "hex" not in st:
                messagebox.showwarning(APP_TITLE, "备份中没有设置表"); return
            try:
                tb = bytes.fromhex(st["hex"])
            except ValueError as e:
                messagebox.showerror(APP_TITLE, f"备份设置表 hex 无效: {e}"); return
            if len(tb) != int(self._cfg('settings_table', 'length', 0) or 0):
                messagebox.showerror(APP_TITLE, f"备份设置表长度 {len(tb)} != {int(self._cfg('settings_table', 'length', 0) or 0)}，拒绝恢复")
                return
            if not self._require_backup_ack():
                return
            if not messagebox.askyesno(APP_TITLE,
                                       f"将把备份中的 {self._cfg('settings_table', 'length')} 字节设置表写回设备。\n这会改校准数据，可能导致异常。继续？"):
                return
            def job(cat):
                def prog(a, b): self.status(f"恢复设置表 {a}/{b} …")
                n = restore_table(cat, tb, progress=prog, cancel=self.cancel, verify=True)
                self.log(f"设置表恢复完成: {n} 字节，已校验")
            self.bg(job)

        def do_restore_fram(self):
            if not self.require_open(): return
            if not self.last_backup:
                messagebox.showwarning(APP_TITLE, "请先载入备份文件"); return
            if not self._backup_model_matches():
                return
            mm = self.last_backup.get("data", {}).get("mem", {})
            if "hex" not in mm:
                messagebox.showwarning(APP_TITLE, "备份中没有外部 FRAM 数据"); return
            try:
                start = int(mm["start"], 16)
                mem = bytes.fromhex(mm["hex"])
            except (KeyError, ValueError) as e:
                messagebox.showerror(APP_TITLE, f"备份 FRAM 数据无效: {e}"); return
            max_addr = int(self._profile_memory("address_max", -1))
            min_addr = int(self._profile_memory("address_min", 0))
            if not (min_addr <= start <= max_addr and start + len(mem) - 1 <= max_addr):
                messagebox.showerror(APP_TITLE, "备份 FRAM 地址/长度超出当前机型范围，拒绝恢复")
                return
            if not self._require_backup_ack():
                return
            if not messagebox.askyesno(APP_TITLE,
                                       "将把外部 FRAM 数据写回设备！\n这极其危险，可能导致无法开机。\n仍要继续？"):
                return
            if not messagebox.askyesno(APP_TITLE, "再次确认：写入外部 FRAM？"):
                return
            def job(cat):
                def prog(a, b): self.status(f"恢复外部 FRAM {a}/{b} …")
                n = restore_fram(cat, start, mem, progress=prog, cancel=self.cancel)
                self.log(f"外部 FRAM 恢复完成: {n} 字节")
            self.bg(job)

        # ---------------- Tab: 终端 ----------------
        def _tab_term(self):
            f = ttk.Frame(self.nb, padding=10)
            self.nb.add(f, text="终端")
            r1 = ttk.Frame(f); r1.pack(fill="x", pady=3)
            ttk.Label(r1, text="ASCII 命令:").pack(side="left")
            self.ent_ascii = ttk.Entry(r1, width=44)
            self.ent_ascii.pack(side="left", padx=4)
            self.btn_term_ascii = ttk.Button(r1, text="发送", command=self.do_ascii)
            self.btn_term_ascii.pack(side="left")
            ttk.Label(r1, text="(不含结尾 ; )").pack(side="left", padx=4)

            r2 = ttk.Frame(f); r2.pack(fill="x", pady=3)
            ttk.Label(r2, text="原始字节(hex):").pack(side="left")
            self.ent_hex = ttk.Entry(r2, width=44)
            self.ent_hex.pack(side="left", padx=4)
            self.btn_term_hex = ttk.Button(r2, text="发送", command=self.do_hex)
            self.btn_term_hex.pack(side="left")
            self.var_auto_semi = tk.BooleanVar(value=True)
            ttk.Checkbutton(r2, text="自动补 ';'", variable=self.var_auto_semi).pack(side="left", padx=6)

            r3 = ttk.Frame(f); r3.pack(fill="x", pady=3)
            ttk.Label(r3, text="读取:").pack(side="left")
            self.ent_readn = ttk.Entry(r3, width=8); self.ent_readn.insert(0, "256"); self.ent_readn.pack(side="left", padx=4)
            ttk.Label(r3, text="字节 / 或读到分号").pack(side="left")
            ttk.Button(r3, text="纯读", command=self.do_pureread).pack(side="left", padx=6)
            ttk.Button(r3, text="清空记录", command=lambda: self.txt_term.delete("1.0", "end")).pack(side="left")

            ttk.Label(f, text="帧构造助手(SP 子协议):").pack(anchor="w", pady=(6, 0))
            r4 = ttk.Frame(f); r4.pack(fill="x", pady=3)
            ttk.Label(r4, text="SP body(hex):").pack(side="left")
            self.ent_spbody = ttk.Entry(r4, width=30)
            self.ent_spbody.pack(side="left", padx=4)
            ttk.Button(r4, text="构造→原始框", command=self.do_build_sp).pack(side="left")
            ttk.Label(r4, text="例: 57 0123 4567 ⟶ SPW 0x0123=0x45,0x67",
                      foreground="#666").pack(side="left", padx=6)

            self.txt_term = self._mktext(f, 20)
            self.txt_term.pack(fill="both", expand=True, pady=(6, 0))

        def do_ascii(self):
            if not self.require_open(): return
            cmd = self.ent_ascii.get()
            def job(cat):
                r = cat._ascii(cmd, expect=None, timeout=1.0)
                self._q_append_text(self.txt_term, f"> {cmd};\n< {hexs(r)}\n")
            self.bg(job)

        def do_hex(self):
            if not self.require_open(): return
            try:
                data = bytes.fromhex(self.ent_hex.get().replace(" ", ""))
            except ValueError as e:
                messagebox.showwarning(APP_TITLE, f"hex 解析失败: {e}"); return
            if self.var_auto_semi.get() and not data.endswith(b";"):
                data += b";"
            def job(cat):
                cat.link.write(data)
                r = cat.link.drain(1.0)
                self._q_append_text(self.txt_term, f"> {data.hex()}\n< {hexs(r)}\n")
            self.bg(job)

        def do_pureread(self):
            if not self.require_open(): return
            try:
                n = int(self.ent_readn.get())
            except ValueError:
                n = 256
            def job(cat):
                r = cat.link.read_exact(n, timeout=cat._t_for(n, 1.0)) if n > 0 else cat.link.drain(1.0)
                self._q_append_text(self.txt_term, f"< ({len(r)}B) {hexs(r)}\n")
            self.bg(job)

        def do_build_sp(self):
            try:
                body = bytes.fromhex(self.ent_spbody.get().replace(" ", ""))
            except ValueError as e:
                messagebox.showwarning(APP_TITLE, f"hex 解析失败: {e}"); return
            try:
                frame = sp_frame(body)
            except CatError as e:
                messagebox.showwarning(APP_TITLE, str(e)); return
            self.ent_hex.delete(0, "end")
            self.ent_hex.insert(0, frame.hex())
            self.txt_term.insert("end", f"SP frame = {frame.hex()}   (校验 {frame[-2]:02X})\n")
            self.txt_term.see("end")

    app = App()
    app.mainloop()


# ============================================================ CLI
