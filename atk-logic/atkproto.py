# -*- coding: utf-8 -*-
"""ATK-Logic-Analyzer 协议 + 设备层（单一事实来源）

被 server.py(MCP) / smoke_test.py / tools/probes/*.py 共用。

协议与寄存器语义均来自厂商源码 (github.com/alientek-openedv/atk-logic):
  - pv/usb/usb_control.cpp : 命令码 / 帧格式 / CRC / 交织
  - pv/static/util.cpp     : triggerStringToByte() 触发字节编码 / gCRC32
关键实测结论（2026-09-11 本机实测, 详见 PROTOCOL-NOTES.md §实测）:
  1. Buffer 模式 (flags bit7=1) 下**触发条件不生效**: 设备总是录满缓冲后回传;
     trigger 标志字节(立即/等待) 只在非 Buffer 模式有意义。
  2. 一次采集**总是回传全部 16 通道**, 通道使能位只影响触发条件, 不影响数据。
  3. 采集结束后设备仍可能继续吐"缓冲外垃圾"(2 采样抖动)。**必须按请求深度截断**,
     否则频率/边沿统计会被垃圾尾巴污染(凭空多出 ~28 个上升沿)。
  4. 截断后时基精确: 同一路 1kHz 信号在 1/5/25MHz 档测得周期 = 1000/5000/25000 采样。
"""
import os
import sys
import json
import time
import tempfile
import ctypes.util
import threading
from collections import Counter

# ============================== 常量 ==============================
VID, PID = 0x1A86, 0xFFCC
EP_OUT, EP_IN = 0x02, 0x81
RATES = [1, 2, 4, 5, 10, 20, 25, 40, 50, 100, 200]    # MHz 档位 (hzIndex = 下标+1)
# 200MHz 是第 11 档: 厂商上位机存档里出现过 (setHz=200000000, selectHzIndex=10 → hzIndex=11),
# 2026-09-11 真机实测: 发 hzIndex=11 设备接受, 采 PWM0 1kHz 得周期 200000 采样 (=200MHz) → 支持。
CHANNELS = 16
TRIGGER_HINT = "rising/falling/any(双沿)/high/low"

# 触发类型 → ('R' 上升, 'F' 下降, '1' 高, '0' 低, 'X' 任意变化/双沿)
TRIG_MAP = {
    "rising": "R", "上升沿": "R", "rise": "R",
    "falling": "F", "下降沿": "F", "fall": "F",
    "any": "X", "双沿": "X", "change": "X", "任意变化": "X",
    "high": "1", "高电平": "1",
    "low": "0", "低电平": "0",
}

# 命令码
CMD_GET_DEVICE_DATA = 0x10
CMD_PARAM_SETTING = 0x11
CMD_SIMPLE_TRIGGER = 0x12
CMD_STOP = 0x15
CMD_PWM = 0x17
CMD_EXIT = 0x18

FLAG_BUFFER = 0x80   # bit7: 缓冲模式
FLAG_RLE = 0x40      # bit6: RLE 压缩（载荷 = [(count,byte)...] 对，见 decode_rle）


# ============================== libusb 后端 ==============================
def setup_backend(extra_dirs=()):
    """把 pyusb 的 libusb 查找指向包内 DLL (Windows 上必需)。"""
    import usb.backend.libusb1 as libusb1

    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.environ.get("ATK_LIBUSB_DLL")]
    cands += [os.path.join(d, "libusb-1.0.dll") for d in ([here] + list(extra_dirs))]
    cands += [os.path.join(here, "..", "siglent-sds-usb", "libusb-1.0.dll")]
    found = next((p for p in cands if p and os.path.exists(p)), None)
    if found is None:
        # 退回系统查找
        return None
    _orig = libusb1.get_backend
    libusb1.get_backend = lambda *a, **kw: _orig(
        *a, **{**kw, "find_library": lambda _c: found})
    return found


# ============================== CRC32 ==============================
_CRC_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0xEDB88320 if (_c & 1) else (_c >> 1)
    _CRC_TABLE.append(_c)


def crc32(data):
    """厂商 gCRC32: 反射表 0xEDB88320, 初值 0, 末异或 0xFFFFFFFF。"""
    crc = 0
    for b in data:
        crc = _CRC_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


