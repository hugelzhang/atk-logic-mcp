# 01 - ATK-Logic 逻辑分析仪 MCP

通过 USB 控制正点原子 **ATK-Logic-Analyzer**(VID_1A86 / PID_FFCC,WCH 芯片),把逻辑分析仪封装成 MCP 工具给 Claude 用。

## 工具清单

| 工具 | 说明 |
|------|------|
| `identify` | 识别设备 (型号/序列号/固件) |
| `status` | 设备连接状态 |
| `pwm` | 控制逻辑分析仪自带 PWM0 输出 (可自测) |
| `capture` | 采集指定通道数字波形 (0-15 通道, 1-100 MHz) |
| `save_capture` | 采集并保存波形到 CSV |

## 部署步骤

1. **装 Python**: 64 位 Python 3.10+,安装时勾选 **Add Python to PATH**。
2. **装驱动**: 插入逻辑分析仪 → 右键管理员运行 `atk-logic\drivers\install_driver.bat`(自动用 Zadig 装 WinUSB)。若自动化失败,手动用 Zadig 选设备装 WinUSB(VID_1A86 PID_FFCC)。
3. **安装**: 双击 `install.bat`(创建 venv + 装依赖 + 注册 MCP `atk-logic`)。
4. **验证**: `claude mcp list` 应显示 atk-logic 已连接。

## 自检

用逻辑分析仪自带 **PWM0 输出**接到 CH0 自测:
- 问 Claude: “逻辑分析仪 PWM0 输出 1kHz 50%,然后采集 CH0”
- 应采集到 ~1kHz 方波,高电平约 50%

## 协议要点 (PROTOCOL-NOTES.md)

- 采样率档位: 1/2/4/5/10/20/25/40/50/100 MHz
- 必须用 Buffer 模式采集;SimpleTrigger 用 2 字节 dataBytes
- 采样数据 LSB 在前,每字节 8 采样

## 常见坑

- **必须共地**: 被测信号地与逻辑分析仪地不连,读数悬空抓不到。
- 采集前不要发 RestartMCU,直接唤醒即可。
- 中文/自定义协议帧 `[AA][55][LEN][PAYLOAD][XOR]` 已实测 100% 解码。
