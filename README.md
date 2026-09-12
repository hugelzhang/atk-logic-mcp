# ATK-Logic-MCP

把正点原子 **ATK-Logic-Analyzer** 逻辑分析仪（USB `VID_1A86 / PID_FFCC`）封装成 **MCP 服务器**，
让 Claude Code、Hermes 等任意 MCP 客户端直接采集、解码、分析数字波形。

通信协议由**厂商源码逆向 + 真机逐条实测**校准（见 [`atk-logic/PROTOCOL-NOTES.md`](atk-logic/PROTOCOL-NOTES.md)），
不是靠猜：帧格式、命令码、触发字节编码、RLE 压缩算法都能在厂商源码里找到对应实现。

> 非官方项目，与正点原子 / ALIENTEK 无关联。

## 能做什么

| 能力 | 说明 |
|---|---|
| **采集** | 16 通道、1~200 MHz 采样率、单次最多千万点；单通道波形+统计，或一次采集拿多通道统计（硬件本来就同时回 16 通道，不加耗时） |
| **统计可信** | 按 `depth` 截断 + 用**周期中位数**算频率 + 毛刺/窄脉冲计数。早期版本把超出深度的残流一起统计过，频率会虚高 3%~15%，已修 |
| **总线调试** | `bus_decode` 一行出**按时间排序的事务列表**；不知道线接在哪就用 `probe=True` 逐通道摸（边沿/频率/占空比/UART 自动试解） |
| **208 个协议解码器** | 厂商上位机自带的就是 sigrok 的 Python PD，本包复刻了宿主框架，因此这 208 个解码器**直接在 MCP 里可用**，不用装 sigrok、不用它的驱动 |
| **栈式链路** | `uart → modbus`、`i2c → eeprom24xx`、`spi → spiflash` 等分层解码（按 PD 的 `inputs` 元数据自动推导或手动指定） |
| **波形存档** | 读厂商 `.atkdl` 存档（含官方解码器配置，可当离线测试向量）；也能**写出**上位机可以直接打开的文件 |
| **RLE 压缩** | 深窗口采集开 `rle=True` 走设备侧压缩，省 90%+ USB 带宽：200 MHz × 8M 点从 15.9 MB / 3.5 s 降到 100 KB / 0.1 s |
| **离线分析** | 不插设备也能分析 `.atkdl` / 裸 `.bin`：会话摘要 + 统计 + 跳变 + UART 解码 |

## 安装

1. **Python**：64 位 3.10+（安装时勾 "Add Python to PATH"）
2. **驱动**：插入逻辑分析仪 → **管理员身份**运行 `atk-logic\drivers\install_driver.bat`（用 Zadig 装 WinUSB）。
   自动化失败就手动用 Zadig 给 `VID_1A86 PID_FFCC` 装 WinUSB。
3. **装依赖 + 注册**：双击 `install.bat`（建 venv、装依赖、注册 MCP `atk-logic`）

手动注册到 Hermes，在 `config.yaml` 里加：

```yaml
mcp_servers:
  atk-logic:
    command: D:\MCP\01-atk-logic\.venv\Scripts\python.exe
    args: [D:\MCP\01-atk-logic\atk-logic\server.py]
    enabled: true
```

验证：`python smoke_test.py` 全绿，或让 agent 调一次 `capture`（设备自带 PWM0 接到 CH0 应看到 ~1 kHz 方波）。

## 工具清单（13 个）

| 工具 | 说明 |
|------|------|
| `identify` | 识别设备（型号 / 序列号 / USB ID） |
| `status` | 连接状态 + 桥版本 + 重连次数 + 最近错误 |
| `reset` | 重建 USB 句柄（采集报 `EIO` 时用，**不必重启 MCP**） |
| `pwm` | 控制设备自带 PWM 输出（自测用） |
| `capture` | 采单通道：统计 + 下采样波形 + 电平变化点 |
| `capture_multi` | 一次采集拿多通道统计 |
| `save_capture` | **全量**存盘 CSV + 原始 BIN + JSON 元数据；`fmt="atkdl"` 写厂商格式 |
| `load_waveform` | **离线**分析 `.atkdl` / 裸 `.bin`（不占设备） |
| `decode_uart` | 采集并解码 UART（8N1，`baud=0` 自动测波特率） |
| `list_protocols` | 列出可用的 sigrok 解码器（208 个） |
| `decode_protocol` | 跑 sigrok 解码器（照存档里的官方配置 / 手工指定通道 / 采一次再解） |
| `bus_decode` | **MCU 总线调试**：采集或读存档 → 解码 → 事务列表 |
| `send_raw` | 逃生口：直接下发帧命令，便于后续逆向 |

