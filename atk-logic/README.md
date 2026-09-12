# 01 - ATK-Logic 逻辑分析仪 MCP (v2, 2026-09-11)

通过 USB 控制正点原子 **ATK-Logic-Analyzer** (VID_1A86 / PID_FFCC, WCH+FPGA)，
把逻辑分析仪封装成 MCP 工具给 agent 用。协议由厂商源码逆向 + 真机实测校准。

- 桥版本: **2.0 (2026-09-11)**  ← v1 的频率/边沿统计有系统性错误（见下文「v2 修了什么」）
- 结构: `atkproto.py`(协议+设备层, 单一事实来源) / `atkdl.py`(`.atkdl` 读写) / `srdhost.py`(sigrok 解码器宿主)
  / `server.py`(MCP 工具) / `smoke_test.py`(真机自检) / `test_offline.py`(无硬件单测) / `test_atkdl.py` / `test_srdhost.py` / `test_mcp.py`(MCP 端到端)

## 工具清单

| 工具 | 说明 |
|------|------|
| `identify` | 识别设备 (型号/序列号) |
| `status` | 设备连接状态 + 桥版本 + 重连次数 + 最近错误 |
| `reset` | 强制重建 USB 句柄（采集报 I/O 错误 EIO 时用，**不必再重启 MCP**） |
| `pwm` | 控制设备自带 PWM 输出（channel=0/1），自测用 |
| `capture` | 采单通道：统计 + 波形(下采样) + 电平变化点 + order-3 偏移 |
| `capture_multi` | **一次采集拿多通道统计**（硬件本来就同时回 16 通道，不加耗时） |
| `save_capture` | **全量**存盘 CSV + BIN + JSON 元数据（默认目录见 `ATK_SAVE_DIR`，未设则 `<工具目录>/captures/`）；`fmt="atkdl"` 直接写厂商格式 |
| `load_waveform` | **离线**分析 `.atkdl` / 裸 `.bin`（不占设备）：会话摘要 + 统计 + 跳变 + 可选 UART |
| `decode_uart` | 采集并解码 UART（8N1，baud=0 → 自动波特率） |
| `list_protocols` | 列出可用的 sigrok 协议解码器（208 个） |
| `decode_protocol` | 跑 sigrok 解码器（照 `.atkdl` 官方配置 / 手工通道选项 / `live=True` 采一次再解） |
| `bus_decode` | **MCU 总线调试**：采/读 → 解码 → **按时间排序的事务列表**；`probe=True` 先逐通道摸接线 |
| `send_raw` | 逃生口：直接下发帧命令，便于后续逆向 |

