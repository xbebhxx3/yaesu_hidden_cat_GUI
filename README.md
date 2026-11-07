# Yaesu Hedden CAT

Yaesu Hedden CAT 是配置驱动的 Yaesu CAT 桌面工具。面向维护用的隐藏cat命令，隐藏串口与通用 CAT/SP 帧处理、功率，频率范围等配置、文件操作 每个机型的数据分离存储。

> 仅供个人研究使用，使用请尊重所在国家的法律法规，产生的一切影响与开发者无关；责任自行承担。
## 启动

需要 Python 3.10+、Tkinter 和 pyserial：

```bash
cd src
python -m pip install -r requirements.txt
python main.py
```

无界面备份示例（端口和波特率按电台实际设置修改）：

```bash
python main.py --model ft891 --port /dev/ttyUSB0 --baud 38400 --backup backup.json
```

FT-891 配置未声明通用默认波特率，因此命令行必须明确指定 `--baud`；示例中的 `38400` 只是命令格式示例，不是软件替设备选择的默认值。

## 可执行文件

生成物在release

重新打包：

```bash
./packaging/build-linux.sh     # Linux
./packaging/build-windows.sh   # Windows（在 Linux 上通过 Wine 构建）
```

`build-windows.sh` 在 `build/wineprefix/` 中安装 Windows Python 3.12 + Tkinter，再运行 PyInstaller，需要 `wine`、`winepath`、`curl`、`rsync`。下载的安装包、虚拟环境、Wine 前缀和构建中间文件都放在项目内的 `build/`打包后可整体删除。

## 目录

- `src/main.py`：应用启动入口
- `src/radio_cat/core.py`：通用串口、CAT 命令和 SP/SPR/SPW 协议
- `src/radio_cat/file_ops.py`：备份、恢复及 JSON/二进制文件操作
- `src/radio_cat/profiles.py`：机型配置加载、结构校验和机型数据加载
- `src/radio_cat/ui/main_window.py`：桌面界面
- `src/radio_cat/profiles/*.json`：每个机型独立配置
- `src/radio_cat/profiles/profile-template.json.example`：机型模板
- `src/radio_cat/data/<model>/`：机型的 FRAM 映射与服务菜单数据
- `packaging/`：PyInstaller spec 与 Linux/Windows 打包脚本
- `dist/`、`build/`：构建产物与构建缓存（已忽略，不入库）

## 目前能力

1. 已实现全部已知的维护cat命令。
2. 读改TS(频率范围)，TS=2包括5mhz业余段，
3. 临时EXP（扩频模式），RESET恢复或软件中关闭。
4. 读写功率等级 100w 50w 20w 10w；FTDX10 同时写入功率上限（AC），FT-891 只写等级。
5. 读写整个FRAM。
6. 备份用户设置，FRAM，TS PWR等设置。


## 配置及文档依据

- FT-891 与 FTDX10 的外部 FRAM 都是 32 KiB，地址范围 `0x0000–0x7FFF`。该容量边界来自两个机型各自的固件研究文档。
- `FRAM 全表` 的悬停提示显示区域作用、已知可用数值、证据与置信度；十六进制编辑框按光标所在字节定位。全表写入按行地址执行，接受当前机型 FRAM 范围内的任意地址/字节；SP 帧按固定长度解析。
- 功率页写入功率等级与功率上限（AC）。功率等级为 `FRAM[0x0007]` bit 3–4（`0x18`=100 W、`0x00`=50 W、`0x08`=20 W、`0x10`=10 W）；上限按机型配置 `power.ac_write` 用 SPW 成对写外部 FRAM（FTDX10 为 MAX POWER `FRAM[0x046A–0x046D]`），写后固件重载设置块立即生效、不受档位区间夹取，写完回读核对。功率等级仍需整机断电重开；FT-891 只写等级，不写最大功率。RF POWER（F）仍由面板旋钮设置。功率等级与 TS/EXP 写入只校验 CAT ID 并弹出写入确认，写入后自动回读验证。
- `TS` 是 `FRAM[0x0007]` bit 0–2 的频段组合选择，和 bit 3–4 的功率等级是两个独立字段。FTDX10 提供专用 TS/EXP 写入；FT-891 的 TS 位保留在 FRAM 中，但本软件不通过专用控件改写。EXP 是临时状态：设备复位或设置版本迁移时会按 JP1006/EXP 跳线状态覆盖，不能作为持久修改。