公共参数：`depth`（该通道采样点数，8 的倍数）· `sample_rate_mhz`（1/2/4/5/10/20/25/40/50/100/200）·
`threshold_v` · `trigger`（rising/falling/any/high/low）· `mode` · `timeout_s` · `rle`

## 典型用法

```python
# ① 不知道线接在哪个通道：先摸一遍
bus_decode(probe=True, sample_rate_mhz=10, depth=2000000, trigger="any")

# ② 知道接线就直接出事务列表（live=True 顺手把波形存成 .atkdl 当证据）
bus_decode(protocol="i2c",  channels="scl=3,sda=4", sample_rate_mhz=10, depth=2000000)
bus_decode(protocol="spi",  channels="clk=0,miso=1,mosi=2,cs=3", sample_rate_mhz=50)
bus_decode(protocol="uart", channels="rx=0", options="baudrate=115200", sample_rate_mhz=10)
bus_decode(protocol="modbus", stack="uart")        # 栈式：uart → modbus

# ③ 分析已有存档，不占用设备
bus_decode(path="docs/波形存档/xxx.atkdl", live=False)
```

返回的 `text` 就是能直接读的事务列表：

```
    0.0400 ms  [i2c           ] Start
    0.0400 ms  [i2c           ] 0x50 WR: 00 53
    0.0400 ms  [eeprom24xx    ] Byte write (addr=00, 1 byte): 53
    0.0511 ms  [i2c           ] Address write: 50
```

实测样例（全部离线、不插分析仪，套件 `test_srdhost.py`）：

| 官方样例 | 解码链 | 解出内容（节选） |
|---|---|---|
| `uart_tx_115200.atkdl` | uart | `ALIENTEK` × 7 个包 |
| `eeprom_24c02_i2c.atkdl` | i2c → eeprom24xx | `0x50 WR: 00 53`；`Byte write (addr=00, 1 byte): 53` |
| `spi_flash_w25q128.atkdl` | spi → spiflash | MOSI `81 ff ff ce 7f…` |
| `modbus.atkdl` | uart → modbus | `Slave ID: 1 / Function 1: Read Coils` |
| `can_500K.atkdl` | can | `Identifier: 18 (0x12)`；`Data: 0x98 … 0x9F` |
| `AM230x_DHT11.atkdl` | am230x | `Humidity: 55.0 %` / `Temperature: 30.0 °C` / `Checksum: OK` |
| `swd.atkdl` | swd | `IDCODE 0x1ba01477`、DP 读写序列 |
| `USB PD.atkdl` | usb_power_delivery | `SOURCE CAP`；`[1] [Fixed] 5V 3A (15W)` |
| `MIPI_DSI_lP.atkdl` | mipi_dsi | `Escape mode entry/ESC`、LP 数据 |

采样率怎么选（**≥5 个采样/位**才稳）：I²C 100 kHz→1 MHz 档、400 kHz→4/5 MHz 档；
SPI 常用 10~25 MHz（时钟多快就至少多快）；UART 115200→≥1 MHz；PWM/ADC 包络→1~10 MHz。
窗口 = `depth / 采样率`（10 MHz × 2M 点 = 200 ms）。

## 实测事实（重要，别再踩）

