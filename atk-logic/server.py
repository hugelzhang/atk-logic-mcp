#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ATK-Logic-Analyzer USB MCP 桥 (v2)
==================================
通过 pyusb 控制正点原子逻辑分析仪 (VID 0x1A86 PID 0xFFCC)。
协议/设备层在 atkproto.py (厂商源码 github.com/alientek-openedv/atk-logic 逆向 + 实测校准)。

要点:
- Buffer 模式(默认) 触发条件**不生效**: 立即录满缓冲后回传, 一次采集含全部 16 通道。
- 非 Buffer 模式(mode="trigger") 会带上厂商协议里的"等待触发"标志, 但**本机固件实测同样
  立即录满**(恒定电平信号也照录) → 触发条件在本机上不可用作"数据闸门", mode 仅决定
  下发哪个协议字段。返回里的 trigger_offset(order-3) 语义未完全确定, 只作参考。
- 统计数据按请求深度**截断后**计算, 频率取**周期中位数**(直接按边沿计数会被缓冲外
  垃圾尾巴的毛刺污染, 凭空多出 ~28 个上升沿)。
- USB 报错(EIO/句柄失效)自动重连重试, 无需重启 MCP; 也可显式调用 reset。
- 波形存档: save_capture(fmt="atkdl") 直接写厂商上位机格式(ATK-Logic 能打开);
  load_waveform 离线分析 .atkdl/裸 .bin —— **不占设备**, 配合官方样例
  (C:\Program Files\ATK-Logic\test\*.atkdl) 可在没插分析仪时验证解码链路。
- 跨进程互斥: 同一时刻只允许一个进程持有设备(命名 Mutex), 被占用时给出持有者 PID。
- **协议解码**: decode_protocol 跑厂商上位机自带的 208 个 sigrok 解码器(I²C/SPI/CAN/Modbus/
  SWD/USB PD/WS2812/DHT11…), 支持栈式链路(uart→modbus、i2c→24xx、spi→spiflash);
  既能照 .atkdl 里官方存的配置解, 也能手工指定通道/选项, 还能 live=True 采一次再解。
- **MCU 总线调试**: bus_decode 采/读 → 解码 → 输出**按时间排序的事务列表**(text 字段可直接读),
  live 时自动把波形存成 .atkdl 当证据; probe=True 可先逐通道"摸接线"(边沿/频率/UART 自动试解)。
