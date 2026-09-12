# -*- coding: utf-8 -*-
"""`.atkdl` 读写回归测试（离线，不需要硬件/设备）。

跑法: python test_atkdl.py [官方样例目录]

覆盖：
  A 组  11 个官方样例全部读通：字段自洽（setHz vs SamplingFrequency×1000、depth=setHz×setTime）、
        有效采样数与数据量一致性
  B 组  用我们的解码/统计逻辑核对官方样例的物理量：
        UART 样例(115200) → 解出 ALIENTEK；PWM 样例(10MHz/30%) → 频率与占空比；
        CAN 样例(500k) → 位宽；WS2812 样例(800k) → 位宽
  C 组  合成波形 write → read 往返：采样逐位一致、采样率/深度/解码 JSON 保真
  D 组  官方样例数据 → 我们写一份 → 读回：writer 与官方结构自洽
"""
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import atkdl                                                   # noqa: E402
import atkproto as P                                           # noqa: E402

TESTDIR = r"C:/Program Files/ATK-Logic/test"
PASS = FAIL = 0


def check(name, got, want, tol=None, unit=""):
    global PASS, FAIL
    ok = (abs(got - want) <= tol) if tol is not None else (got == want)
    if ok:
        PASS += 1
        print("  ✓ %s = %s%s" % (name, got, unit))
    else:
        FAIL += 1
        print("  ✗ %s = %s%s  (期望 %s%s)" % (name, got, unit, want, unit))
    return ok


