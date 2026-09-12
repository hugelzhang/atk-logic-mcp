# -*- coding: utf-8 -*-
"""离线单元测试（不需要硬件）: 触发编码 / 帧解析 / 采样统计 / UART 解码。

运行: <任意 python3> test_offline.py      (只依赖标准库)
"""
import os
import sys
import random

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import atkproto as P

FAIL = []


def check(name, got, want, tol=None):
    ok = (abs(got - want) <= tol) if (tol is not None and isinstance(got, (int, float))) else (got == want)
    print(("  ✓ " if ok else "  ✗ ") + f"{name}: {got}" + ("" if ok else f"  (期望 {want})"))
    if not ok:
        FAIL.append(name)


def synth_square(srate_hz, freq_hz, duty_pct, n, glitch_positions=()):
    """合成方波位流 (LSB 索引=时间顺序)。glitch_positions: 在这些索引插入 2 采样反向脉冲。"""
    period = srate_hz / freq_hz
    high = period * duty_pct / 100.0
    s = bytearray(n)
    for i in range(n):
        s[i] = 1 if (i % period) < high else 0
    for p in glitch_positions:
        if 0 <= p + 1 < n:
            s[p] = 1 - s[p]
            s[p + 1] = 1 - s[p + 1]
    return bytes(s)


def synth_uart(data, srate_hz, baud, data_bits=8, stop_bits=1, idle_lead=20):
    """合成 8N1 UART 波形位流 (空闲高, LSB 先)。"""
    bit = srate_hz / float(baud)
    s = bytearray([1] * idle_lead)
    for byte in data:
        s += bytearray([0] * int(round(bit)))                       # 起始位
        for b in range(data_bits):
            s += bytearray([(byte >> b) & 1] * int(round(bit)))     # 数据位 LSB 先
        s += bytearray([1] * int(round(bit * stop_bits)))           # 停止位
    s += bytearray([1] * 40)
    return bytes(s)


print("=" * 70)
print("[1] 触发字节编码 (厂商 triggerStringToByte 语义)")
t0 = P.trigger_bytes({0: "R"})
check("ch0 上升沿 → 首字节", t0[0], 0x90)
check("载荷长度 (8 对 + 立即标志)", len(t0), 9)
check("立即标志", t0[8], 0x01)
check("ch0 任意变化 → 首字节", P.trigger_bytes({0: "X"})[0], 0xF0)
check("ch3 上升沿 (第 2 字节低半字节)", P.trigger_bytes({3: "R"})[1], 0x09)
check("ch15 上升沿 (末字节低半字节)", P.trigger_bytes({15: "R"})[7], 0x09)
check("ch0 高电平", P.trigger_bytes({0: "1"})[0], 0xC0)
check("ch1 高电平", P.trigger_bytes({1: "1"})[0], 0x0C)
check("未列出的通道不使能", P.trigger_bytes({0: "R"})[1], 0x00)
check("等触发标志 (立即=0)", P.trigger_bytes({0: "R"}, immediate=False)[8], 0x00)

print("\n[2] 0x11 采集参数载荷")
p = P.parameter_setting_data(25, 1000000, 1.5)
check("Buffer 模式 flags", p[0], 0x80)
check("阈值 1.5V → 0x0F", p[1], 0x0F)
check("25MHz → hzIndex 7", p[2], 7)
check("深度 5B LE", int.from_bytes(p[3:8], "little"), 1000000)
check("触发深度默认 = 深度/2", int.from_bytes(p[8:13], "little"), 500000)

print("\n[3] 位流方向 (LSB 在前)")
b = P.bits_from_bytes(bytes([0b00000001, 0b10000000]))
check("首字节 LSB 在时间上最早", list(b), [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1])

print("\n[4] 帧解析 (FrameStream 增量解析, 含碎片化喂入)")
def mkframe(order, payload):
    return (b"\x0a" + bytes([order]) + len(payload).to_bytes(2, "little")
            + payload + b"\x00\x0b")

raw = bytearray()
raw += mkframe(1, bytes([0, 0x00]) + bytes(range(32)))      # ch0 数据
raw += mkframe(5, b"\x10\x00")                              # 进度
raw += mkframe(1, bytes([1, 0x00]) + bytes(range(16)))      # ch1 数据
raw += mkframe(3, bytes([0, 0]) + (12345).to_bytes(5, "little"))
fs = P.FrameStream()
for i in range(len(raw)):                                   # 逐字节喂: 最狠的碎片化
    fs.feed(bytes(raw[i:i + 1]))
check("order 统计", dict(fs.orders), {1: 2, 5: 1, 3: 1})
check("ch0 字节数", len(fs.chan[0]), 32)
check("ch1 字节数", len(fs.chan[1]), 16)
check("触发偏移解析", fs.trig_offset, 12345)
check("按深度截断", len(fs.channel_bytes(0, 64)), 8)

