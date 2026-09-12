# ATK-Logic-Analyzer 协议笔记（厂商源码 + 真机实测双校准）

设备: `USB\VID_1A86&PID_FFCC` (WCH 芯片, WinUSB), 接口0, 端点 OUT `0x02` / IN `0x81` (512B 包)。
厂商源码: `github.com/alientek-openedv/atk-logic`（Qt 上位机, GPL-3.0）——下述格式均引自
`pv/usb/usb_control.cpp` 与 `pv/static/util.cpp`，并逐条在本机真机验证。

---

## 1. 命令发送（端点 0x02, bulk out）

### 1.1 裸命令（MCU 直接识别；发 512 字节，不足补零）
`[0x0A][code][data...]`

| code | 含义 | 备注 |
|---|---|---|
| 0x80 | 进 bootloader | data = `"ATK-LOGIC-ANALYZER"` |
| 0x81 | GetMCUVersion | 响应 `0a 81 <数据> <填充>` |
| 0x82/0x83 | 硬件升级 (MCU) | 升级用，勿碰 |
| 0x84 | RestartMCU | **采集前不要发** |
| 0x85/0x86 | 硬件升级 (FPGA) | 升级用，勿碰 |
| 0x87 + state | SetResetState | 1=唤醒 FPGA / 0=休眠（采集前发 1 + drain） |
| 0x88 | GetResetState | |

### 1.2 帧命令（有 CRC32，`USBControl::Write`）
载荷 `[code][len-1][data...]`（len-1 = data 长度 + 1），
整帧 `[8×0x00][0x0A][载荷][0x0B][CRC32-LE]`，补零到 2048 对齐后**交织**发。

| code | 用途 | data |
|---|---|---|
| 0x10 | GetDeviceData | — |
| 0x11 | ParameterSetting | 采集配置（见 §2） |
| 0x12 | SimpleTrigger | 启动采集（见 §3） |
| 0x15 | Stop | — |
| 0x17 | PWM 控制 | 见 §5 |
| 0x18 | Exit | — |

### 1.3 CRC32（`util.cpp::gCRC32`）
反射表 `0xEDB88320`、初值 `0`、末异或 `0xFFFFFFFF` —— **不是** zlib 标准 CRC32。
（本包 `tests`/`test_offline.py` 有对照断言。）

### 1.4 交织（`logic_analyzer_convert_to_device`）
2048 字节块分 4 条 lane（各 512B）：`out[lane*512 + 2j] = src[(4j+lane)*2 .. +1]`。
发送与接收（≥2048 的块）都这样处理；**<2048 的短响应不解交织**。

---

## 2. 采集配置 `0x11` 的 data（`setArray`）

| 偏移 | 含义 |
|---|---|
| [0] | flags：bit7=**Buffer 模式**，bit6=**RLE 压缩**（算法已解，见 §4.5） |
| [1] | 电压阈值：bit7=负号，低 7 位 = \|V\|×10（1.5V → 0x0F） |
| [2] | hzIndex+1（采样率档位下标+1） |
| [3..7] | 采样深度（5B LE，单位=采样点） |
| [8..12] | 触发深度（5B LE） |

采样率档位表：`[1, 2, 4, 5, 10, 20, 25, 40, 50, 100, 200]` MHz（hzIndex = 下标+1）。
**第 11 档 200 MHz 是实测确认的**（2026-09-11）：厂商存档里出现过 `setHz=200000000、selectHzIndex=10`
（`pwm_10M_30_25.atkdl`），本机发 hzIndex=11 设备接受，采 PWM0 1 kHz 得周期**正好 200000 采样**
（若设备其实跑 100 MHz，同一信号会算成 2000 Hz）→ 档位真实存在，复现脚本 `tools/verify_rate_200mhz.py`。

---

## 3. 启动采集 `0x12` 的 data（`SimpleTrigger`）

本包按 `util.cpp::triggerStringToByte` 语义构造：**每字节管一对通道**（高半字节=偶数通道 2k，
低半字节=奇数通道 2k+1），最多 8 对字节，**末尾再跟 1 个"立即采集"标志字节**：

