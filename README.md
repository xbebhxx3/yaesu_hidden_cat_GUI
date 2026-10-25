# Yaesu Hedden CAT

Yaesu Hedden CAT 是配置驱动的 Yaesu CAT 桌面工具。面向维护用的隐藏cat命令，隐藏串口与通用 CAT/SP 帧处理、功率，频率范围等配置、文件操作 每个机型的数据分离存储。

> 仅供个人研究使用，使用请尊重所在国家的法律法规，产生的一切影响与开发者无关；责任自行承担。
## 启动

需要 Python 3.10+、Tkinter 和 pyserial：

```bash
python -m pip install -r requirements.txt
python main.py
```

无界面备份示例（端口和波特率按电台实际设置修改）：

```bash
python main.py --model ft891 --port /dev/ttyUSB0 --baud 38400 --backup backup.json
```

FT-891 配置未声明通用默认波特率，因此命令行必须明确指定 `--baud`；示例中的 `38400` 只是命令格式示例，不是软件替设备选择的默认值。

## 目录

- `main.py`：唯一应用启动入口
- `radio_cat/core.py`：通用串口、CAT 命令和 SP/SPR/SPW 协议
- `radio_cat/file_ops.py`：备份、恢复及 JSON/二进制文件操作
- `radio_cat/profiles.py`：机型配置加载、结构校验和机型数据加载
- `radio_cat/ui/main_window.py`：桌面界面
- `radio_cat/profiles/*.json`：每个机型独立配置
- `radio_cat/profiles/profile-template.json.example`：新增机型模板
- `radio_cat/data/<model>/`：机型的 FRAM 映射、服务菜单数据与当前结论（`CONCLUSIONS.md`）

## 目前能力

1. 已实现全部已知的维护cat命令。
2. 读改TS(频率范围)，TS=2包括5mhz业余段，
3. 临时EXP（扩频模式），RESET恢复或软件中关闭。
4. 读写功率等级 100w 50w 20w 10w，并按机型写入功率上限（AC）。
5. 读写整个FRAM。
6. 备份用户设置，FRAM，TS PWR等设置。


## 配置及文档依据

- FT-891 与 FTDX10 的外部 FRAM 都是 32 KiB，地址范围 `0x0000–0x7FFF`。该容量边界来自两个机型各自的固件研究文档。
- `FRAM 全表` 的悬停提示显示区域作用、已知可用数值、证据与置信度；十六进制编辑框按光标所在字节定位。全表写入按行地址执行，接受当前机型 FRAM 范围内的任意地址/字节；SP 帧按固定长度解析。
- 功率页写入功率等级与功率上限（AC）。功率等级为 `FRAM[0x0007]` bit 3–4（`0x18`=100 W、`0x00`=50 W、`0x08`=20 W、`0x10`=10 W）；上限用标准 CAT `EX` 写入（FTDX10 为 MAX POWER，FT-891 为各模式功率上限）。`EX` 上限按设备当前档位夹取，超出会返回 `?;`；刚改过档位时需整机断电重开后再写。RF POWER（F）仍由面板旋钮或 `PC` 设置。
- `TS` 是 `FRAM[0x0007]` bit 0–2 的频段组合选择，和 bit 3–4 的功率等级是两个独立字段。FTDX10 提供专用 TS/EXP 写入；FT-891 的 TS 位保留在 FRAM 中，但本软件不通过专用控件改写。EXP 是临时状态：设备复位或设置版本迁移时会按 JP1006/EXP 跳线状态覆盖，不能作为持久修改。
- 未能确认用户用途的低层触发不会作为普通功能按钮呈现。原始命令能力/编码若有必要，留在对应机型配置和协议层；新增型号不得复制另一型号的专属值冒充已验证。

## 运行限制与安全说明

已在FTDX10完成部分实机器只读验证；隐藏 CAT 写入、输出功率与 FT-891 的全部行为仍需在对应机型及固件上验证。

所有 FRAM/校准/维护写入都有风险。写入前先保存并核对备份；不得把静态逆向结果等同于真机确认。

### JP 写入安全状态

`1234EDCB` 是 FTDX10 隐藏 CAT 文档中的命令格式示例（两个 16 位字互补），不是默认值。JP 写入语义已由固件静态确认：

- FTDX10 `JP0010<8hex>`：8hex 为 `v` + `~v`；`v` 低 5 位写入 `FRAM[0x0007]` bit0–4（TS/功率），bit5（EXP）保留、bit6–7（AUX）清零；`FRAM[0x0006]` 清零；随后写回 FRAM[0x0006/0x0007]，并把 RAM `0xFF9886` 的 5 字节目标参数写到 `FRAM[0x02F0–0x02F4]`。不复位，但副作用超出功率页范围。
- FT-891 `JP0891<8hex>`：8hex 为 `v` + `~v`；`v` 低 5 位写入 `FRAM[0x0007]` bit0–4（TS/功率），bit5–7 保留；`FRAM[0x0006]` 原样写回；不复位。

两款机型的 JP 写入均未做真机验证；本软件功率页只用 SPW 写功率等级位，JP 写入保持禁用

### PE 校准（TX AUDIO EQ/P-EQ）

两款机型的 `PE` 都是 TX AUDIO EQ/P-EQ，不是 RF 功率校准。选择子 `A`=0/2（EQ/P-EQ）、`B`=0/1/2（1/2/3 段）、`C`=0/1/2（FREQ/LEVEL/BWTH）。值格式：FREQ/BWTH 为 3 位十六进制，LEVEL 为符号 + 2 位十六进制。

- FT-891：静态确认对应 `FRAM[0x0233–0x0244]`；完整地址表、值域见 `radio_cat/data/ft891/CONCLUSIONS.md`。FT-891 PE 仍保持禁用。

PE 页按 `selector_positions` 生成 18 个组合；写入值接受 `+05`、`-03`、`010`。写入前先记录当前回包并备份相关 FRAM。

## FTDX10 服务菜单

FTDX10 的“服务菜单”页依据 MAIN V01-15 静态分析启用：内部项目索引范围为 `N=0x000–0x125`（294 项），索引/子码分别对应 FRAM `0x0344–0x0345` 与 `0x0346`；项目源值为 371 字节设置块中的 `0x000A + N`。项目映射文件位于 `radio_cat/data/ftdx10/service_items.csv`。

当前 `radio_cat/data/ftdx10/service_items.csv` 已包含 MAIN V01-15 静态导出的 294 项完整标签、频段与参数，不再留空或显示“标签未提供”。已根据文档给出的地址公式生成项目源值地址、特殊 11 字节区、共享 54 字节区和 VDD/ALC/IDD 表头映射。

`SPN` 选择项目会立即应用该项目的波段/模式，可能改变当前工作状态；服务模式入口命令可能导致软复位。校准写入前应备份。上述内容是 MAIN V01-15 固件静态分析映射；实机 MAIN V01-14 尚未逐项验证。FT-891 的服务菜单表已在固件中定位（226 项，`0xE7BA`，25B/项；值在 `FRAM[0x000A+N]`），但本软件的服务菜单页按 FTDX10 的索引/子码模型实现，FT-891 仍未开放。