print("\n[5] 波形统计 (周期中位数法 vs 朴素的边沿计数法)")
s = synth_square(1_000_000, 1000, 50, 1_000_000)
st = P.waveform_stats(s, 1_000_000)
check("频率 (中位数法)", st["frequency_hz"], 1000.0, tol=0.001)
check("占空比", st["high_pct"], 50.0, tol=0.01)
check("上升沿 (首采样已为高, 不数第 0 点的假跳变)", st["rising_edges"], 999)
check("无毛刺", st["glitch_edges"], 0)
check("无窄脉冲", st["narrow_pulses"], 0)

# 单采样毛刺 (真实设备"缓冲外垃圾尾巴"的典型形态: 每 2 采样反向)
s2 = bytearray(synth_square(1_000_000, 1000, 50, 1_000_000))
base_i = 300000
for k in range(30):
    p = base_i + k * 2
    s2[p] = 1 - s2[p]
s2 = bytes(s2)
st2 = P.waveform_stats(s2, 1_000_000)
check("有毛刺时频率仍准确 (毛刺被剔除)", st2["frequency_hz"], 1000.0, tol=0.001)
check("毛刺边沿被计数", st2["glitch_edges"] >= 25, True)
check("窄脉冲被计数", st2["narrow_pulses"] >= 25, True)
check("朴素边沿法被毛刺污染 (所以不能用)", st2["rising_edges"] > 999 + 25, True)

print("\n[6] UART 解码 (合成 8N1 波形, 单/多字节 + 自动波特率)")
msg = "ATK-Logic 逻辑分析仪 UART 测试 ok".encode("gbk")
for srate, baud in ((25_000_000, 115200), (1_000_000, 9600), (10_000_000, 921600)):
    wave = synth_uart(msg, srate, baud)
    d1 = P.uart_decode(wave, srate, baud=baud)
    d2 = P.uart_decode(wave, srate, baud=None)
    ok1 = d1.get("hex", "").replace(" ", "") == msg.hex()
    ok2 = d2.get("hex", "").replace(" ", "") == msg.hex()
    print(f"  {'✓' if ok1 else '✗'} {baud}bps@{srate/1e6:g}MHz 指定波特率解码: {d1.get('bytes_decoded')} 字节, 文本={d1.get('text')!r}")
    print(f"  {'✓' if ok2 else '✗'} 同波形自动波特率: 测得 {d2.get('detected_baud')} bps (真值 {baud})")
    if not ok1:
        FAIL.append(f"uart decode {baud}")
    if not ok2:
        FAIL.append(f"uart auto-baud {baud}")

print("\n[7] CRC32 (对照厂商实现: 表驱动反射, 初值 0, 末异或 0xFFFFFFFF)")
import binascii
data = b"ATK-LOGIC-ANALYZER"
# 厂商 gCRC32: crc=0; table[(crc^b)&0xff]^(crc>>8); 末异或
ref = 0
for b in data:
    ref = P._CRC_TABLE[(ref ^ b) & 0xFF] ^ (ref >> 8)
ref ^= 0xFFFFFFFF
check("crc32 与手算一致", P.crc32(data), ref)
check("同一数据的 zlib 不同 (证明非标准 CRC)", P.crc32(data) != binascii.crc32(data) & 0xFFFFFFFF, True)

print("\n[8] 跨进程单实例互斥 (防止两个进程抢同一台设备)")
# 用独立名字测试，避免和"正在运行的 MCP 服务进程持有真实设备锁"互相干扰
_tname = "atk-logic-selftest-%d" % os.getpid()
g1 = P.SingleInstanceGuard(_tname)
check("首个实例获取成功", g1.acquire(), True)
g2 = P.SingleInstanceGuard(_tname)
check("第二个实例被拦下", g2.acquire(), False)
check("能报出持有者信息", "PID" in g1.holder_info(), True)
print("      持有者: %s" % g1.holder_info())
g1.release()
check("释放后新实例可获取", P.SingleInstanceGuard(_tname).acquire(), True)
os.environ["ATK_ALLOW_MULTI"] = "1"
check("ATK_ALLOW_MULTI=1 可强制绕过", P.SingleInstanceGuard.bypassed(), True)
del os.environ["ATK_ALLOW_MULTI"]
_held = not P.SingleInstanceGuard(P.SingleInstanceGuard.NAME).acquire()
print("      真实设备锁当前状态: %s" % ("被其他进程持有（正常，例如 MCP 服务在跑）" if _held else "空闲"))