```
偶数通道(2k): bit7=使能  bit4=上升沿  bit5=下降沿  bit6=高电平
奇数通道(2k+1): bit3=使能  bit0=上升沿  bit1=下降沿  bit2=高电平
未列出的通道 = 不使能 (对应位 0)；低电平触发 = 只置使能位
"任意变化/双沿" = 上升|下降|高电平 = 0x70（+使能 = 0xF0）
末字节: 0x01 = 立即采集, 0x00 = 等触发条件
```

厂商源码片段（`util.cpp`）：

```cpp
for (QChar i : text) {                 // 每字符 = 一个通道的触发类型
    if (channelArray[index]["enable"].toBool()) b += (ii%2==0 ? 128 : 8);
    if (i=='R')       b += (ii%2==0 ? 16 : 1);   // 上升沿
    else if (i=='1')  b += (ii%2==0 ? 64 : 4);   // 高电平
    else if (i=='F')  b += (ii%2==0 ? 32 : 2);   // 下降沿
    else if (i=='0')  {}                          // 低电平
    else if (i=='C')  { b += (ii%2==0?16:1); b += (ii%2==0?32:2); }   // 双沿
    else              { b += (ii%2==0?16:1); b += (ii%2==0?32:2); b += (ii%2==0?64:4); } // 随机
    ...
}
```

> **实测（本机固件）**：即使末字节给 `0x00`（等触发），设备也会立即录满缓冲并回传；
> PWM0 输出占空比 0%（恒定低电平、无跳变）时同样回满数据。
> 即**触发条件在这台设备上不构成数据闸门**；`mode` 参数只决定下发哪个协议字段。
> v1 只发 2 字节（等于只配了 CH0/CH1 那一对），其余通道的触发配置从未下发。

---

## 4. 响应流（端点 0x81, bulk in）

- 短响应（<2048）不解交织，直接 `[0x0A][order][data]`
- ≥2048 的块需解交织后按帧解析
- **实测（USBPcap 抓厂商上位机流量）**：厂商软件按 **16 KB URB** 读，每条 16 KB 记录自身是 2048 对齐的
  → 逐记录解交织即可；512 字节的短响应**不解交织**（混在一起按整条流解会错位）

帧格式：`[0x0A][order][len 2B LE][data][0x00][0x0B]`

| order | 含义 |
|---|---|
| 1 | 采样数据：`data = [channelID][1 字节保留][样本字节...]`，每字节 = 该通道 8 个采样，**LSB 在前**（bit0 最早） |
| 3 | 采集信息（**87/88 字节结构，实测已解**，见 §4.1） |
| 4 | 命令应答（`ff 00 <cmd> 03`） |
| 5 | 采集进度（**只在 Buffer 模式出现**；非 Buffer 模式无此帧——实测） |
| 6 | 传输完成 |

### 4.1 order-3 的真实结构（厂商抓包实测）

厂商上位机一次 10M 采样/20MHz 采集里的 order-3 载荷（87 字节）：

```
ff 00 | 10 00 00 00 00 | (c5 09 00 00 00) × 16 | 00
 ↑应答头   ↑首个 5B 字段             ↑每通道一个 5B              ↑尾字节
```

- 首个 5 字节字段：**非 Buffer 模式 ≈ 16**（触发点就在记录起点附近）；**Buffer 模式 = 触发深度 + 8**
  （我们自己的抓取：深度 200000 → 100008）。即它**不是**绝对触发时刻，只是设备报的记录/触发位置。
- 之后 16 个 5 字节值是**每通道数据量**（上例 16 通道各 2501；对应每通道 1,250,016 字节 → 单位约 500 字节）
- 末尾 1 字节（`00`）

> 我们 v2 工具里 `trigger_offset_samples` 就是上面那个"首个 5B 字段"，语义已按本节说明，切勿当绝对触发时刻用。