1. **一次采集永远包含全部 16 通道**；通道使能位只影响触发条件，不影响数据。
2. **请求深度之外的数据是垃圾**：录满后设备仍会继续吐带 2 采样抖动的残流，必须按 `depth` 截断。
3. **频率用周期中位数算**，不要用「上升沿数 / 时长」（另给 `frequency_hz_edgecount` 作对照）。
4. **触发条件在本机固件上不生效**：`mode="trigger"` 会下发厂商协议里的"等待触发"标志，
   但恒定电平（占空比 0%）也照样录满缓冲。`mode` 只决定下发哪个协议字段。
5. **时基是准的**：同一路 1 kHz 信号在 1 MHz / 25 MHz 档测得周期精确等于 1000 / 25000 采样。
6. **设备是独占资源**：两个进程同时开会出 `EIO` / 脏数据。本包有跨进程互斥，
   第二个进程会直接被告知"设备已被 PID xxx 占用"（`ATK_ALLOW_MULTI=1` 可强制并存，仅调试）。
7. 厂商上位机与 MCP **不能同时开设备**（WinUSB 独占），看波形前先把 MCP 进程关掉。

## 可靠性 / 回归

```bash
PY=.venv/Scripts/python.exe          # Windows
cd atk-logic
$PY test_offline.py    # 无硬件：触发编码/帧解析/统计/UART/RLE/CRC/互斥     68 项
$PY smoke_test.py      # 真机：设备识别/PWM 自测/档位一致/多通道/存盘/重连  17 项
$PY test_mcp.py        # MCP 端到端（走 stdio 调工具）                      23 项
$PY test_atkdl.py      # .atkdl 读写回归                                    73 项
$PY test_srdhost.py    # sigrok 解码器宿主 + 8 个官方样例事务列表          117 项
```

> ⚠️ 跑真机脚本时别让别的东西占用设备：MCP 是懒加载，首次调用工具后才 claim 设备。

## 目录结构

```
atk-logic/
  atkproto.py     协议层 + 设备层（单一事实来源：帧格式、参数、统计、RLE、CRC32）
  atkdl.py        .atkdl 厂商格式读写
  srdhost.py      sigrok 解码器宿主（208 个 PD、栈式链路、事务列表）
  server.py       MCP 服务器（13 个工具）
  smoke_test.py   真机自检     test_offline.py / test_mcp.py / test_atkdl.py / test_srdhost.py
  tools/          真机专项验证脚本（200MHz 档位 / RLE / 总线解码 / 叠层链路）
  drivers/        WinUSB 驱动安装（Zadig）
  docs/波形存档/  随仓库保存的波形样例（只有 demo_pwm1k_1MHz.atkdl 被测试引用）
  captures/       运行时自动存证目录（默认，已 gitignore；可用 ATK_SAVE_DIR 改）
  PROTOCOL-NOTES.md / TEST-REPORT.md
```

## 文档

| 文档 | 内容 |
|---|---|
| [`atk-logic/README.md`](atk-logic/README.md) | 详细用法、v1→v2 修了什么、`.atkdl` 格式要点、踩坑清单 |
| [`atk-logic/PROTOCOL-NOTES.md`](atk-logic/PROTOCOL-NOTES.md) | 协议逆向笔记：命令码、帧结构、触发编码、RLE、CRC32 |
| [`atk-logic/TEST-REPORT.md`](atk-logic/TEST-REPORT.md) | 实测报告：真机 17 项、200 MHz 档位查证、RLE 13 项、UART 100% 逐字节复核 |

## 已知限制

- **触发条件不构成数据闸门**（本机固件行为，已记录在案）：靠 Buffer 模式录满 `depth` 再看。
- 采集依赖设备侧缓冲，超长事件要看窗口大小上限（用 `rle=True` 可显著拉长窗口）。
- 协议解码依赖厂商安装目录里的 sigrok 解码器；本仓库不包含这些 PD 文件。

## 第三方与许可

- `atk-logic/drivers/zadig.exe`：第三方工具（GPLv3），仅用于装 WinUSB 驱动，来源 <https://zadig.akeo.ie/>。
- `atk-logic/libusb-1.0.dll`：libusb（LGPL-2.1），pyusb 的后端。
- 协议解码器运行时从本机厂商安装目录加载，未随本仓库分发。
- 本项目自身**尚未选定开源许可证**。