# ============================== 交织 / 解交织 ==============================
def interleave(data):
    """2048 字节块交织成 4 条 512 字节 lane (发往设备用)。"""
    n2 = len(data) - len(data) % 2048
    out = bytearray(n2)
    for base in range(0, n2, 2048):
        src = data[base:base + 2048]
        for j in range(256):
            for lane in range(4):
                v = src[(j * 4 + lane) * 2] | (src[(j * 4 + lane) * 2 + 1] << 8)
                di = base + lane * 512 + 2 * j
                out[di] = v & 0xFF
                out[di + 1] = (v >> 8) & 0xFF
    return bytes(out)


def deinterleave(data):
    """设备回传的 2048 字节块解交织 (≥2048 的块才交织)。"""
    n2 = len(data) - len(data) % 2048
    out = bytearray(n2)
    for base in range(0, n2, 2048):
        s = [data[base + 512 * k:base + 512 * (k + 1)] for k in range(4)]
        idx = base
        for j in range(256):
            for k in range(4):
                v = s[k][2 * j] | (s[k][2 * j + 1] << 8)
                out[idx] = v & 0xFF
                out[idx + 1] = (v >> 8) & 0xFF
                idx += 2
    return bytes(out)


# ============================== 命令构造 ==============================
def framed_cmd(code, data=b""):
    """帧命令: [8×00][0x0A][code][len-1][data][0x0B][CRC32-LE] → 2048 对齐 → 交织。"""
    payload = bytes([code, len(data) + 1]) + bytes(data)
    frame = (b"\x00" * 8 + b"\x0a" + payload + b"\x0b"
             + crc32(payload).to_bytes(4, "little"))
    pad = (2048 - len(frame) % 2048) % 2048
    return interleave(frame + b"\x00" * pad)


def raw_cmd(*data):
    """裸命令 (MCU 直接识别, 512 字节补零)。"""
    b = bytes(data)
    return b + b"\x00" * (512 - len(b))


def parameter_setting_data(rate_mhz, depth, threshold_v=1.5, trigger_depth=None,
                           buffer_mode=True, rle=False):
    """0x11 载荷: [flags][阈值][hzIndex][深度5B][触发深度5B]。

    flags: bit7=Buffer 模式, bit6=RLE 压缩（厂商 `session_controller.cpp:231-235` 同款）。
    """
    if rate_mhz not in RATES:
        raise ValueError("采样率必须是 %s 之一" % RATES)
    flags = FLAG_BUFFER if buffer_mode else 0x00
    if rle:
        flags |= FLAG_RLE
    thr = int(round(abs(threshold_v) * 10)) & 0x7F
    if threshold_v < 0:
        thr |= 0x80
    if trigger_depth is None:
        trigger_depth = depth // 2
    return (bytes([flags, thr, RATES.index(rate_mhz) + 1])
            + int(depth).to_bytes(5, "little")
            + int(trigger_depth).to_bytes(5, "little"))


def trigger_bytes(channel_types, immediate=True, n_channels=CHANNELS):
    """构造 0x12 触发载荷 (厂商 triggerStringToByte 语义)。

    channel_types: {通道号: 'R'|'F'|'1'|'0'|'X'}; 只有列出的通道被使能。
    每字节管一对通道: 高半字节 = 偶数通道(2k), 低半字节 = 奇数通道(2k+1)
      偶数通道: bit7=使能 bit4=上升 bit5=下降 bit6=高电平
      奇数通道: bit3=使能 bit0=上升 bit1=下降 bit2=高电平
    返回: n_channels/2 个通道对字节 + 1 个"立即采集"标志字节。
    """
    n_bytes = (n_channels + 1) // 2
    out = bytearray()
    for pair in range(n_bytes):
        b = 0
        even, odd = 2 * pair, 2 * pair + 1
        t = channel_types.get(even)
        if t is not None:
            b |= 0x80
            if t in ("R", "X"):
                b |= 0x10
            if t in ("F", "X"):
                b |= 0x20
            if t in ("1", "X"):
                b |= 0x40
        t = channel_types.get(odd)
        if t is not None:
            b |= 0x08
            if t in ("R", "X"):
                b |= 0x01
            if t in ("F", "X"):
                b |= 0x02
            if t in ("1", "X"):
                b |= 0x04
        out.append(b)
    out.append(0x01 if immediate else 0x00)
    return bytes(out)