### 4.2 实测补充（v2 修正的关键）
1. **一次采集回全部 16 通道**（每通道各若干 order-1 帧），通道使能位不影响回传内容。
2. **请求深度之外是垃圾**：录满后设备继续吐 2 采样抖动的残流。按 `depth` 截断后
   统计才干净；v1 没截断 → 频率虚高（每档多算约 28 个上升沿）。
3. **时基精确**：同一路 1kHz 信号，1MHz 档周期=1000 采样、5MHz 档=5000、25MHz 档=25000。
   与 25MHz 档 115200bps UART 的位宽 215 采样（≈24.8MHz）自洽。
4. **触发条件不构成数据闸门**（本机固件）：非 Buffer 模式 + "等待触发"标志 + **PWM0 已停（真无跳变）**，
   仍然回满请求深度（重复两次一致）。厂商上位机用的是非 Buffer + 全通道"任意变化"。
5. ~~**RLE 模式（flags bit6）不支持**~~ → **已解并实现**（见 §4.5；上一轮"厂商上位机源码里没有解码器"的结论是错的，
   解压器在 `pv/thread/thread_work.cpp:172-182`，当时只 grep 了部分目录）。

### 4.3 厂商上位机（ATK-Logic V1.1.2.1）实际下发的序列（USBPcap 抓包）

```
0a 87 01                                  裸命令: 唤醒 FPGA
0a 17 11 c8 00 00 00 64 00 00 00          帧命令 0x17: PWM0 启动 maxHz=200 dutyCnt=100 (自测信号)
0a 17 20 / 0a 17 10                       帧命令 0x17: PWM1 停 / PWM0 停 (CloseAllPWM, 与厂商源码一致)
0a 10                                     帧命令 0x10: GetDeviceData
0a 11 00 10 06 80 96 98 00 00 a0 86 01 00 00   帧命令 0x11: flags=0x00(非Buffer) 阈值1.6V
                                                hzIndex=6(20MHz) 深度10,000,000 触发深度100,000
0a 12 ff×8 00 00                          帧命令 0x12: 8 个通道对全 0xFF (=16 通道全使能"任意变化")
                                                + **尾部 2 字节 0x00 0x00**（本包发 9 字节: 8 对+1 标志；两种都能被接受）
0a 15                                     帧命令 0x15: Stop
```

对照结论：
- 厂商**默认非 Buffer 模式**（flags bit7=0），本包默认 Buffer 模式；两种模式本机都能出数据，
  非 Buffer 时无 order-5 进度帧、order-3 首字段是小值。
- 厂商的**自测流程**是 PWM0 1MHz → 采集 → 停 PWM，本包 `smoke_test.py` 用 PWM0 1kHz 做自检（同思路）。
- 触发载荷厂商发 **10 字节**（8 对 + 2 个 0x00），我们发 9 字节（8 对 + 1 个立即标志）——
  都能启动采集；多出的那个字节语义未定（疑为保留/超时）。

### 4.4 官方文档与可复用资产（2026-09-11 发现）

- **官方教程**：`http://www.openedv.com/ATK-Prod/ATK-Logic/docs/index.html`
  （「逻辑分析仪协议解码教程」，Sphinx；含参数说明 / sigrok 介绍 / 如何编写解码器（UART + 上层协议示例））
  - 注意：`www.openedv.com` 在本机解析到 `198.18.0.13`（代理软件的假 IP 段），
    Hermes 的 web 抓取器会以"私网地址"拦掉 → **用 `curl -sSk -x http://127.0.0.1:7897` 走系统代理取**。
- **官方明说**：ATK-Logic 的协议解码器 = **sigrok `libsigrokdecode` 的 Python 解码器**，可直接用、无需修改；
  正点原子只改了框架源码（fork：`github.com/alientek-openedv/atk_libsigrokdecode`，GPL-3.0，2026-08 仍更新）。
