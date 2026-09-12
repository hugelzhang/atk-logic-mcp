# 协议考古：用 USB 抓包对照厂商上位机（已完成，结论见 TEST-REPORT §5）

**结论摘要（2026-09-11 抓包闭环）**：`order-3` 结构已解；厂商软件真实参数已知（非 Buffer、20MHz、
10M 深度、触发深度 100k）；触发字节编码与厂商核对一致（厂商多 1 个保留字节）；
厂商自测/唤醒序列已知（0x87 唤醒 → PWM0 1MHz 自测 → 0x10 GetDeviceData → 0x11 → 0x12 → 0x15 Stop）。
仍未解：RLE 编码、触发载荷第 10 字节、触发条件为何在本机不构成数据闸门。

- 证据文件：`vendor_capture.pcap`（21 MB，含厂商完整连接+采集+停止序列）
- 解析脚本：`tools/usbcap_extract.py`（一条命令把 pcap 还原成"发了什么/回了什么"）

## 复现步骤

### 1. 起抓包（需要管理员/UAC —— 唯一需要人工授权的一步）

`USBPcapCMD` **非管理员会静默退出并留下 0 字节 pcap**（本机实测症状）。
本目录的 `capture_session.ps1`（**纯 ASCII**，别写中文：Windows PowerShell 按 ANSI 读 .ps1，
中文会让脚本解析失败、退出码 1）会对 USBPcap1..4 同时起抓包，并等 `STOP` 标志文件出现后停抓：

```powershell
# 普通(非提权) PowerShell 里执行, 会弹 UAC:
Start-Process -Verb RunAs -FilePath powershell -ArgumentList `
  '-NoProfile','-ExecutionPolicy','Bypass','-File','D:\MCP\01-atk-logic\atk-logic\tools\usbcap\capture_session.ps1'
# 停止: 创建空文件 STOP (脚本会等 3 秒让缓冲落盘再杀进程)
New-Item -ItemType File -Path 'D:\MCP\01-atk-logic\atk-logic\tools\usbcap\STOP' -Force
```

要点：
- **必须同时抓多个根集线器**：本机 logic analyzer 挂在 `ROOT_HUB30 (PCI 51ED)` 上，
  不是 1 号；抓错集线器只会得到 24 字节（仅文件头）的 pcap。
- USB 3.0(xHCI) 根集线器需要先以管理员跑一次 `USBPcapCMD.exe -I`（NonStandardHWIDs）——
  脚本里已经带上了。
- 抓包期间**不要**同时用自己的 MCP 调设备（流量混在一起；设备互斥也会拦住我们）。

### 2. 让厂商上位机做一次动作

`C:\Program Files\ATK-Logic\ATK-Logic.exe`。**用真实鼠标点蓝色播放键**——
GUI 自动化在这台机器上点不动它（后台合成点击 = 界面无变化；前台 `delivery_mode='foreground'`
被系统拒绝：窗口拿不到前台焦点）。所以这一步交给人在键盘前完成，5 秒的事。

### 3. 还原

```bash
PY=D:/MCP/01-atk-logic/.venv/Scripts/python.exe
$PY tools/usbcap_extract.py tools/usbcap/vendor_capture.pcap              # 全部
$PY tools/usbcap_extract.py tools/usbcap/vendor_capture.pcap --out-only --only-cfg   # 只看采集/触发命令
$PY tools/usbcap_extract.py tools/usbcap/vendor_capture.pcap --addr 4     # 指定设备地址
```

## 解析器踩过的坑（下次直接用）

1. **别按 VID/PID 过滤** —— 设备在抓包前就已枚举，没注入描述符时 tshark 取不到
   `usb.idVendor`，过滤结果恒为空。按 **`usb.device_address`** 过滤；用"哪个地址有 OUT 0x02"
   自动识别设备（本机是 4；地址 9 是另一个 HID 设备，会刷屏）。
2. **USBPcap 按 USB 包/URB 记录**：厂商上位机读 16 KB URB，每条 16 KB 记录**自身 2048 对齐** →
   **逐记录**解交织；512 字节的短响应**不解交织**。把短响应和 16 KB 记录拼成一条流再整体解交织会错位
   （这是最初"解析不出帧"的原因）。
3. 抓包时文件被 USBPcapCMD 独占 → tshark 会报 "You don't have permission to read the file"，
   **先停抓包再解析**。
4. 提权进程写出来的 pcap 归管理员所有；`Get-Item` 仍能看大小，但读内容最好在停抓后由普通用户读
   （本机验证可行）。

## 已知限制

- 厂商 GUI 无法用 agent 自动点击（见步骤 2），需要人工配合一次。
- RLE 模式本轮没触发到（要研究 RLE 得在上位机里显式打开对应选项再抓一次）。