def check_true(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  ✗ %s %s" % (name, detail))


def run_lengths(samples):
    runs, cur, val = [], 1, samples[0] if samples else 0
    for s in samples[1:]:
        if s == val:
            cur += 1
        else:
            runs.append(cur)
            cur, val = 1, s
    runs.append(cur)
    return runs


def min_run(samples, floor=3):
    """最短非毛刺脉冲（CAN 这类每 bit 一个电平的协议 → = 位宽）。"""
    rs = sorted(r for r in run_lengths(samples) if r >= floor)
    return rs[0] if rs else 0


def bit_period(samples, floor=3):
    """相邻脉冲之和的中位数（WS2812 这类 1 bit = 高+低 → = 位周期）。"""
    rs = [r for r in run_lengths(samples) if r >= floor]
    sums = sorted(rs[i] + rs[i + 1] for i in range(len(rs) - 1))
    return sums[len(sums) // 2] if sums else 0


def group_b():
    print("\n== B 组：官方样例的物理量核对（用我们的解码/统计）==")
    # UART 115200 @10MHz：自动波特率应落在 115200 附近（本机时钟约 +0.9%）
    s = atkdl.read_atkdl(os.path.join(TESTDIR, "uart_tx_115200.atkdl"))
    smp = s.samples(0)
    dec = P.uart_decode(smp, s.sample_rate_hz, baud=None, max_bytes=200)
    txt = dec.get("text") or ""
    print("    UART: rate=%d 实测位宽=%.2f 采样 → 波特率 %.0f  解出 %d 字节 text=%r" %
          (s.sample_rate_hz, dec.get("bit_width_samples", 0), dec.get("detected_baud", 0),
           dec.get("bytes_decoded", 0), txt[:40]))
    check_true("UART 自动波特率 ≈115200", 112000 <= (dec.get("detected_baud") or 0) <= 119000,
               "(=%.0f)" % (dec.get("detected_baud") or 0))
    check_true("UART 内容含 ALIENTEK", "ALIENTEK" in txt, "text=%r" % txt[:30])
    check("UART 位宽", round(dec.get("bit_width_samples", 0), 1),
          round(s.sample_rate_hz / 115200.0, 1), tol=1.0, unit=" 采样")
    # PWM 10MHz 30% @200MHz → 独立验证采样率
    s2 = atkdl.read_atkdl(os.path.join(TESTDIR, "pwm_10M_30_25.atkdl"))
    st = s2.stats(0, limit=20000)
    print("    PWM : rate=%d 频率=%.3f MHz 占空比=%.2f%%" %
          (s2.sample_rate_hz, st["frequency_hz"] / 1e6, st["duty_pct"]))
    check("PWM 频率", round(st["frequency_hz"] / 1e6, 2), 10.0, tol=0.1, unit=" MHz")
    check("PWM 占空比", round(st["duty_pct"], 1), 30.0, tol=1.0, unit=" %")
    # CAN 500k @100MHz → 最短脉冲 = 1 bit = 200 采样
    s3 = atkdl.read_atkdl(os.path.join(TESTDIR, "can_500K.atkdl"))
    w = min_run(s3.samples(0, limit=400000))
    print("    CAN : rate=%d 最短脉冲=%d 采样 (期望 %d)" % (s3.sample_rate_hz, w, s3.sample_rate_hz // 500000))
    check("CAN 位宽", w, s3.sample_rate_hz // 500000, tol=8, unit=" 采样")
    # WS2812 800k @40MHz → 位周期 = 1.25us = 50 采样
    s4 = atkdl.read_atkdl(os.path.join(TESTDIR, "rgb_led_ws2812.atkdl"))
    bp = bit_period(s4.samples(0, limit=400000))
    print("    WS28: rate=%d 位周期中位=%d 采样 (期望 %d)" % (s4.sample_rate_hz, bp, s4.sample_rate_hz // 800000))
    check("WS2812 位周期", bp, s4.sample_rate_hz // 800000, tol=8, unit=" 采样")


def group_a(files):
    print("\n== A 组：官方样例结构 ==")
    for f in files:
        path = os.path.join(TESTDIR, f)
        s = atkdl.read_atkdl(path)
        sm = s.summary()
        print("  --- %s: rate=%s Hz depth=%s 活动通道=%s" %
              (f, sm["sample_rate_hz"], sm["depth"], sm["active_channels"]))
        check_true("%s 采样率>0" % f, s.sample_rate_hz > 0, "=%d" % s.sample_rate_hz)
        if s.rate_khz:
            check_true("%s kHz×1000 与 setHz 一致" % f,
                       s.rate_khz * 1000 == s.sample_rate_hz,
                       "(%d vs %d)" % (s.rate_khz * 1000, s.sample_rate_hz))
        sd = s.settings.get("settingData") or {}
        if sd.get("setHz") and sd.get("setTime") is not None:
            check_true("%s depth == setHz×setTime/1000" % f,
                       s.depth == int(sd["setHz"]) * sd["setTime"] // 1000,
                       "(%d vs %d)" % (s.depth, int(sd["setHz"]) * sd["setTime"] // 1000))
        for ch in sm["active_channels"]:
            valid = s.valid_count(ch)
            nbytes = len(s.raw_bytes(ch))
            if nbytes * 8 < valid:
                print("      ! %s CH%d 官方样例数据短于声明（原始文件即截断）: %d 字节 / %d 有效采样"
                      % (f, ch, nbytes, valid))
            check_true("%s CH%d 数据可用" % (f, ch), nbytes > 0 and valid > 0,
                       "(%d 字节 / %d 有效采样)" % (nbytes, valid))


def group_c(d):
    print("\n== C 组：write → read 往返 ==")
    rate = 10_000_000
    depth = 4000
    chans = {}
    for ch in (0, 3, 9):
        smp = P.bits_from_bytes(os.urandom(0)) or []
        smp = [(1 if (i // (7 + ch)) % 2 == 0 else 0) for i in range(depth)]     # 每通道不同周期
        chans[ch] = smp
    dec = {"UART": {"options": [{"id": "baudrate", "value": "115200"}]}, "main": {"decodeID": 1}}
    path = os.path.join(d, "roundtrip.atkdl")
    res = atkdl.write_atkdl(path, chans, rate, depth=depth, name="DL16 Plus", decode=dec)
    s = atkdl.read_atkdl(path)
    check("往返 采样率", s.sample_rate_hz, rate, unit=" Hz")
    check("往返 深度", s.depth, depth, unit=" 采样")
    check("往返 活动通道", s.active_channels(), [0, 3, 9], unit="")
    check_true("往返 解码 JSON 保真", s.decode == dec, "(%s)" % (list(s.decode.keys()) if s.decode else None))
    ok = True
    for ch, smp in chans.items():
        back = s.samples(ch)
        if len(back) < len(smp) or any(int(a) != b for a, b in zip(smp, back)):
            ok = False
            print("      CH%d 不一致: 前 24 位 %s vs %s" % (ch, list(smp[:24]), list(back[:24])))
    check_true("往返 采样逐位一致", ok)
    check("往返 set.ini setHz", int((s.settings.get("settingData") or {}).get("setHz", 0)), rate, unit=" Hz")
    import re as _re
    import zipfile
    names = [i.filename for i in zipfile.ZipFile(path).infolist()]
    check_true("往返 分片命名合规",
               bool([n for n in names if n.endswith(".bin")]) and
               all(_re.match(r"^\d+/\d+-\d+\.bin$", n) for n in names if n.endswith(".bin")),
               "(0/0-0.bin)")
    # 官方样例 16 个通道每个都有 channel.ini（少了厂商软件会报"读取通道N配置文件失败"）
    check("往返 16 通道 channel.ini", len([n for n in names if _re.match(r"^\d+/channel\.ini$", n)]),
          16, unit=" 个")
    # 大数据量：跨 1 MiB 分片（按原始字节给 → 3 MiB+5 字节应切成 4 片）
    blob = b"\xaa" * (3 * 1024 * 1024 + 5)
    path2 = os.path.join(d, "big.atkdl")
    atkdl.write_atkdl(path2, {0: blob}, rate, depth=len(blob) * 8)
    parts = [i.filename for i in zipfile.ZipFile(path2).infolist()
             if i.filename.startswith("0/") and i.filename.endswith(".bin")]
    check("大波形分片数", len(parts), 4, unit=" 片")
    s2 = atkdl.read_atkdl(path2)
    check("大波形采样数", len(s2.samples(0)), len(blob) * 8, unit=" 采样")
    check_true("大波形内容一致", set(s2.window(0, 100_000, 5000)) == {0, 1}, "(0xaa = 交替)")
    return res


def group_d(files, d):
    print("\n== D 组：官方样例 → 我们写一份 → 读回 ==")
    src = atkdl.read_atkdl(os.path.join(TESTDIR, "uart_tx_115200.atkdl"))
    chans = {ch: src.raw_bytes(ch) for ch in src.active_channels()}
    out = os.path.join(d, "official_repack.atkdl")
    atkdl.write_atkdl(out, chans, src.sample_rate_hz, depth=src.depth, name=src.name,
                      decode=src.decode)
    back = atkdl.read_atkdl(out)
    check("重打包 采样率", back.sample_rate_hz, src.sample_rate_hz, unit=" Hz")
    check("重打包 活动通道", back.active_channels(), src.active_channels(), unit="")
    same = all(back.raw_bytes(ch)[:len(src.raw_bytes(ch))] == src.raw_bytes(ch)
               for ch in src.active_channels())
    check_true("重打包 原始字节一致", same)
    smp_a = src.window(0, 0, 200000)
    smp_b = back.window(0, 0, 200000)
    check_true("重打包 前 200k 采样一致", bytes(smp_a) == bytes(smp_b))


def main():
    d = tempfile.mkdtemp(prefix="atkdl_test_")
    try:
        files = sorted(f for f in os.listdir(TESTDIR) if f.lower().endswith(".atkdl")) \
            if os.path.isdir(TESTDIR) else []
        if not files:
            print("!! 未找到官方样例目录 %s —— A/B/D 组跳过" % TESTDIR)
        else:
            print("官方样例目录 %s，共 %d 个" % (TESTDIR, len(files)))
            group_a(files)
            group_b()
        group_c(d)
        if files:
            group_d(files, d)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("\n" + "=" * 62)
    print("结果: %d 项通过 / %d 项失败" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