`capture` / `capture_multi` / `save_capture` / `decode_uart` / `bus_decode` 公共参数：
`depth`(该通道采样点数, 8 的倍数) · `sample_rate_mhz`(1/2/4/5/10/20/25/40/50/100/**200**) ·
`threshold_v` · `trigger`(rising/falling/any/high/low) · `mode`(buffer|trigger) · `timeout_s` ·
`rle`(True 让设备回 **RLE 压缩载荷**, 桥自动展开, 省 90%+ USB 带宽; 返回附压缩率 —— 见 PROTOCOL-NOTES §4.5)

## MCU 总线调试怎么用（`bus_decode`）

```python
# ① 不知道线接在哪个通道：先摸一遍（16 通道画像：边沿/频率/占空比/UART 自动试解）
bus_decode(probe=True, sample_rate_mhz=10, depth=2000000, trigger="any")

# ② 知道接线就直说，直接出事务列表（live=True 会顺手把波形存成 .atkdl 当证据）
bus_decode(protocol="i2c", channels="scl=3,sda=4", sample_rate_mhz=10, depth=2000000)
bus_decode(protocol="spi", channels="clk=0,miso=1,mosi=2,cs=3", sample_rate_mhz=50, depth=2000000)
bus_decode(protocol="uart", channels="rx=0", options="baudrate=115200", sample_rate_mhz=10)
bus_decode(protocol="modbus", stack="uart")          # 栈式：uart → modbus

# ③ 分析已有存档（不用设备）
bus_decode(path="docs/波形存档/xxx.atkdl", live=False)
```

返回的 `text` 就是能直接读的事务列表，例如官方 I²C 样例解出来是：

```
    0.0400 ms  [i2c           ] Start
    0.0400 ms  [i2c           ] 0x50 WR: 00 53
    0.0400 ms  [eeprom24xx    ] Byte write (addr=00, 1 byte): 53
    0.0511 ms  [i2c           ] Address write: 50
```

（同一份样例其实有 4 次写：`00→53, 01→54, 02→4D, 03→33`，`max_items` 调大就能看全。）

**注意响应体积**：`capture` 的 `max_points<=0` = 完全不回 `time_s/level` 数组（只看统计时用它，
否则 40 万点的采集会回 2 MB）；`bus_decode` 默认也不回完整的逐行 `layers`，要看才加 `include_layers=True`。

采样率怎么选（**≥5 个采样/位** 才稳）：I²C 100kHz→1MHz 档、400kHz→4/5MHz 档；
SPI 常用 10~25MHz（时钟多快就至少多快）；UART 115200→≥1MHz；ADC/PWM 包络→1~10MHz。
窗口 = `depth / 采样率`：10MHz × 2M 点 = 200ms，够看一帧 I²C/SPI 事务或几行 UART。

## 自检 / 回归

```bash
PY=D:/MCP/01-atk-logic/.venv/Scripts/python.exe
cd D:/MCP/01-atk-logic/atk-logic

$PY test_offline.py     # 无硬件: 触发编码/帧解析/统计/UART 解码/CRC/互斥/抓包解析器 (51 项)
$PY smoke_test.py       # 真机: 17 项 (需 CH0 接着设备自带 PWM0 输出)
$PY test_mcp.py         # MCP 端到端 (走 stdio 调工具, 需设备空闲)
$PY tools/debug_trigger_semantics.py   # 触发/模式语义复现实验
$PY tools/debug_uart.py                # UART 解码边界复现实验
$PY tools/usbcap_extract.py <pcap>     # USB 抓包 → 可读命令/回传 (协议考古, 见 tools/usbcap/README.md)
```

> ⚠️ 任何真机脚本运行时，**不要让别的东西占用设备**：Hermes / Claude Code 里注册的
> atk-logic MCP 进程会占用它（MCP 是懒加载，首次调用工具后才 claim）。
> v2 有两道保险：① 跨进程互斥会直接报"设备已被 PID xxx 占用"（不会再写出 EIO/脏数据）；
> ② 若真的发生 I/O 错误，调一次 `reset` 即可恢复（不用重启 MCP）。

## 实测事实（重要，别再踩）

1. **一次采集永远包含全部 16 通道**；通道使能位只影响触发条件，不影响数据。要哪个通道就解哪个通道的位流。
2. **请求深度之外的数据是垃圾**：录满后设备仍会继续吐 2 采样抖动的残流。
   v1 把垃圾尾巴一起算统计 → 频率虚高 3%~15%（每档凭空多 ~28 个上升沿）。
   v2 按 `depth` 截断，并在"请求通道都够采样"时立刻停止读取。
3. **频率用周期中位数算**，不要用 `上升沿数 / 时长`。另给 `frequency_hz_edgecount` 作对照。
4. **触发条件在本机固件上不生效**：`mode="trigger"` 会下发厂商协议里的"等待触发"标志，
   但恒定电平（PWM0 占空比 0%）也照样录满缓冲。`mode` 只决定下发哪个协议字段。
5. **时基是准的**：同一路 1kHz 信号在 1MHz / 25MHz 档测得周期 = 1000 / 25000 采样（精确）。
   `order-3` 是 **87 字节结构**（`ff 00` + 5B 位置 + 16×5B 每通道数据量 + 尾字节），
   其中首个 5B 在 Buffer 模式 = 触发深度+8、非 Buffer 模式 ≈ 16 —— **不是**绝对触发时刻。
6. ~~**RLE 模式不支持**~~ → **已支持**（`rle=True`，桥自动展开；算法与实测见 PROTOCOL-NOTES §4.5）。
7. **设备是独占资源**：两个进程同时开 → EIO/脏数据。v2 加了跨进程互斥，第二个进程会直接被告知
   "设备已被 PID xxx 占用"（不必再靠猜）。`ATK_ALLOW_MULTI=1` 可强制并存（仅调试）。

## 波形存档 `.atkdl`（厂商上位机格式：能读也能写）

官方样例就在 `C:\Program Files\ATK-Logic\test\`（11 个：UART/I2C/SPI/CAN/Modbus/SWD/WS2812/DHT11/MIPI DSI/USB PD/PWM），
**每个都带官方解码器配置**，可以当离线测试向量（不插分析仪）。

| 工具 | 说明 |
|---|---|
| `load_waveform(path, channel=-1, uart_baud=…)` | **离线**分析 `.atkdl`/裸 `.bin`（不占设备）：会话摘要 + 统计 + 跳变 + 可选 UART 解码 |
| `save_capture(..., fmt="atkdl")` | 采集后直接写厂商格式；**ATK-Logic 上位机能打开**（实测 2026-09-11） |
| `atkdl.py` | 读写实现（单一事实来源）；`test_atkdl.py` 是它的回归套件（73 项） |
| `tools/atkdl_dump.py` / `tools/atkdl_probe.py` | 解剖样例 / 快速试读 |

格式要点（全部实测，踩过就记住了）：

1. 文件 = **ZIP**：顶层 `channel.ini` / `set.ini` / `vernier.ini` + 一个"毫秒时间戳"名条目（解码器配置 JSON）；
   每通道 `N/channel.ini` + `N/<偏移>-<序号>.bin`（1 MiB 一片，按序号排序拼接）。
2. **采样率真值在 `set.ini` 的 `settingData.setHz`（Hz）**；顶层 `channel.ini` 的 `SamplingFrequency` 是**kHz**
   （差 1000 倍，最容易踩）。`SamplingDepth == setHz × setTime(ms)/1000` 可自校验。
3. 通道级 `N/channel.ini` **第 3 行 = 该通道有效采样数**；数据可能比他长（设备录满后继续吐流，上位机照存）
   或比他短（官方几个大样例本身就是截断的）→ 读时按它截断。
4. 每字节 8 采样、**LSB 在前**；行尾是 `\r\r\n`（厂商 Windows 文本模式写的，照抄别"修好"）。
5. **16 个通道每个都必须有 `channel.ini`**（空的也要有，valid=0）——少一个，上位机就弹
   "读取通道N配置文件失败, 已经跳过"（这个错是我们实测打开自己写的文件时才发现的）。
6. 厂商上位机与我们的 MCP **不能同时开设备**（WinUSB 独占）：上位机会弹"读取设备权限不足"。
   要看波形就先把 MCP 那份进程关掉。

## 协议解码：208 个 sigrok 解码器（I²C/SPI/CAN/Modbus/SWD/USB PD/WS2812/DHT11…）

厂商上位机自带的协议解码器就是 **sigrok `libsigrokdecode` 的 Python PD**（官方文档明说"无需修改"），
本包用纯 Python 复刻了宿主框架（`srdhost.py`），因此**这 208 个解码器可以直接在 MCP 里用**，
不用装 sigrok、不用它的驱动。

| 工具 | 说明 |
|---|---|
| `decode_protocol(protocol, path=…, from_saved=True)` | 解码波形。**默认照 `.atkdl` 里官方存的配置**（通道/选项/解码链全照抄） |
| `decode_protocol(protocol="i2c", channels="scl=7,sda=6", options="address_format=shifted", path=…)` | 手工指定通道/选项 |
| `decode_protocol(protocol="spi", channels="clk=0,miso=1,mosi=2,cs=3", live=True, depth=400000, sample_rate_mhz=25)` | 采一次再解 |
| `list_protocols(filter="")` | 列出可用的解码器 |
| `srdhost.py` / `test_srdhost.py` | 宿主实现 / 回归套件（81 项）；`tools/srd_all.py` 批量跑官方样例 |

支持**栈式链路**（上层协议吃下层吐的包）：`uart → modbus`、`i2c → 24xx EEPROM`、`spi → SPI flash`，
用 `stack="uart"` 指定，或者直接 `from_saved=True` 让它自己按 PD 的 `inputs` 元数据推导。

实测（`test_srdhost.py`，2026-09-11，全部离线、不插分析仪）：

| 官方样例 | 解码链 | 解出内容（节选） |
|---|---|---|
| `uart_tx_115200.atkdl` | uart | `ALIENTEK` × 7 个包 |
| `eeprom_24c02_i2c.atkdl` | i2c → eeprom24xx | `0x50 WR: 00 53`；`Byte write (addr=00, 1 byte): 53` |
| `spi_flash_w25q128.atkdl` | spi → spiflash | MOSI `81 ff ff ce 7f…`；MISO 数据流 |
| `modbus.atkdl` | uart → modbus | `Slave ID: 1 / Function 1: Read Coils` |
| `can_500K.atkdl` | can | `Identifier: 18 (0x12)`；`Data: 0x98 … 0x9F` |
| `pwm_10M_30_25.atkdl` | pwm | `10.000 MHz` / `30.000000%` |
| `rgb_led_ws2812.atkdl` | rgb_led_ws281x | `(hex)RGB#202020` |
| `AM230x_DHT11.atkdl` | am230x | `Humidity: 55.0 %` / `Temperature: 30.0 °C` / `Checksum: OK` |
| `swd.atkdl` | swd | `IDCODE 0x1ba01477`、DP 读写序列 |
| `USB PD.atkdl` | usb_power_delivery | `SOURCE CAP`；`[1] [Fixed] 5V 3A (15W)` |
| `MIPI_DSI_lP.atkdl` | mipi_dsi | `Escape mode entry/ESC`、LP 数据 |

在线路径也验过：PWM0 输出 1 kHz 采集后交 PWM 解码器 → `1.000 kHz / 50.000000%`。

宿主实现要点（踩过的坑都在这）：`wait()` 条件支持 `{}`（取当前采样点）、`None`（前进一格）、
`{pin: 'l'/'h'/'r'/'f'/'e'}`、`{'skip': N}`（超时，按当前位置起算）、**列表 = OR 且 `self.matched[i]` 标记命中项**；
匹配点用"段边界归并 + 二分"跳着找（~14M 采样/秒）；采样率通过老 API `pd.metadata('samplerate', v)` 下发；
注解/二进制类名允许是**字符串**（厂商 fork 的 PD 会这么用）；`.atkdl` 存档含触发前长空闲 →
窗口自动从"首个跳变前"开始，解不到时自动放大窗口重试。

## 与厂商上位机的对照（2026-09-11 USBPcap 抓包，已闭环）

抓包方式与结论见 `tools/usbcap/README.md`；要点：

| 项 | 厂商上位机 ATK-Logic V1.1.2.1 | 本包 v2 |
|---|---|---|
| 采集模式 | **非 Buffer**（flags=0x00） | 默认 Buffer（flags=0x80），可选 `mode="trigger"` |
| 采样率/深度 | 20MHz / 10,000,000（触发深度 100,000） | 参数化 |
| 触发载荷 | 8 对通道全 0xFF（16 通道任意变化）+ `00 00` = **10 字节** | 8 对 + 1 个立即标志 = **9 字节**（都能启动采集） |
| 采集前 | `0x87 唤醒` + PWM0 自测（1MHz）+ `0x10 GetDeviceData` | 唤醒 + 可选用自带 PWM 自检 |
| 结束 | `0x15 Stop` | 同 |
| 读取方式 | 16 KB URB，逐记录 2048 对齐解交织 | pyusb 4096B 读 + 2048 对齐解交织（同构） |

已解掉的"未解项"：order-3 结构、厂商真实参数、触发字节编码核对、厂商自测/唤醒序列。
仍未解：触发载荷第 10 个字节、触发条件为何在本机不构成数据闸门。（RLE 已解，见 PROTOCOL-NOTES §4.5）

## 协议要点

见 `PROTOCOL-NOTES.md`（帧格式、命令码、触发字节编码全部来自厂商源码 `pv/usb/usb_control.cpp`
与 `pv/static/util.cpp::triggerStringToByte`，并逐条真机验证）。

## 部署

1. 装 64 位 Python 3.10+；插上逻辑分析仪。
2. **驱动**：右键管理员运行 `atk-logic\drivers\install_driver.bat`（用 Zadig 装 WinUSB，
   VID_1A86 PID_FFCC）。自动化失败就手动用 Zadig 选设备装 WinUSB。
3. `install.bat`（创建 venv + 装依赖 + 注册到 Claude Code）：
   或手动注册到 Hermes，在 `config.yaml` 里加
   ```yaml
   mcp_servers:
     atk-logic:
       command: D:\MCP\01-atk-logic\.venv\Scripts\python.exe
       args: [D:\MCP\01-atk-logic\atk-logic\server.py]
       enabled: true
   ```
4. 验证：`$PY smoke_test.py` 全绿，或让 agent 调 `capture`（CH0 应看到 ~1kHz 方波）。

## v2 修了什么（相对 v1）

| # | 问题（v1） | 修法（v2） |
|---|---|---|
| 1 | USB 报 EIO 后全局句柄不复位，之后所有调用全失败，必须重启 MCP | `AtkDevice` 全局锁 + `USBError` 自动 dispose/重枚举/重试；新增 `reset` 工具 |
| 2 | 触发字节只发 2 字节（仅 CH0/CH1 那对），其余通道从未配置 | 按厂商 `triggerStringToByte` 语义生成 8 对字节 + 立即标志，`channel` 真正参与触发编码 |
| 3 | 统计把深度之外的垃圾数据一起算 → 频率/边沿数系统性偏高 | 按 `depth` 截断 + 增量解析 + "读够即停"（也更快） |
| 4 | `save_capture` 只存下采样点，还有一段死代码 | 全量 CSV + 原始 BIN + JSON 元数据，支持一次存多通道 |
| 5 | `test_mcp.py` 硬编码 `D:\AI\Claude\...` 与 `C:\Python314`（本机已不存在） | 路径自动定位（相对 `__file__` + 就近 venv），并能开箱跑通 |
| 6 | 无多通道/无协议解码/`channel` 传参不生效 | 新增 `capture_multi`、`decode_uart`(自动波特率)、`send_raw` |
| 7 | 统计口径不透明 | 新增 `glitch_edges`/`narrow_pulses`/周期 min-med-max、`transitions` 变化点、`complete` 完整性标记 |
| 8 | 两个进程同时开设备 → EIO/脏数据（9-03 实际踩过） | **跨进程单实例互斥**（Windows 命名 Mutex / POSIX flock）：第二个进程直接报"设备已被 PID xxx 占用"，不再写坏数据；进程退出自动释放，`ATK_ALLOW_MULTI=1` 可强制并存 |

## 常见坑

- **必须共地**：被测信号地与逻辑分析仪地不连，读数是悬空噪声。
- 采集前**不要发 RestartMCU**，直接唤醒即可（v2 已封装）。
- 采样数据**每字节 8 个采样，LSB 在前**（bit0 = 时间上最早）。
- 探头悬空/接触不良的典型现象：读数恒定 + 零星 1 采样毛刺 → 先查线再查固件。