# ============================== 帧解析 ==============================
def decode_rle(payload):
    """RLE 模式下 order-1 载荷 → 原始字节流（每字节 8 个采样）。

    厂商实现即权威（`alientek-openedv/atk-logic` `pv/thread/thread_work.cpp:172-182`）：
    载荷前 2 字节是 [通道号][保留]，之后**每 2 字节一组**：
      count = 第 1 字节（1..255，单位是**字节**，即 8 个采样）
      value = 第 2 字节（该段所有采样的字节值）
    展开 = 重复 value 共 count 次。载荷长为奇数时最后一个孤立字节忽略。
    """
    out = bytearray()
    n = len(payload) - 2
    for i in range(2, 2 + (n - n % 2), 2):
        cnt = payload[i]
        if cnt:
            out += bytes((payload[i + 1],)) * cnt
    return bytes(out)


def encode_rle(blob, max_run=255):
    """原始字节流 → RLE 载荷体（不带头部；反向实现，供单测/自发自收对照）。"""
    out = bytearray()
    i, n = 0, len(blob)
    while i < n:
        v = blob[i]
        j = i + 1
        while j < n and blob[j] == v and (j - i) < max_run:
            j += 1
        out += bytes((j - i, v))
        i = j
    return bytes(out)


def parse_frames(data):
    """解析 [0x0A][order][len2B LE][data][0x00][0x0B] 帧流 (无状态, 供脚本用)。"""
    frames = []
    i, n = 0, len(data)
    while i < n:
        if data[i] == 0x0A and i + 4 < n:
            order = data[i + 1]
            flen = data[i + 2] | (data[i + 3] << 8)
            end = i + 4 + flen
            if end + 1 < n and data[end] == 0x00 and data[end + 1] == 0x0B:
                frames.append((order, bytes(data[i + 4:end])))
                i = end + 2
                continue
        i += 1
    return frames


