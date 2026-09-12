# -*- coding: utf-8 -*-
"""真机验证 RLE（flags bit6）：设备是否真的回压缩载荷、我们的展开是否正确。

判据（不靠猜）：
  1) PWM0 1kHz 50% @1MHz：周期 1000 采样 = 125 字节，50% 占空 → 每段游程必是 **62 或 63 字节**，
     且值只在 0x00/0xFF 之间交替。→ 展开正确才有这个结构。
  2) 展开后字节数必须 == depth/8（一个不多一个不少）。
  3) 统计（频率/占空比）与不压缩采集一致。
  4) PWM0 停 → 全 0 的静态信号：RLE 应压到极小（每 255 字节 1 对）。

用法（需设备空闲）：python tools/verify_rle.py
"""
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import atkproto as P                                              # noqa: E402

FAIL = 0


def check(name, ok, detail=""):
    global FAIL
    print(("  ✓ " if ok else "  ✗ ") + name + ("  " + str(detail) if detail else ""))
    if not ok:
        FAIL += 1


def main():
    P.setup_backend()
    depth, rate = 400000, 1
    dev = P.AtkDevice()
    try:
        print("== 0) 起 PWM0 1kHz 50% ==")
        dev.pwm_start(1000, 50)
        time.sleep(0.2)

        print("== 1) RLE 采集 @%dMHz depth=%d ==" % (rate, depth))
        r = dev.acquire(rate, depth, trigger="any", mode="buffer", rle=True)
        st = r["stream"]
        exp_bytes = depth // 8
        check("设备确实回了 RLE 对（rle_pairs>0）", r["rle_pairs"] > 0, "pairs=%d" % r["rle_pairs"])
        check("线上字节 << 展开字节（真压缩）", r["wire_bytes"] < r["expanded_bytes"],
              "线上 %d / 展开 %d（省 %.1f%%）" % (r["wire_bytes"], r["expanded_bytes"],
                                                  (r["compression"] or 0) * 100))
        got = bytes(st.chan.get(0, b""))[:exp_bytes]
        check("展开后字节数 == depth/8", len(got) == exp_bytes, len(got))

        # 结构判据：1kHz@1MHz → 周期 1000 采样 = 125 字节；每周期 2 个"跳变字节"（边沿落在字节中间）
        mixed = [i for i, b in enumerate(got) if b not in (0x00, 0xFF)]
        periods = depth // 1000
        check("混合(跳变)字节数 == 2×周期数", len(mixed) == 2 * periods,
              "%d 个（应 %d）" % (len(mixed), 2 * periods))
        gaps = sorted({mixed[i + 1] - mixed[i] for i in range(len(mixed) - 1)})
        check("跳变字节间距 ∈ {62,63}（两个间距 = 一个周期 125 字节 = 1000 采样）",
              set(gaps) <= {62, 63}, gaps[:6])
        check("每个混合字节内部恰好 1 个跳变（= 每个边沿只落在一个字节里）",
              all(bin((got[i] ^ (got[i] >> 1)) & 0x7F).count("1") == 1 for i in mixed),
              "检查 %d 个" % len(mixed))

        smp = P.samples_of(st, 0, depth, rate * 1e6)[0]
        stt = P.waveform_stats(smp, rate * 1e6)
        check("RLE 展开后频率 = 1000Hz", abs(stt["frequency_hz"] - 1000) < 2, stt["frequency_hz"])
        check("RLE 展开后占空比 = 50%", abs((stt["duty_pct"] or 0) - 50) < 1, stt["duty_pct"])

        print("== 2) 对照：不压缩采集 ==")
        r2 = dev.acquire(rate, depth, trigger="any", mode="buffer", rle=False)
        got2 = bytes(r2["stream"].chan.get(0, b""))[:exp_bytes]
        mixed2 = [i for i, b in enumerate(got2) if b not in (0x00, 0xFF)]
        check("对照：同为 2×周期数 个跳变字节", len(mixed2) == 2 * periods, len(mixed2))
        pure1 = sum(1 for b in got if b in (0x00, 0xFF))
        pure2 = sum(1 for b in got2 if b in (0x00, 0xFF))
        check("对照：纯电平字节数一致（±2，相位可能差）", abs(pure1 - pure2) <= 2,
              "压缩 %d vs 原样 %d" % (pure1, pure2))
        check("对照：压缩后线上字节确实更少", r["wire_bytes"] < r2["wire_bytes"],
              "压缩 %d < 原样 %d" % (r["wire_bytes"], r2["wire_bytes"]))

        print("== 3) 静态信号（PWM0 停）→ 极限压缩 ==")
        dev.pwm_stop()
        time.sleep(0.2)
        r3 = dev.acquire(rate, depth, trigger="any", mode="buffer", rle=True)
        got3 = bytes(r3["stream"].chan.get(0, b""))[:exp_bytes]
        check("静态全 0", set(got3) == {0x00}, "非零字节 %d" % sum(1 for b in got3 if b))
        check("静态压缩率 > 99%", (r3["compression"] or 0) > 0.99,
              "线上 %d / 展开 %d" % (r3["wire_bytes"], r3["expanded_bytes"]))
        check("静态仍回满 depth", len(got3) == exp_bytes, len(got3))
    finally:
        try:
            dev.pwm_stop()
            dev.close()
        except Exception:
            pass

    print("\n结果:", "全部通过" if not FAIL else "%d 项失败" % FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
