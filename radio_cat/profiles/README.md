# 机型配置

程序只加载此目录中的 `*.json` 机型文件；不会在代码中回退到内置机型默认值。

## 主要字段

- `id`, `name`, `cat_id`, `status`, `notes`: 机型身份与证据状态
- `ve_magic`: VE 查询串；`service_code`: JP 与 ZZ 共用的 4 位机型代码（如 `0010`、`0891`）。程序按通用协议生成 `JP{service_code}` 与 `ZZPE{service_code}`。JP 写入数据值不是机型代码；未由可靠资料确认安全值或存在未验证副作用时，不配置、不启用写入。
- `serial`: `baud_default`, `data_bits`, `parity`, `stop_bits`, `flow_control`, `evidence`
- `responses`: 各 CAT 命令回包长度
- `fram`: 外部 FRAM 地址范围与配置对
- `power`: 配置字节位掩码、功率等级映射、TS 频段组合表与扩展范围；可选 `ac_write` 声明用标准 CAT `EX` 写入的功率上限项（`value_digits`、`items`（`label`/`addr`）、`targets`（按功率等级标签给出等长目标值），`items` 与每个 `targets` 数组长度必须一致，`addr` 为偶数位十六进制）
- `settings_table`: 内部设置/校准表基址、起止地址和长度
- `dumps`: E0/E8 转储长度；未知值必须是 `null`
- `service_menu` / `service_mode`: 项目数、索引/子码地址、入口操作码与风险说明
- `pe_calibration`: 选择子与可选参数地址范围；`selector_positions` 为 A/B/C 三个位置各自的合法值（如 `[[0,2],[0,1,2],[0,1,2]]`），PE 值按 3 字符读写（`+05`/`-03`/`010`）。`selectors` 仅作为旧版回退。
- `capabilities.kt_read` / `kt_write`: 机型不支持 KT 时设为 `false`，`responses.kt_length` 设为 `null`。`capabilities.spa_rd` 表示 SPARD 命令存在；通常需要先进入服务模式，默认备份不采集。
- `data_files`: 相对于应用包目录的机型数据文件路径
- `capabilities`: 功能开关。只有资料足够支持的能力才设置为 `true`。`cat_commands` 表示资料中识别到的命令名，不代表该操作已验证安全；UI 功能开关单独控制是否开放。

ASCII CAT 命令如果没有确认的定长回包长度，可使用终止符读取；定长二进制转储长度未知时，相关按钮会禁用。

功率页写入功率等级与功率上限（AC）：等级位改 `FRAM[0x0007]`，上限按 `power.ac_write` 用标准 CAT `EX` 写入。`EX` 上限受设备当前档位夹取，未配置 `ac_write` 的机型只写等级位。`FRAM 全表` 编辑器仍按地址写入任意字节。`power.exp_note`、`power.ts_note` 与 `power.band_frequency_hints` 仅用于界面提示，不参与位运算；`power.ts_invalid` / `power.ts_conditional` 用于标注已知无效或条件有效的 TS 值；`band_frequency_hints` 是通用业余频段参考，不代表本机开放范围。

配置文件中的静态证据不等于真机验证。本发行包不包含测试目录或测试脚本。


### 配置对地址与缺省值

`fram.config_pair_addr` 是实际读写配置对的地址；`power.config_pair_addr` 仅用于界面显示，二者必须一致，`validate_profile` 会检查。程序不提供内置机型默认值；位掩码、等级表、地址等未确认字段应显式填写或保持 `null`/缺省，相关能力关闭。