print("\n[9] USB 抓包考古解析器自证 (toolchain self-check, 无需真抓包)")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
import usbcap_extract as U                                       # noqa: E402

# 主机→设备: 采集配置帧
blob = P.framed_cmd(P.CMD_PARAM_SETTING, P.parameter_setting_data(25, 1000000, 1.5))
txt = "\n".join(U.describe_command(blob))
check("还原 ParameterSetting: Buffer 标志", "flags=0x80(Buffer=1,RLE=0)" in txt, True)
check("还原 采样率档位", "hzIndex=7(25MHz)" in txt, True)
check("还原 深度", "深度=1000000" in txt, True)
print("      " + txt.replace("\n", "\n      "))

# 主机→设备: 触发帧 (ch0 上升 + 立即)
blob = P.framed_cmd(P.CMD_SIMPLE_TRIGGER, P.trigger_bytes({0: "R"}, immediate=True))
txt = "\n".join(U.describe_command(blob))
check("还原触发通道对", "CH0上升" in txt and "立即采集标志=1" in txt, True)
print("      " + txt.replace("\n", "\n      "))

# 主机→设备: PWM 启动
blob = P.framed_cmd(P.CMD_PWM, bytes([0x11]) + (200000).to_bytes(4, "little")
                    + (100000).to_bytes(4, "little"))
txt = "\n".join(U.describe_command(blob))
check("还原 PWM 参数", "→ 1000.0Hz 50.00%" in txt, True)

# 设备→主机: 帧流 (需 ≥2048 才解交织)
def mkframe(order, payload):
    return (b"\x0a" + bytes([order]) + len(payload).to_bytes(2, "little")
            + payload + b"\x00\x0b")
stream = bytearray()
stream += mkframe(1, bytes([0, 0]) + bytes(200))
stream += mkframe(3, bytes([0, 0]) + (10008).to_bytes(5, "little"))
stream += mkframe(4, b"\xff\x00\x11\x03")
stream += mkframe(5, b"\x10\x00")
stream += b"\x00" * (2048 - len(stream) % 2048)
txt = U.summarize_in(P.interleave(bytes(stream)))
check("还原回传帧 order 统计", "order 统计" in txt and "{1: 1, 3: 1" in txt, True)
check("还原 order-3 偏移", "int=10008" in txt, True)
print("      " + txt.replace("\n", "\n      "))

# ------------------------------------------------------------------ RLE（flags bit6）
print("\n== RLE 载荷编解码（厂商 thread_work.cpp:172-182 语义）==")
import random
random.seed(7)


def _hdr(blob, ch=3):
    return bytes([ch, 0]) + P.encode_rle(blob)


for _n in (0, 1, 7, 8, 100, 1000):                      # 1) 往返
    _raw = bytes(random.choice((0x00, 0xFF, 0xAA)) for _ in range(_n))
    check("RLE 往返 n=%d" % _n, P.decode_rle(_hdr(_raw)), _raw)

_raw = bytes([0xFF]) * 300                              # 2) 长游程拆多对（count 上限 255）
_enc = P.encode_rle(_raw)
check("300 长游程拆成 2 对", len(_enc), 4)
check("拆对后解码还原", P.decode_rle(bytes([0, 0]) + _enc), _raw)

check("奇数尾字节忽略", P.decode_rle(bytes([0, 0, 5, 0xFF, 0x77])), b"\xff" * 5)
check("count=0 不产出", P.decode_rle(bytes([0, 0, 0, 0xFF, 2, 0x01])), b"\x01" * 2)

check("flags: buffer+rle = 0xC0", P.parameter_setting_data(1, 8000, rle=True)[0], 0xC0)
check("flags: buffer 无 rle = 0x80", P.parameter_setting_data(1, 8000)[0], 0x80)
check("flags: 非 buffer + rle = 0x40",
      P.parameter_setting_data(1, 8000, buffer_mode=False, rle=True)[0], 0x40)

_raw = b"\xff" * 64 + b"\x00" * 64                      # 3) FrameStream 透明展开
_st = P.FrameStream(rle=True)
_st.feed(mkframe(1, bytes([2, 0]) + P.encode_rle(_raw)))
check("FrameStream(rle) 展开正确", bytes(_st.chan[2]), _raw)
check("FrameStream(rle) 记到对数", _st.rle_pairs > 0, True)
check("FrameStream(rle) 线上字节 < 展开字节", _st.raw_bytes < len(_raw), True)
_st2 = P.FrameStream()
_st2.feed(mkframe(1, bytes([2, 0]) + _raw))
check("非 RLE 路径不受影响", bytes(_st2.chan[2]), _raw)

print("\n" + "=" * 70)
if FAIL:
    print("失败项: %s" % FAIL)
    sys.exit(1)
print("全部离线测试通过 ✓")