"""
import os
import sys
import time
import json

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import atkproto as P
import atkdl
import srdhost

P.setup_backend([_HERE, os.path.join(_HERE, "..")])

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("atk-logic", instructions=__doc__)

_dev = P.AtkDevice()
VERSION = "2.0 (2026-09-11)"

RATE_HINT = "/".join(str(r) for r in P.RATES)


# ============================== 内部工具 ==============================
def _ch_list(channels, channel):
    """统一 channels/channel 两种入参 → 通道号列表。"""
    if channels is None:
        items = [channel]
    elif isinstance(channels, (list, tuple)):
        items = list(channels) or [channel]
    else:
        items = [channels]
    out = []
    for c in items:
        idx = P.channel_to_index(c)
        if not 0 <= idx < P.CHANNELS:
            raise P.AtkError("通道号必须 0..15 (收到 %s)" % c)
        if idx not in out:
            out.append(idx)
    return out


def _acquire(channels, depth, sample_rate_mhz, threshold_v, trigger, mode, timeout_s,
             rle=False):
    return _dev.acquire(rate_mhz=sample_rate_mhz, depth=depth, threshold_v=threshold_v,
                        trigger=trigger, channel=channels[0], mode=mode,
                        timeout_s=timeout_s, extra_channels=channels[1:], rle=rle)


def _rle_info(res):
    """RLE 采集时给返回里附一段"压缩效果"说明（不压缩时返回 {}）。"""
    if not res.get("rle"):
        return {}
    return {"rle": True, "rle_pairs": res.get("rle_pairs"),
            "wire_bytes": res.get("wire_bytes"), "expanded_bytes": res.get("expanded_bytes"),
            "compression_pct": round((res.get("compression") or 0) * 100, 1)}


def _warn(res, channels, depth):
    msgs = []
    if not res["complete"]:
        got = {c: res["stream"].samples_available(c) for c in channels}
        msgs.append("未在 %.1fs 内采到全部请求通道的 %d 采样 (实得 %s)；"
                    "trigger 模式下通常是触发条件未满足。" % (res["elapsed_s"], depth, got))
    if res["stream"].orders.get(6):
        pass
    return msgs


# ============================== 设备信息 ==============================
@mcp.tool()
def identify() -> str:
    """识别逻辑分析仪设备信息 (型号/序列号/固件)。"""
    def job(d):
        return "设备: %s %s\n序列号: %s\nUSB ID: %04X:%04X" % (
            d.manufacturer, d.product, d.serial_number or "N/A", P.VID, P.PID)
    return _dev.call(job)


@mcp.tool()
def status() -> str:
    """查看逻辑分析仪状态: 设备/接口/端点、连接是否健康、重连次数、最近一次错误。"""
    info = _dev.call(lambda d: "设备: %s %s\n接口: 0 端点: %s" % (
        d.manufacturer, d.product,
        [hex(e.bEndpointAddress) for e in d.get_active_configuration()[(0, 0)]]))
    return (info
            + "\n桥版本: %s" % VERSION
            + "\n重连次数: %d" % _dev.reconnects
            + "\n最近错误: %s" % (_dev.last_error or "无"))


@mcp.tool()
def reset() -> str:
    """强制断开并重新枚举 USB 句柄 (采集报 I/O 错误 EIO 时用; 不用重启 MCP)。"""
    _dev.reconnect(wake=True)
    return "USB 句柄已重建 (第 %d 次重连), 设备已唤醒。" % _dev.reconnects


@mcp.tool()
def pwm(hz: int = 1000, duty_pct: int = 50, stop: bool = False, channel: int = 0) -> str:
    """控制逻辑分析仪自带的 PWM 输出 (自测用)。hz=频率, duty_pct=占空比, stop=True 停止。
    channel=0 → PWM0, channel=1 → PWM1 (哪一路从哪个脚输出由硬件决定)。"""
    with _dev._lock:
        _dev.wake()
        if stop:
            _dev.pwm_stop(channel)
            return "PWM%d 已停止" % channel
        _dev.pwm_start(hz, duty_pct, channel)
        return "PWM%d: %d Hz, %d%%" % (channel, hz, duty_pct)


# ============================== 采集 ==============================
@mcp.tool()
def capture(channel: int = 0, depth: int = 2000000, sample_rate_mhz: int = 1,
            threshold_v: float = 1.5, trigger: str = "rising", max_points: int = 2000,
            mode: str = "buffer", timeout_s: float = 0, include_transitions: bool = True,
            rle: bool = False) -> dict:
    """采集单个通道的数字波形 (一次采集其实拿到全部 16 通道, 这里只回你问的那个)。

    channel=0..15; depth=该通道采样点数(8 的倍数); sample_rate_mhz 档位 %s;
    threshold_v=阈值(如 1.5V); trigger=%s;
    mode="buffer"(默认, 立即录满) | "trigger"(协议上等触发; 本机固件实测也立即录满,
    触发条件不生效 —— 见模块说明);
    max_points=返回波形的下采样点数(默认 2000; **<=0 则完全不回 time_s/level 数组**, 只要统计时用,
    否则 400k 点的采集会回 2 MB 数据); include_transitions=额外给电平变化点(更忠实);
    rle=True 让设备回 **RLE 压缩载荷** (省 USB 带宽; 桥会自动展开, 统计/波形与不压缩完全一致),
    返回里附压缩率; timeout_s=0 表示自动(约 2×录满时间+4s)。

    返回: 统计(频率=周期中位数法, 不受毛刺污染; 另有 frequency_hz_edgecount 旧算法对照)
    + 时间/电平数组 + 电平变化点 + order-3 偏移。""" % (RATE_HINT, P.TRIGGER_HINT)
    chs = _ch_list(None, channel)
    depth = int(depth) - int(depth) % 8
    res = _acquire(chs, depth, sample_rate_mhz, threshold_v, trigger, mode,
                   timeout_s if timeout_s else None, rle=rle)
    st = res["stream"]
    srate = sample_rate_mhz * 1e6
    samples, nbytes = P.samples_of(st, chs[0], depth, srate)
    stats = P.waveform_stats(samples, srate)
    out = {
        "channel": chs[0],
        "mode": mode,
        "sample_rate_hz": srate,
        "depth_requested": depth,
        "points": stats.get("points", 0),
        "bytes_read_total": None,
        "elapsed_s": res["elapsed_s"],
        "complete": res["complete"],
        "high_pct": stats.get("high_pct"),
        "duty_pct": stats.get("duty_pct"),
        "rising_edges": stats.get("rising_edges"),
        "falling_edges": stats.get("falling_edges"),
        "glitch_edges": stats.get("glitch_edges"),
        "narrow_pulses": stats.get("narrow_pulses"),
        "frequency_hz": stats.get("frequency_hz"),
        "period_samples_median": stats.get("period_samples_median"),
        "frequency_hz_edgecount": (round(stats["rising_edges"] / stats["points"]
                                          * srate, 3)
                                   if stats.get("points") else None),
        "trigger_offset_samples": st.trig_offset,
        "trigger_offset_s": (round(st.trig_offset / srate, 9)
                             if st.trig_offset is not None else None),
        "frames": dict(st.orders),
        "channels_with_data": sorted(st.chan.keys()),
        "warning": "; ".join(_warn(res, chs, depth)) or None,
    }
    if max_points and max_points > 0:              # max_points<=0 → 不回波形数组(只要统计/跳变时用)
        step = max(1, len(samples) // max_points)
        out["level_downsample_stride"] = step      # time_s/level 是等间隔抽稀, 仅供粗看
        out["time_s"] = [round(i / srate, 9) for i in range(0, len(samples), step)]
        out["level"] = [samples[i] for i in range(0, len(samples), step)]
    else:
        out["level_downsample_stride"] = None
        out["time_s"] = out["level"] = None
    if include_transitions:
        tr = P.transitions(samples, srate, max_points or 400)
        out["transitions"] = tr["changes"]
        out["transitions_total"] = tr["total"]
        out["transitions_truncated"] = tr["truncated"]
    out.update(_rle_info(res))
    return out


@mcp.tool()
def capture_multi(channels: str = "0", depth: int = 200000, sample_rate_mhz: int = 1,
                  threshold_v: float = 1.5, trigger: str = "rising",
                  mode: str = "buffer", timeout_s: float = 0,
                  include_trace: bool = False, max_points: int = 200,
                  rle: bool = False) -> dict:
    """一次采集, 返回多个通道的统计 (硬件本来就同时回 16 通道, 不额外耗时)。

    channels: 逗号分隔, 如 "0,1,2,3" 或 "CH0,CH3" (0..15)。
    单个通道的波形用 capture; 这里默认只回统计, include_trace=True 时附下采样波形。
    """
    chs = _ch_list(str(channels).split(","), 0)
    depth = int(depth) - int(depth) % 8
    res = _acquire(chs, depth, sample_rate_mhz, threshold_v, trigger, mode,
                   timeout_s if timeout_s else None, rle=rle)
    st = res["stream"]
    srate = sample_rate_mhz * 1e6
    per = {}
    for c in chs:
        samples, _ = P.samples_of(st, c, depth, srate)
        d = P.waveform_stats(samples, srate)
        d["frequency_hz_edgecount"] = (round(d["rising_edges"] / d["points"] * srate, 3)
                                       if d.get("points") else None)
        if include_trace or not d.get("points"):
            step = max(1, d["points"] // max_points) if (max_points and d.get("points") > max_points) else 1
            d["level"] = [samples[i] for i in range(0, len(samples), step)]
        per["CH%d" % c] = d
    out = {
        "mode": mode,
        "sample_rate_hz": srate,
        "depth_requested": depth,
        "elapsed_s": res["elapsed_s"],
        "complete": res["complete"],
        "trigger_offset_samples": st.trig_offset,
        "channels": per,
        "channels_with_data": sorted(st.chan.keys()),
        "warning": "; ".join(_warn(res, chs, depth)) or None,
    }
    out.update(_rle_info(res))
    return out


# ============================== 存盘 ==============================
def _default_out_dir():
    d = os.path.join(os.getcwd(), "docs", "波形存档")
    return d


@mcp.tool()
def save_capture(channel: int = 0, depth: int = 2000000, sample_rate_mhz: int = 1,
                 threshold_v: float = 1.5, trigger: str = "rising",
                 mode: str = "buffer", channels: str = "", out_file: str = "",
                 out_dir: str = "", also_raw: bool = True, tag: str = "",
                 fmt: str = "csv", rle: bool = False) -> str:
    """采集并**全量**(不下采样)存盘。默认目录 `<当前目录>/docs/波形存档/`。

    channels: 空 → 用 channel; 或 "0,1,2" 一次存多通道 (同一次采集)。
    out_file: 指定单个文件的完整路径 (单通道时用; fmt=atkdl 时可放多通道)。
    also_raw: 同时写 .bin 原始采样字节 (每字节 8 采样, LSB 在前) 与 .json 元数据。
    fmt: "csv"(默认) 或 "atkdl" —— 后者是**厂商上位机格式**, 可直接用 ATK-Logic 打开;
         官方样例就是这格式 (C:\\Program Files\\ATK-Logic\\test\\*.atkdl)。
    """
    chs = _ch_list(channels.split(",") if channels else None, channel)
    depth = int(depth) - int(depth) % 8
    res = _acquire(chs, depth, sample_rate_mhz, threshold_v, trigger, mode, None, rle=rle)
    st = res["stream"]
    srate = sample_rate_mhz * 1e6
    out_dir = out_dir or _default_out_dir()
    os.makedirs(out_dir, exist_ok=True)
    if out_file:                       # out_file 的父目录可能还不存在
        os.makedirs(os.path.dirname(os.path.abspath(out_file)), exist_ok=True)
    stamp = tag or time.strftime("%Y%m%d_%H%M%S")
    if fmt.lower() == "atkdl":
        path = out_file or os.path.join(out_dir, "atk_%s.atkdl" % stamp)
        atkdl.stream_to_atkdl(st, chs, sample_rate_mhz, depth, path,
                              threshold_v=threshold_v, trigger=trigger, mode=mode)
        note = "" if res["complete"] else "  ⚠ 数据未采满请求深度\n"
        return ("已保存 %d 通道 × %d 采样 (全量, .atkdl 厂商格式; 可直接用 ATK-Logic 打开)\n%s%s"
                % (len(chs), depth, note, os.path.abspath(path)))
    written = []
    for idx, c in enumerate(chs):
        samples, nbytes = P.samples_of(st, c, depth, srate)
        path = (out_file if (out_file and len(chs) == 1)
                else os.path.join(out_dir, "atk_%s_ch%d.csv" % (stamp, c)))
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("# ATK-Logic capture: ch%d %dMHz depth=%d threshold=%sV trigger=%s mode=%s\n"
                    % (c, sample_rate_mhz, depth, threshold_v, trigger, mode))
            f.write("time_s,level\n")
            buf = []
            for i in range(len(samples)):
                buf.append("%.9f,%d" % (i / srate, samples[i]))
                if len(buf) >= 20000:
                    f.write("\n".join(buf) + "\n")
                    buf = []
            if buf:
                f.write("\n".join(buf) + "\n")
        written.append(os.path.abspath(path))
        if also_raw:
            rawpath = os.path.splitext(path)[0] + ".bin"
            with open(rawpath, "wb") as f:
                f.write(st.channel_bytes(c, depth))
            stat = P.waveform_stats(samples, srate)
            metapath = os.path.splitext(path)[0] + ".json"
            with open(metapath, "w", encoding="utf-8") as f:
                json.dump({
                    "tool": "atk-logic MCP %s" % VERSION,
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "channel": c, "sample_rate_hz": srate, "depth": depth,
                    "threshold_v": threshold_v, "trigger": trigger, "mode": mode,
                    "points": len(samples), "raw_bytes": nbytes,
                    "bit_order": "LSB-first per byte (bit0 = 最早)",
                    "stats": stat,
                    "trigger_offset_samples": st.trig_offset,
                    "complete": res["complete"], "elapsed_s": res["elapsed_s"],
                    "files": {"csv": os.path.basename(path),
                              "bin": os.path.basename(rawpath)},
                }, f, ensure_ascii=False, indent=2)
            written.append(os.path.abspath(rawpath))
            written.append(os.path.abspath(metapath))
    note = "" if res["complete"] else "  ⚠ 数据未采满请求深度, 文件可能被截断\n"
    return ("已保存 %d 通道 × %d 采样 (全量)\n%s%s"
            % (len(chs), depth, note, "\n".join(written)))


# ============================== 离线波形（.atkdl / .bin） ==============================
@mcp.tool()
def load_waveform(path: str, channel: int = -1, max_samples: int = 2000000,
                  sample_rate_hz: int = 0, uart_baud: int = 0,
                  uart_max_bytes: int = 512, transitions_max: int = 200) -> dict:
    """**离线**分析波形存档 —— 不占用设备、不用插分析仪。

    支持 `.atkdl`(厂商上位机格式; 官方样例在 `C:\\Program Files\\ATK-Logic\\test\\`) 与
    裸 `.bin`(每字节 8 采样、LSB 在前; 这种必须给 sample_rate_hz)。
    channel=-1 → 自动挑第一个有数据的通道。
    uart_baud: 0=不解码, >0=按该波特率解码, -1=自动测波特率。
    返回: 会话摘要(采样率/深度/活动通道/官方解码器) + 统计 + 跳变 + 可选 UART 解码。
    """
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(p):
        return {"error": "文件不存在: %s" % p}
    out = {"path": p, "size_bytes": os.path.getsize(p)}
    if p.lower().endswith(".atkdl"):
        s = atkdl.read_atkdl(p)
        out.update(s.summary())
        active = out.get("active_channels") or []
        if channel < 0:
            channel = active[0] if active else 0
        if channel not in s.channels or not s.raw_bytes(channel):
            return dict(out, error="通道 %d 没有数据; 可用通道: %s" % (channel, active))
        rate = s.sample_rate_hz
        total = s.valid_count(channel) or len(s.raw_bytes(channel)) * 8
        n = min(int(max_samples), total)
        samples = s.window(channel, 0, n)
        out["channel"] = channel
        out["excerpt"] = {"start_sample": 0, "samples": n, "total_samples": total,
                          "truncated": n < total}
        sd = s.settings.get("settingData")
        out["session_settings"] = sd if isinstance(sd, dict) else None
    else:
        if not sample_rate_hz:
            return dict(out, error="裸 .bin 必须给 sample_rate_hz(Hz)")
        with open(p, "rb") as f:
            blob = f.read((int(max_samples) + 7) // 8)
        rate = int(sample_rate_hz)
        samples = P.bits_from_bytes(blob)
        out.update({"channel": channel if channel >= 0 else 0, "sample_rate_hz": rate,
                    "excerpt": {"samples": len(samples), "total_samples": os.path.getsize(p) * 8,
                                "truncated": os.path.getsize(p) * 8 > len(samples)}})
    out["stats"] = P.waveform_stats(samples, rate or 1)
    out["transitions"] = P.transitions(samples, rate or 1, max_points=int(transitions_max))
    if uart_baud:
        out["uart"] = P.uart_decode(samples, rate or 1,
                                    baud=(uart_baud if uart_baud > 0 else None),
                                    max_bytes=int(uart_max_bytes))
    return out


# ============================== sigrok 协议解码器（离线/在线） ==============================
def _parse_pairs(text):
    """解析 "scl=7,sda=6" / "baudrate=115200,parity=none" → dict。"""
    out = {}
    for item in (text or "").replace("，", ",").split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


@mcp.tool()
def list_protocols(filter: str = "") -> str:
    """列出可用的 sigrok 协议解码器（厂商上位机自带 208 个）。filter 为名字子串。"""
    names = srdhost.list_decoders()
    if filter:
        names = [n for n in names if filter.lower() in n.lower()]
    return "共 %d 个解码器:\n%s" % (len(names), ", ".join(names))


@mcp.tool()
def decode_protocol(protocol: str = "", path: str = "", from_saved: bool = True,
                    channels: str = "", options: str = "", stack: str = "",
                    live: bool = False, depth: int = 1000000, sample_rate_mhz: int = 1,
                    threshold_v: float = 1.5, trigger: str = "rising", mode: str = "buffer",
                    sample_rate_hz: int = 0, max_samples: int = 4000000,
                    start_sample: int = -1, annotation_limit: int = 60,
                    decoders_dir: str = "") -> dict:
    """用 **sigrok 协议解码器**（厂商上位机自带的 208 个 PD：I²C/SPI/CAN/Modbus/1-Wire/SWD/USB PD…）解码波形。

    三种用法：
    1) 照存档里官方的配置解码（最省事）: `decode_protocol(protocol="modbus", path="...atkdl")`
       —— from_saved=True 时通道/选项/解码链**全部照抄 .atkdl 里的官方配置**。
    2) 手上的原始字节/在线采集: `decode_protocol(protocol="i2c", channels="scl=0,sda=1",
       options="address_format=shifted", path="...atkdl")`
    3) 直接采一次再解: `decode_protocol(protocol="spi", channels="clk=0,miso=1,mosi=2,cs=3",
       live=True, depth=400000, sample_rate_mhz=25)`

    stack: 栈式协议的下层（如 "uart" 配 modbus、"i2c" 配 24xx EEPROM），多个用逗号分隔。
    官方样例在 `C:\\Program Files\\ATK-Logic\\test\\*.atkdl`（11 个：UART/I²C/SPI/CAN/Modbus/
    SWD/WS2812/DHT11/MIPI DSI/USB PD/PWM），可直接拿来当回归用例。
    start_sample: -1 = 自动从"首个跳变前"开始（存档含触发前长空闲，从 0 解往往解不到）。
    返回：每层协议的注解（按行分组）、binary、窗口信息。
    """
    dd = decoders_dir or srdhost.DEFAULT_DECODERS_DIR
    try:
        if path and path.lower().endswith(".atkdl") and from_saved:
            chain, out = srdhost.from_atkdl(
                path, protocol=protocol or None,
                stack=[x for x in (stack or "").split(",") if x] or None,
                max_samples=int(max_samples), decoders_dir=dd,
                annotation_limit=int(annotation_limit),
                start=None if start_sample < 0 else int(start_sample))
            return {"chain": chain, "layers": out, "source": path, "from_saved": True}

        if not protocol:
            return {"error": "非 from_saved 模式必须给 protocol"}
        chain = [srdhost.resolve(x, dd) for x in (stack.split(",") if stack else []) if x]
        chain.append(srdhost.resolve(protocol, dd))
        cmap = {k: int(v) for k, v in _parse_pairs(channels).items()}
        opts = _parse_pairs(options)
        # 通道属于链路**最底层**解码器（sigrok 语义：上层吃下层的包，不直接吃引脚）
        cmaps = {chain[0]: cmap}
        opts_map = {chain[-1]: opts}

        if live:
            hw = sorted(set(cmap.values()))
            if not hw:
                return {"error": "live 模式必须给 channels（如 clk=0,miso=1,mosi=2,cs=3）"}
            res = _acquire(hw, int(depth) - int(depth) % 8, sample_rate_mhz, threshold_v,
                           trigger, mode, None)
            srate = sample_rate_mhz * 1e6
            samples = {h: P.samples_of(res["stream"], h, int(depth), srate)[0] for h in hw}
            src = "设备在线采集 %dMHz depth=%d" % (sample_rate_mhz, depth)
        elif path:
            s = atkdl.read_atkdl(path) if path.lower().endswith(".atkdl") else None
            if s is not None:
                if not cmap:
                    return {"error": "用手工通道时要给 channels（这个存档里有官方配置，"
                                     "也可以 from_saved=True 照抄）"}
                hw = sorted(set(cmap.values()))
                st = 0 if start_sample < 0 else int(start_sample)
                if start_sample < 0:
                    acts = [s.first_activity(h) for h in hw]
                    acts = [a for a in acts if a is not None]
                    st = max(0, min(acts) - 4000) if acts else 0
                samples = {h: s.window(h, st, int(max_samples)) for h in hw}
                srate = s.sample_rate_hz
                src = "%s (窗口起点 %d)" % (path, st)
            else:
                if not sample_rate_hz:
                    return {"error": "裸 .bin 必须给 sample_rate_hz(Hz)"}
                with open(path, "rb") as f:
                    blob = f.read((int(max_samples) + 7) // 8)
                seq = P.bits_from_bytes(blob)
                samples = {h: seq for h in cmap.values()}
                srate = int(sample_rate_hz)
                src = path
        else:
            return {"error": "要给 path（波形存档），或 live=True 在线采集"}
        results = srdhost.run_chain(chain, cmaps, opts_map, srate, samples, dd)
        out = srdhost.summarize(results, max_items=int(annotation_limit))
        return {"chain": chain, "layers": out, "source": src,
                "sample_rate_hz": srate, "from_saved": False}
    except (srdhost.PdError, Exception) as e:                     # noqa: BLE001
        return {"error": "%s: %s" % (type(e).__name__, e)}


# ============================== MCU 总线调试（事务列表） ==============================
def _probe_channels(samples, srate):
    """逐通道画像：边沿数/估频率/占空比 + UART 自动波特率试解（认接线用）。"""
    out = {}
    for ch in sorted(samples):
        smp = samples[ch]
        st = P.waveform_stats(smp, srate)
        d = {"points": st.get("points"), "edges": st.get("rising_edges"),
             "freq_hz": st.get("frequency_hz"), "high_pct": st.get("high_pct"),
             "glitch_edges": st.get("glitch_edges")}
        if not st.get("rising_edges"):
            d["looks_like"] = "恒定电平（无跳变）"
            out[ch] = d
            continue
        dec = P.uart_decode(smp, srate, baud=None, max_bytes=64)
        txt = dec.get("text") or ""
        printable = sum(1 for c in txt if 32 <= ord(c) < 127)
        if (dec.get("bytes_decoded") or 0) >= 4 and printable >= max(3, len(txt) // 2):
            d["uart_guess"] = {"baud": dec.get("detected_baud"), "text": txt[:60]}
            d["looks_like"] = "UART（自动波特率解出了可读文本）"
        elif st.get("duty_pct") is not None and 40 <= st["duty_pct"] <= 60:
            d["looks_like"] = "可能是时钟线（近 50% 占空比）"
        else:
            d["looks_like"] = "数据/控制线（占空比不固定）"
        out[ch] = d
    return out


@mcp.tool()
def bus_decode(protocol: str = "", channels: str = "", options: str = "", stack: str = "",
               path: str = "", live: bool = True, depth: int = 1000000,
               sample_rate_mhz: int = 10, threshold_v: float = 1.5,
               trigger: str = "any", mode: str = "buffer", sample_rate_hz: int = 0,
               max_samples: int = 4000000, max_items: int = 40, save: bool = True,
               save_dir: str = "", probe: bool = False, rle: bool = False,
               include_layers: bool = False) -> dict:
    """**MCU 总线调试**：采一次（或读存档）→ 协议解码 → 输出按时间排序的**事务列表**。

    用法：
    1) 不知道接线 → `bus_decode(probe=True)`：逐通道报边沿/估频率/占空比/UART 自动试解，帮你认通道。
    2) 知道接线 → 给协议和通道：
       - I²C:   `protocol="i2c", channels="scl=3,sda=4"`
       - SPI:   `protocol="spi", channels="clk=0,miso=1,mosi=2,cs=3"`
       - UART:  `protocol="uart", channels="rx=0", options="baudrate=115200"`
       - 栈式:  `protocol="modbus", stack="uart"`（省略 stack 也能按 PD 的 inputs 自动推）
    3) 读已有波形 → `path="xxx.atkdl", live=False`。

    live=True 时会把这次波形**自动存成 .atkdl**（证据/复现，路径在返回里）。
    trigger: rising/falling/any/high/low；不接信号时建议 any。
    rle=True 让设备回压缩载荷（深窗口省 90%+ 带宽）。
    include_layers=False（默认）只回事务列表；要看完整解码层（每行注解样本）才开。
    返回 text = 可直接读的事务列表；transactions = 同内容的 JSON。
    """
    dd = srdhost.DEFAULT_DECODERS_DIR
    try:
        cmap = {k: int(v) for k, v in _parse_pairs(channels).items()}
        opts = _parse_pairs(options)
        saved, src = None, ""
        if live:
            hw = sorted(set(cmap.values())) or list(range(P.CHANNELS))
            dep = int(depth) - int(depth) % 8
            res = _acquire(hw, dep, sample_rate_mhz, threshold_v, trigger, mode, None, rle=rle)
            srate = sample_rate_mhz * 1e6
            samples = {h: P.samples_of(res["stream"], h, dep, srate)[0] for h in hw}
            if save:
                d = save_dir or _default_out_dir()
                os.makedirs(d, exist_ok=True)
                saved = os.path.join(d, "atk_bus_%s.atkdl" % time.strftime("%Y%m%d_%H%M%S"))
                atkdl.stream_to_atkdl(res["stream"], hw, sample_rate_mhz, dep, saved,
                                      threshold_v=threshold_v, trigger=trigger, mode=mode)
            src = "在线采集 %dMHz depth=%d trigger=%s" % (sample_rate_mhz, dep, trigger)
        elif path:
            if path.lower().endswith(".atkdl"):
                if not cmap and not probe:
                    # 没给手工通道 → 直接照存档里官方的解码器配置跑（最省事）
                    chain, results, info = srdhost.run_from_atkdl(
                        path, protocol=protocol or None,
                        stack=[x for x in (stack or "").split(",") if x] or None,
                        max_samples=int(max_samples), decoders_dir=dd)
                    tx = srdhost.transactions(results, max_items=int(max_items))
                    ln = {"layers": srdhost.summarize(results, max_items=5)} if include_layers else {}
                    return {"source": path, "from_saved": True, "chain": chain,
                            "window": info, "sample_rate_hz": info["sample_rate_hz"],
                            "text": tx["text"], "transactions": tx["items"][:int(max_items)],
                            "transaction_total": tx["total"], "by_protocol": tx["by_protocol"],
                            **ln}
                s = atkdl.read_atkdl(path)
                hw = sorted(set(cmap.values())) or (s.active_channels() if probe else [])
                if not hw:
                    return {"error": "读存档时要给 channels（或 probe=True 自动挑活动通道）"}
                acts = [s.first_activity(h) for h in hw]
                acts = [a for a in acts if a is not None]
                st0 = max(0, min(acts) - 4000) if acts else 0
                samples = {h: s.window(h, st0, int(max_samples)) for h in hw}
                srate = s.sample_rate_hz
                src = "%s (窗口起点 %d)" % (path, st0)
            else:
                if not sample_rate_hz:
                    return {"error": "裸 .bin 必须给 sample_rate_hz(Hz)"}
                with open(path, "rb") as f:
                    blob = f.read((int(max_samples) + 7) // 8)
                seq = P.bits_from_bytes(blob)
                samples = {h: seq for h in cmap.values()}
                srate = int(sample_rate_hz)
                src = path
        else:
            return {"error": "要么 live=True 采一次，要么给 path"}

        out = {"source": src, "sample_rate_hz": srate, "captured_channels": sorted(samples),
               "saved_atkdl": saved}
        out.update(_rle_info(res) if live else {})
        if probe or not cmap or not protocol:
            out["probe"] = _probe_channels(samples, srate)
            if not protocol:
                out["hint"] = ("先看 probe 认通道（UART 候选会有 uart_guess），"
                               "再带 protocol+channels 解事务")
                return out
        chain = [srdhost.resolve(x, dd) for x in (stack.split(",") if stack else []) if x]
        chain.append(srdhost.resolve(protocol, dd))
        # 通道给链路最底层（上层协议吃下层的包，不直接吃引脚）；选项给目标协议
        results = srdhost.run_chain(chain, {chain[0]: cmap}, {chain[-1]: opts}, srate,
                                    samples, dd, max_samples=int(max_samples))
        tx = srdhost.transactions(results, max_items=int(max_items))
        ln = {"layers": srdhost.summarize(results, max_items=5)} if include_layers else {}
        out.update({
            "chain": chain,
            "text": tx["text"],
            "transactions": tx["items"][:int(max_items)],
            "transaction_total": tx["total"],
            "by_protocol": tx["by_protocol"],
            **ln,
        })
        return out
    except Exception as e:                                        # noqa: BLE001
        return {"error": "%s: %s" % (type(e).__name__, e)}


# ============================== 协议解码 ==============================
@mcp.tool()
def decode_uart(channel: int = 0, baud: int = 0, depth: int = 4000000,
                sample_rate_mhz: int = 25, threshold_v: float = 1.5,
                trigger: str = "falling", mode: str = "buffer",
                data_bits: int = 8, parity: str = "none", stop_bits: int = 1,
                max_bytes: int = 4096, out_file: str = "", rle: bool = False) -> dict:
    """采集并解码 UART (异步串行, 默认 8N1)。baud=0 时自动测波特率(按最短脉冲=1位)。

    默认 25MHz / 4M 深度 → 约 160ms 窗口 (115200 下够 1800 字节)。
    返回解码字节 hex + 文本(UTF-8/GBK 尝试) + 波特率/位宽/帧错误数; out_file 可落盘原始字节。
    """
    chs = _ch_list(None, channel)
    depth = int(depth) - int(depth) % 8
    res = _acquire(chs, depth, sample_rate_mhz, threshold_v, trigger, mode, None, rle=rle)
    st = res["stream"]
    srate = sample_rate_mhz * 1e6
    samples, _ = P.samples_of(st, chs[0], depth, srate)
    dec = P.uart_decode(samples, srate, baud=baud or None, data_bits=data_bits,
                        parity=parity, stop_bits=stop_bits, max_bytes=max_bytes)
    dec.update({
        "channel": chs[0], "mode": mode, "sample_rate_hz": srate,
        "points": len(samples), "complete": res["complete"],
        "elapsed_s": res["elapsed_s"],
        "stats": P.waveform_stats(samples, srate),
        "warning": "; ".join(_warn(res, chs, depth)) or None,
    })
    if out_file and "hex" in dec:
        with open(out_file, "wb") as f:
            f.write(bytes.fromhex(dec["hex"]))
        dec["out_file"] = os.path.abspath(out_file)
    return dec


@mcp.tool()
def send_raw(code: int, data_hex: str = "", read_seconds: float = 1.0) -> dict:
    """逃生口: 直接下发帧命令 (便于后续逆向)。code=命令码(十进制或 0x..), data_hex 如 "90 00 01"。
    返回读到的字节数与前若干帧的 order/长度。"""
    if isinstance(code, str):
        code = int(code, 0)
    data = bytes.fromhex(data_hex.replace(" ", "").replace(",", "")) if data_hex.strip() else b""
    _dev.send(code, data)
    full = _dev.read(read_seconds)
    n2 = len(full) - len(full) % 2048
    frames = P.parse_frames(P.deinterleave(full[:n2])) if n2 >= 2048 else []
    return {
        "sent_code": code, "data_len": len(data),
        "bytes_read": len(full),
        "frames": [{"order": o, "len": len(p)} for o, p in frames[:20]],
    }


if __name__ == "__main__":
    mcp.run()