## 安全说明

已在FTDX10完成实机器验证； FT-891 的全部行为仍待验证。

所有 FRAM/校准/维护写入都有风险。写入前先保存并核对备份。

### JP 写入安全状态

`1234EDCB` 是 FTDX10 隐藏 CAT 文档中的命令格式示例（两个 16 位字互补），不是默认值。JP 写入语义已由固件静态确认：

- FTDX10 `JP0010<8hex>`：8hex 为 `v` + `~v`；`v` 低 5 位写入 `FRAM[0x0007]` bit0–4（TS/功率），bit5（EXP）保留、bit6–7（AUX）清零；`FRAM[0x0006]` 清零；随后写回 FRAM[0x0006/0x0007]，并把 RAM `0xFF9886` 的 5 字节目标参数写到 `FRAM[0x02F0–0x02F4]`。不复位，但副作用超出功率页范围。
- FT-891 `JP0891<8hex>`：8hex 为 `v` + `~v`；`v` 低 5 位写入 `FRAM[0x0007]` bit0–4（TS/功率），bit5–7 保留；`FRAM[0x0006]` 原样写回；不复位。

两款机型的 JP 写入均未做真机验证，现已开放：写前校验 v/w 互补与设备身份、要求“已备份”勾选确认，写后回读核对。JP 会同时改写 TS/功率位并有副作用（见机型配置 `jp_write_note`），不要用它替代功率页的等级写入。

### PE 校准（TX AUDIO EQ/P-EQ）

选择子 `A`=0/2（EQ/P-EQ）、`B`=0/1/2（1/2/3 段）、`C`=0/1/2（FREQ/LEVEL/BWTH）。值格式：FREQ/BWTH 为 3 位十六进制，LEVEL 为符号 + 2 位十六进制。

- FT-891：静态确认对应 `FRAM[0x0233–0x0244]`（RAM `0xFF8DB3–0xFF8DC4`）；参数地址与值域见机型配置 `pe_calibration`。FT-891 PE 仍保持禁用。

PE 页按 `selector_positions` 生成 18 个组合；写入值接受 `+05`、`-03`、`010`。写入前先记录当前回包并备份相关 FRAM。

## FTDX10 服务菜单

FTDX10 的“服务菜单”页依据 MAIN V01-15 静态分析启用：内部项目索引范围为 `N=0x000–0x125`（294 项），索引/子码分别对应 FRAM `0x0344–0x0345` 与 `0x0346`；项目源值为 371 字节设置块中的 `0x000A + N`。项目映射文件位于 `src/radio_cat/data/ftdx10/service_items.csv`。

当前 `src/radio_cat/data/ftdx10/service_items.csv` 是 294 项的编号→标签骨架：保留项目编号（`code_hex`/`code_dec`）、标签与 RAM/FRAM 地址映射（含特殊 11 字节区、共享 54 字节区和 VDD/ALC/IDD 表头的 `apply_kind`）；`b0–b5`、频段、分组等内容列默认 `null`（未填）。

`SPN` 选择项目会立即应用该项目的波段/模式，可能改变当前工作状态；服务模式入口命令可能导致软复位。校准写入前应备份。上述内容是 MAIN V01-15 固件静态分析映射；实机 MAIN V01-14 尚未逐项验证。FT-891 的服务菜单表已在固件中定位（226 项，`0xE7BA`，25B/项；值在 `FRAM[0x000A+N]`），但本软件的服务菜单页按 FTDX10 的索引/子码模型实现，FT-891 仍未开放。