- **本机现成资产**：
  - `C:\Program Files\ATK-Logic\decoders\` —— **209 个 sigrok 协议解码器源码**（`<proto>/pd.py`，Python3.7 环境）
  - `C:\Program Files\ATK-Logic\test\*.atkdl` —— **11 个官方样例波形**（UART 115200 / I2C 24C02 / SPI W25Q128 /
    CAN 500K / Modbus / SWD / WS2812 / AM230x-DHT11 / MIPI DSI LP / USB PD / PWM），**每个都含官方解码结果 JSON**
  - `python37.dll` + `python37.zip` —— 厂商软件用内嵌 Python 跑这些 PD（框架在 C 侧）
- **`.atkdl` 格式**（已完整逆向，读写实现在 `atkdl.py`）：ZIP，含顶层 `channel.ini` / `set.ini` /
  `vernier.ini` + 一个"毫秒时间戳"名条目（**解码器配置 JSON**：`{协议名: {annotation_rows, channels,
  opt_channels, options, binary…}, "main": {…}}`，不是解码结果数据），以及每通道
  `N/channel.ini` + `N/<偏移>-<序号>.bin`（1 MiB 一片，按序号排序拼接）。
  - **采样率**：`set.ini` 的 `settingData.setHz` = Hz（真值）；顶层 `SamplingFrequency` = kHz（差 1000 倍）。
    `SamplingDepth = setHz × setTime(ms)/1000` 自校验。
  - **通道级 `N/channel.ini` 第 3 行 = 有效采样数**（写盘时 16 个通道都要有这个文件，空的 valid=0）。
  - 数据每字节 8 采样、LSB 在前；文件行尾是 `\r\r\n`。
  - 物理量交叉验证（`test_atkdl.py`，73 项）：官方 `uart_tx_115200.atkdl` → 10 MHz 档实测位宽 86 采样
    = 116279 baud（115200 + 0.94% 器件晶振偏快）并解出 `ALIENTEKALIENTEK…`；
    `pwm_10M_30_25.atkdl` → 10.000 MHz / 30.00%（与 `pwmData` duty=30 吻合）；
    `can_500K.atkdl` → 最短脉冲 200 采样@100MHz = 500k；`rgb_led_ws2812.atkdl` → 位周期 50 采样@40MHz = 800k。
  - 厂商上位机能打开我们写的文件（实测 `docs/波形存档/demo_pwm1k_1MHz.atkdl`：波形正常显示；
    早期漏写空通道的 `channel.ini` 会弹"读取通道N配置文件失败"，已修）。

### 4.5 RLE 压缩（`0x11` 载荷 flags bit6）——已解并实现（2026-09-11）

> 厂商源码本地副本：`D:/MCP/01-atk-logic/vendor-src`（shallow clone，commit `0dff562`，2024-05-13，77 MB）
> —— 以后逆向字段先在这里全树 grep，别再只看 `pv/usb`。

**算法（厂商源码即权威：`pv/thread/thread_work.cpp:172-182`）**：

```cpp
if(isRLE){
    memset(rleBuffer,0,rleSize);  qint32 current=0;
    for (qint32 i = 2; i < analysisData.ullLen; i+=2){        // 从偏移 2 开始，每 2 字节一组
        memset(rleBuffer+current, analysisData.pData[i+1], (quint8)analysisData.pData[i]);
        current += (quint8)analysisData.pData[i];
    }
    data_=rleBuffer;  datalen=current;
}
```

即 order-1 载荷 = `[通道号][保留]` + **[(count, value)…]**（两者各 1 字节）：
`count = 1..255`，单位是**字节**（8 个采样），`value` 是该段所有采样的字节值；
展开 = 把 `value` 重复 `count` 次。载荷长度为奇数时最后一个孤立字节忽略。
`count` 上限 255 → 更长的游程拆成连续多对（同值），解码端自然拼接。

标志位与厂商一致（`session_controller.cpp:231-235`）：`b += 128`(Buffer) / `b += 64`(RLE)。

**本包实现**：`atkproto.decode_rle()` / `encode_rle()`（反向，供单测）；`FrameStream(rle=True)`
边收边展开（上层拿到的采样/统计与不压缩**完全一致**，只是省 USB 带宽）；
`parameter_setting_data(..., rle=True)`；MCP 工具 `capture/capture_multi/save_capture/decode_uart/bus_decode`
都有 `rle=False` 参数，返回里附 `rle_pairs / wire_bytes / expanded_bytes / compression_pct`。

**真机实测**（`tools/verify_rle.py`，PWM0 1kHz 50% @1MHz、depth=400000）：

| 项 | 实测 |
|---|---|
| 设备确实回 RLE 对 | `pairs=1616`（线上 3232 字节 vs 展开 50001 字节，**省 93.5%**）|
| 展开字节数 | 精确 `50000` = depth/8 |
| 结构校验 | 800 个"跳变字节" = 2×400 周期；间距 ∈ {62,63}（周期 125 字节 = 1000 采样）；每个跳变字节内部恰好 1 个跳变 |
| 统计 | 频率 **1000.0 Hz**、占空比 **50.0%**（与不压缩采集一致）|
| 对照 | 不压缩：纯电平字节数一致（49200 = 49200）；线上 782784 字节 → RLE 3232 字节 |
| 静态信号（PWM0 停） | 832 字节（**省 99%**）、展开全 0、仍回满 depth |

> 注意：**跳变字节**是正常现象——1 kHz@1 MHz 的边沿落在字节中间（如 `0x1F`/`0xFE`），
> 一个周期 = 62 + 1 + 61 + 1 = 125 字节。别把这种"长度 1 的游程"当噪声。

---

## 5. PWM 控制（`0x17`，设备自带输出，自测用）

- PWM0 启动：`data = [0x11][maxHz 4B LE][dutyCnt 4B LE]`，
  `maxHz = round(200000000 / hz)`，`dutyCnt = round(maxHz × duty% / 100)`
- PWM0 停止：`data = [0x10]`
- PWM1 启动：`data = [0x21]...`；PWM1 停止：`data = [0x20]`
- 占空比 0% → 输出**恒定低电平**（无跳变，可当"无信号"信号源用）
- 实测：设 1kHz 时，1/5/25MHz 三档测得周期都是 1000/5000/25000 采样 → 输出频率与
  采样时基同源、比例精确

---

## 6. 已验证的完整流程（v2 实现）

1. `0x87=1` 唤醒 → drain
2. `0x11` setArray = `[0x80|flags, 阈值, hzIndex+1, 深度5B, 触发深度5B]`
3. `0x12` = `[8 对通道字节][立即标志]`
4. 循环读 0x81 → 按 2048 对齐解交织 → 增量解析帧 → 累计各通道样本字节
5. **请求通道都够 depth 采样 → 立刻停止读取**（避免垃圾尾巴，也更快）
6. 每通道按 `depth` 截断 → 位流（LSB 在前）→ 统计/UART 解码

## 7. 仍未解 / 后续可做

- **RLE 载荷编码**（flags bit6）：**已解**（见 §4.5）。
- `0x12` 触发载荷尾部的**第 2 个标志字节**（厂商发 10 字节，我们发 9 字节）语义未定。
- `order-3` 里 16 个"每通道 5 字节量"的**确切单位**（实测 ≈ 每 500 字节计 1）。
- 触发条件为何在本机固件上不构成数据闸门（厂商上位机同样用"等待触发"却没被卡住 →
  这更像固件行为而非我们的用法错误；换固件/换机器可复测）。

### 已解（本次 USBPcap 对照抓包闭环，见 §4.1 / §4.3）
- ✅ `order-3` 的字节结构
- ✅ 厂商软件的真实采集参数（非 Buffer、20MHz、10M 深度、触发深度 100k）
- ✅ 触发字节编码与厂商一致（8 对通道字节 + 标志；厂商多 1 个保留字节）
- ✅ 厂商的自测/唤醒流程（0x87 唤醒、PWM0 自测、0x10 GetDeviceData、0x15 Stop）