class FrameStream:
    """增量帧解析器: 边收边解, 只保留各通道采样字节。

    相比"读满 9 秒再整体解析"的做法: 内存/耗时可控, 且能达成"够了就停"。
    order 语义: 1=采样数据 [ch][保留][样本...]; 3=触发偏移; 4=命令应答; 5=进度; 6=完成
    """

    def __init__(self, rle=False):
        self.buf = bytearray()
        self.pos = 0
        self.chan = {}            # ch -> bytearray(解交织后的样本字节)
        self.orders = Counter()
        self.trig_offset = None
        self.complete_frames = 0
        self.rle = bool(rle)
        self.rle_pairs = 0        # 收到的 RLE (count,byte) 对数（诊断用）
        self.raw_bytes = 0        # RLE 时=压缩后字节数，否则=展开后字节数

    def feed(self, data):
        if data:
            self.buf += data
        self._parse()

    def _parse(self):
        buf, n = self.buf, len(self.buf)
        i = self.pos
        while i < n:
            if buf[i] != 0x0A:
                i += 1
                continue
            if i + 4 >= n:
                break                     # 头部还没收全, 等更多数据
            flen = buf[i + 2] | (buf[i + 3] << 8)
            end = i + 4 + flen
            if end + 1 >= n:
                break                     # 帧不完整, 等更多数据
            if buf[end] != 0x00 or buf[end + 1] != 0x0B:
                i += 1
                continue
            payload = bytes(buf[i + 4:end])
            order = buf[i + 1]
            self.orders[order] += 1
            self.complete_frames += 1
            if order == 1 and len(payload) >= 2:
                if self.rle:
                    self.rle_pairs += max(0, (len(payload) - 2) // 2)
                    blob = decode_rle(payload)
                else:
                    blob = payload[2:]
                self.raw_bytes += len(payload) - 2
                self.chan.setdefault(payload[0], bytearray()).extend(blob)
            elif order == 3 and len(payload) >= 7:
                self.trig_offset = int.from_bytes(payload[2:7], "little")
            i = end + 2
        self.pos = i
        if i > 65536:                      # 丢弃已解析部分, 控制内存
            del self.buf[:i]
            self.pos = 0

    def samples_available(self, ch):
        return len(self.chan.get(ch, b"")) * 8

    def channel_bytes(self, ch, depth):
        """按请求深度截断的原始字节 (depth=每通道采样点数)。"""
        return bytes(self.chan.get(ch, b""))[:depth // 8]


def bits_from_bytes(raw):
    """每字节 8 个采样, **LSB 在前** (bit0 = 时间上最早)。"""
    out = bytearray(len(raw) * 8)
    k = 0
    for v in raw:
        for i in range(8):
            out[k] = (v >> i) & 1
            k += 1
    return bytes(out)


# ============================== 波形统计 ==============================
def waveform_stats(samples, srate_hz, glitch_max=2):
    """稳健统计。频率用**周期中位数**算 —— 直接按边沿数/时长算会被毛刺污染。"""
    n = len(samples)
    if n == 0:
        return {"points": 0}
    rises, falls = [], []
    for i in range(1, n):
        if samples[i] != samples[i - 1]:
            (rises if samples[i] else falls).append(i)
    gaps = [rises[i + 1] - rises[i] for i in range(len(rises) - 1)]
    glitch = sum(1 for g in gaps if g <= glitch_max)
    gaps_clean = [g for g in gaps if g > glitch_max]
    med = sorted(gaps_clean)[len(gaps_clean) // 2] if gaps_clean else None
    # 窄脉冲: 宽度 < 周期/2 的脉冲 (信号完整性/拼接异常指标)
    # 注意排除"记录窗口首尾被截断的半脉冲" —— 那是正常现象, 不是毛刺。
    narrow = 0
    if med:
        widths = []
        cur, cnt = samples[0], 1
        for s in samples[1:]:
            if s == cur:
                cnt += 1
            else:
                widths.append(cnt)
                cur, cnt = s, 1
        widths.append(cnt)
        inner = widths[1:-1] if len(widths) > 2 else []
        narrow = sum(1 for w in inner if w < med / 2.0)
    st = {
        "points": n,
        "high_pct": round(sum(samples) / n * 100, 4),
        "rising_edges": len(rises),
        "falling_edges": len(falls),
        "glitch_edges": glitch,
        "narrow_pulses": narrow,          # 已排除窗口首尾的半脉冲
        "period_samples_median": med,
        "period_samples_min": min(gaps_clean) if gaps_clean else None,
        "period_samples_max": max(gaps_clean) if gaps_clean else None,
        "frequency_hz": round(srate_hz / med, 3) if med else None,
        "duration_s": round(n / srate_hz, 9),
    }
    if st["rising_edges"] and st["falling_edges"]:
        st["duty_pct"] = st["high_pct"]
    return st


def transitions(samples, srate_hz, max_points=400):
    """电平变化点 (比等间隔抽样更忠实地表达波形)。

    返回 {"changes": [[t_s, level], ...], "total": N, "truncated": bool}。
    超过 max_points 时**截断并标记**（绝不抽稀 —— 抽稀会伪造时间轴）。
    """
    out = []
    prev = None
    for i in range(len(samples)):
        lv = samples[i]
        if lv != prev:
            out.append([round(i / srate_hz, 9), int(lv)])
            prev = lv
    total = len(out)
    capped = max_points if max_points else total
    return {"changes": out[:capped], "total": total, "truncated": total > capped}


# ============================== UART 解码 ==============================
def uart_decode(samples, srate_hz, baud=None, data_bits=8, parity="none",
                stop_bits=1, max_bytes=8192, idle_high=True):
    """从采样位流解码 UART (异步串行)。

    时序: 空闲(=idle_high 电平) → 起始位 1 位(反相) → data_bits 位 (LSB 先) → [校验] → stop_bits 位
    采样点取每位中点。baud=None 时用最短脉冲宽度估计位宽(自动波特率)。
    """
    n = len(samples)
    if n < 20:
        return {"error": "采样点太少"}
    idle = 1 if idle_high else 0

    # 位宽
    bit_w = None
    if baud:
        bit_w = srate_hz / float(baud)
    else:
        widths = []
        cur, cnt = samples[0], 1
        for s in samples[1:]:
            if s == cur:
                cnt += 1
            else:
                widths.append(cnt)
                cur, cnt = s, 1
        widths.append(cnt)
        short = [w for w in widths if 2 <= w <= max(4, n // 200)]
        if not short:
            return {"error": "找不到可用脉冲宽度 (信号可能是恒定电平或太快/太慢)"}
        cnt_w = Counter(short)
        cand = sorted(w for w, c in cnt_w.items() if c >= max(2, len(short) // 20))
        bit_w = float(cand[0]) if cand else float(min(short))
        baud = srate_hz / bit_w

    n_par = 0 if parity == "none" else 1
    bits_per_frame = 1 + data_bits + n_par + stop_bits
    out = bytearray()
    frames = []
    framing_errors = 0
    i = 1
    reserve = int(bit_w * bits_per_frame) + 2
    while i < n - reserve and len(out) < max_bytes:
        if samples[i - 1] == idle and samples[i] != idle:     # 起始位跳变
            start = i
            base = start + bit_w * 0.5                       # 第 0 位(起始位)中点
            ok = True
            val = 0
            for b in range(data_bits):
                idx = int(round(base + (b + 1) * bit_w))     # 数据位 b 的中点
                if idx >= n:
                    ok = False
                    break
                if samples[idx] == idle:                     # UART: 空闲电平 = 逻辑 1
                    val |= 1 << b
            parbit = None
            if ok and n_par:
                idx = int(round(base + (data_bits + 1) * bit_w))
                parbit = samples[idx] if idx < n else None
            stop_ok = True
            if ok:
                for s in range(stop_bits):
                    idx = int(round(base + (data_bits + n_par + 1 + s) * bit_w))
                    if idx >= n or samples[idx] != idle:
                        stop_ok = False
                        break
            if ok and stop_ok:
                out.append(val)
                frames.append({"t_s": round(base / srate_hz, 9), "byte": val})
                # 推进到"最后一个停止位的起点"再继续找起始沿: 若假设波特率与实际略有偏差,
                # 直接按整帧长度跳会错过下一帧的起始位 → 整段错位。
                i = start + int(bit_w * (1 + data_bits + n_par))
                if i <= start:
                    i = start + 1
            else:
                framing_errors += 1
                i = start + 1
        else:
            i += 1

    text = None
    for enc in ("utf-8", "gbk"):
        try:
            text = bytes(out).decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return {
        "detected_baud": round(srate_hz / bit_w, 1),
        "bit_width_samples": round(bit_w, 3),
        "bytes_decoded": len(out),
        "framing_errors": framing_errors,
        "hex": bytes(out).hex(" "),
        "text": text,
        "frames": frames[:200],
    }


# ============================== 异常 / 跨进程单实例互斥 ==============================
class AtkError(RuntimeError):
    pass


class DeviceOccupied(AtkError):
    """设备已被其他进程占用（避免两个进程同时读写同一 USB 设备导致 EIO/脏数据）。"""


class SingleInstanceGuard:
    """命名互斥（Windows 用命名 Mutex, POSIX 用 flock），保证同一时刻只有一个进程占用设备。

    同时把持有者 PID 写到 %TEMP%/atk-logic-device-owner.json，便于报出"被谁占了"。
    进程退出时由 OS 自动释放；设 ATK_ALLOW_MULTI=1 可跳过（危险，仅供调试）。
    """

    NAME = "atk-logic-analyzer-1A86-FFCC"

    def __init__(self, name=None):
        self._handle = None
        self._fh = None
        self.name = name or self.NAME
        self.owner_file = os.path.join(tempfile.gettempdir(), "atk-logic-device-owner.json")

    @staticmethod
    def bypassed():
        return os.environ.get("ATK_ALLOW_MULTI", "") not in ("", "0", "false", "False")

    def _write_owner(self):
        try:
            with open(self.owner_file, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "exe": sys.executable,
                           "started": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
        except OSError:
            pass

    def _clear_owner(self):
        try:
            if os.path.exists(self.owner_file):
                os.remove(self.owner_file)
        except OSError:
            pass

    def holder_info(self):
        try:
            with open(self.owner_file, encoding="utf-8") as f:
                d = json.load(f)
            return "PID %s (%s, 启动于 %s)" % (d.get("pid"), d.get("exe"), d.get("started"))
        except Exception:
            return "未知进程"

    def acquire(self):
        """成功返回 True；已被占用返回 False。"""
        if self._handle is not None or self._fh is not None:
            return True
        if os.name == "nt":
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.CreateMutexW(None, False, self.name)
            err = ctypes.get_last_error()
            if not h:
                return True                      # 拿不到锁机制就别挡路
            if err == 183:                       # ERROR_ALREADY_EXISTS
                k32.CloseHandle(h)
                return False
            self._handle = h
        else:
            import fcntl
            p = os.path.join(tempfile.gettempdir(), "atk-logic-device.lock")
            self._fh = open(p, "w")
            try:
                fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self._fh.close()
                self._fh = None
                return False
        self._write_owner()
        return True

    def release(self):
        if self._handle is not None and os.name == "nt":
            import ctypes
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self._handle)
            self._handle = None
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        self._clear_owner()

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass


# ============================== 设备 ==============================
class AtkDevice:
    """带全局锁 + USB 错误自动重连的 ATK 逻辑分析仪句柄。

    EIO / 句柄失效后不必重启 MCP: 捕获 USBError → dispose → 重新枚举 → 重试一次。
    """

    def __init__(self, retries=1):
        self._lock = threading.RLock()
        self._dev = None
        self._retries = retries
        self.reconnects = 0
        self.last_error = None
        self.guard = SingleInstanceGuard()

    # ---------- 连接管理 ----------
    def _open(self):
        import usb.core
        import usb.util
        if not SingleInstanceGuard.bypassed() and not self.guard.acquire():
            raise DeviceOccupied(
                "设备已被另一个进程占用：%s。\n"
                "（同一台逻辑分析仪不能同时被两个进程读写，否则会 EIO 或读到脏数据）\n"
                "处理：① 停掉那个进程（若是 Hermes/Claude 的 atk-logic MCP，关掉对应会话或重启 Hermes）；"
                "② 或设 ATK_ALLOW_MULTI=1 强制并存（仅调试）。" % self.guard.holder_info())
        d = usb.core.find(idVendor=VID, idProduct=PID)
        if d is None:
            self.guard.release()
            raise AtkError("未找到 ATK-Logic-Analyzer (VID 1A86 / PID FFCC)。"
                           "检查 USB 连接与 WinUSB 驱动 (drivers/install_driver.bat, 需管理员)。")
        try:
            d.set_configuration()
        except usb.core.USBError:
            pass
        usb.util.claim_interface(d, 0)
        self._dev = d
        return d

    def device(self):
        if self._dev is None:
            return self._open()
        return self._dev

    def close(self):
        d, self._dev = self._dev, None
        if d is not None:
            try:
                import usb.util
                usb.util.dispose_resources(d)
            except Exception:
                pass
        self.guard.release()

    def reconnect(self, wake=True):
        """强制丢弃句柄并重新枚举 (EIO / 重新插拔后调用)。"""
        with self._lock:
            self.close()
            time.sleep(0.4)
            d = self._open()
            self.reconnects += 1
            if wake:
                self._wake_locked(d)
            return d

    def call(self, fn, retries=None):
        """在锁内执行 fn(dev); USBError 时重连并重试。"""
        tries = self._retries if retries is None else retries
        with self._lock:
            attempt = 0
            while True:
                try:
                    return fn(self.device())
                except Exception as e:              # noqa: BLE001 - USB 层错误种类多
                    is_usb = type(e).__module__.startswith("usb") or "usb" in type(e).__name__.lower()
                    self.last_error = "%s: %s" % (type(e).__name__, e)
                    if not is_usb or attempt >= tries:
                        raise
                    attempt += 1
                    self.close()
                    time.sleep(0.5)
                    self.reconnects += 1

    # ---------- 基础 IO ----------
    def _wake_locked(self, d):
        import usb.core
        try:
            d.write(EP_OUT, raw_cmd(0x0A, 0x87, 0x01), timeout=1000)
        except usb.core.USBError:
            pass
        self._read_locked(d, 0.4)

    def wake(self):
        return self.call(lambda d: self._wake_locked(d))

    def _read_locked(self, d, seconds, chunk=4096):
        """尽量读满 seconds; 返回原始字节 (全部, 未解交织)。"""
        import usb.core
        out = []
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                out.append(bytes(d.read(EP_IN, chunk, timeout=100)))
            except usb.core.USBTimeoutError:
                continue
        return b"".join(out)

    def read(self, seconds):
        return self.call(lambda d: self._read_locked(d, seconds))

    def write_raw(self, data):
        return self.call(lambda d: d.write(EP_OUT, data, timeout=1000))

    def send(self, code, data=b""):
        return self.write_raw(framed_cmd(code, data))

    def drain(self, seconds=0.3):
        try:
            self.read(seconds)
        except Exception:
            pass

    def stop(self):
        try:
            self.send(CMD_STOP)
            time.sleep(0.15)
            self.drain(0.2)
        except Exception:
            pass

    # ---------- 采集 ----------
    def acquire(self, rate_mhz, depth, threshold_v=1.5, trigger="rising",
                channel=0, mode="buffer", timeout_s=None, extra_channels=(),
                progress=True, rle=False):
        """配置 → (等)采集 → 读到"请求通道都够 depth 采样"就停。

        rle=True 时下发 flags bit6，设备回**压缩载荷**，由 FrameStream 透明展开
        （所以上层拿到的采样/统计与不压缩时完全一致；只是想省 USB 带宽/存盘小时用）。

        返回 dict: {stream: FrameStream, elapsed_s, complete: bool,
                    bytes_read: int, raw_tail_junk: bool}
        """
        if mode not in ("buffer", "trigger"):
            raise AtkError("mode 必须是 buffer(立即录满) 或 trigger(等触发条件)")
        if RATES.count(rate_mhz) != 1:
            raise AtkError("采样率必须是 %s 之一" % RATES)
        if not 0 <= channel < CHANNELS:
            raise AtkError("channel 必须 0..15")
        depth = int(depth)
        if depth < 8:
            raise AtkError("depth 至少 8 (且需为 8 的倍数才能整字节返回)")
        depth -= depth % 8

        trig_type = TRIG_MAP.get(str(trigger).lower())
        if trig_type is None:
            raise AtkError("trigger 必须是 %s" % TRIGGER_HINT)
        want = sorted({channel} | {int(c) for c in extra_channels})
        for c in want:
            if not 0 <= c < CHANNELS:
                raise AtkError("通道号必须 0..15 (收到 %s)" % c)

        immediate = (mode == "buffer")
        srate_hz = rate_mhz * 1e6
        if timeout_s is None:
            timeout_s = max(5.0, depth / srate_hz * 2.0 + 4.0)

        def job(d):
            import usb.core
            stream = FrameStream(rle=rle)
            self._wake_locked(d)
            self._read_locked(d, 0.2)
            d.write(EP_OUT, framed_cmd(CMD_PARAM_SETTING,
                                       parameter_setting_data(rate_mhz, depth, threshold_v,
                                                              rle=rle)),
                    timeout=1000)
            time.sleep(0.25)
            self._read_locked(d, 0.2)
            trig = trigger_bytes({channel: trig_type}, immediate=immediate)
            d.write(EP_OUT, framed_cmd(CMD_SIMPLE_TRIGGER, trig), timeout=1000)
            t0 = time.time()
            need_bytes = depth // 8
            complete = False
            while time.time() - t0 < timeout_s:
                try:
                    blk = bytes(d.read(EP_IN, 4096, timeout=100))
                except usb.core.USBTimeoutError:
                    if all(stream.samples_available(c) >= depth for c in want):
                        complete = True
                        break
                    continue
                if not blk:
                    continue
                n2 = len(blk) - len(blk) % 2048
                if n2:
                    stream.feed(deinterleave(blk[:n2]))
                if all(stream.samples_available(c) >= depth for c in want):
                    complete = True
                    break
            return {"stream": stream, "elapsed_s": round(time.time() - t0, 3),
                    "complete": complete, "raw_need_bytes": need_bytes,
                    "rle": bool(rle), "rle_pairs": stream.rle_pairs,
                    "wire_bytes": stream.raw_bytes,
                    "expanded_bytes": sum(len(v) for v in stream.chan.values()),
                    "compression": (round(1 - stream.raw_bytes / max(1, sum(len(v) for v in stream.chan.values())), 3)
                                    if rle else None)}

        return self.call(job)

    # ---------- PWM (设备自带输出) ----------
    def pwm_start(self, hz, duty_pct, index=0):
        if not 0 < hz <= 200000000:
            raise AtkError("频率范围 1..200000000 Hz")
        maxhz = int(round(200000000 / hz))
        cnt = int(round(maxhz * max(0, min(100, duty_pct)) / 100.0))
        head = 0x11 if index == 0 else 0x21
        return self.send(CMD_PWM, bytes([head]) + maxhz.to_bytes(4, "little")
                         + cnt.to_bytes(4, "little"))

    def pwm_stop(self, index=0):
        return self.send(CMD_PWM, bytes([0x10 if index == 0 else 0x20]))


# ============================== 便捷封装 ==============================
def channel_to_index(name):
    """'CH3' / 'ch3' / 3 → 3"""
    if isinstance(name, int):
        return name
    s = str(name).strip().upper()
    if s.startswith("CH"):
        s = s[2:]
    return int(s)


def samples_of(stream, channel, depth, srate_hz):
    raw = stream.channel_bytes(channel, depth)
    return bits_from_bytes(raw), len(raw)
