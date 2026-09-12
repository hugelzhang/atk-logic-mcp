# -*- coding: utf-8 -*-
"""验证 200 MHz 档位是否存在（厂商存档里出现过 setHz=200000000、selectHzIndex=10）。

流程：① 发 0x11 参数命令，hzIndex=11（=第 11 档）看设备是否应答；
      ② 临时把 200 加进档位表，用它采一次 PWM0(1kHz) 看周期是不是 200000 采样（而不是垃圾）。
跑法（需设备空闲）: python tools/verify_rate_200mhz.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import atkproto as P                                            # noqa: E402

FAIL = 0


def check(name, ok, detail=""):
    global FAIL
    print(("  ✓ " if ok else "  ✗ ") + name + ("  " + str(detail) if detail else ""))
    if not ok:
        FAIL += 1


def main():
    P.setup_backend([os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))])
    print("现有档位表:", P.RATES, "MHz")
    dev = P.AtkDevice()
    try:
        print("\n① 发 0x11 参数命令 hzIndex=11（厂商表第 11 档 = 200MHz）")
        # [flags=Buffer][阈值 1.6V=0x10][hzIndex=11][深度 200000][触发深度 100000]
        payload = (bytes([P.FLAG_BUFFER, 0x10, 11])
                   + int(200000).to_bytes(5, "little")
                   + int(100000).to_bytes(5, "little"))
        dev.send(0x11, payload)
        resp = dev.read(0.6)
        print("     收到 %d 字节，前 48: %s" % (len(resp), resp[:48].hex(" ")))
        n2 = len(resp) - len(resp) % 2048
        frames = P.parse_frames(P.deinterleave(resp[:n2])) if n2 >= 2048 else P.parse_frames(resp)
        orders = [o for o, _ in frames]
        print("     帧 order:", orders[:10])
        check("设备对 hzIndex=11 有应答", len(resp) > 0, "(%d 字节)" % len(resp))

        print("\n② 用 200MHz 采一次 PWM0 1kHz（临时把 200 加进档位表）")
        if 200 not in P.RATES:
            P.RATES.append(200)
        dev.pwm_start(1000, 50)
        try:
            res = dev.acquire(rate_mhz=200, depth=600000, threshold_v=1.5,
                              trigger="rising", mode="buffer")
            samples = P.samples_of(res["stream"], 0, 600000, 200e6)[0]
            st = P.waveform_stats(samples, 200e6)
            print("     CH0: %d 采样, 频率 %.4f Hz, 占空比 %.2f%%, 周期中位 %s" %
                  (len(samples), st["frequency_hz"], st["duty_pct"], st.get("period_samples_median")))
            check("200MHz 档采到 PWM0 1kHz", abs(st["frequency_hz"] - 1000.0) < 5.0,
                  "%.3f Hz" % st["frequency_hz"])
            check("占空比 50%", abs(st["duty_pct"] - 50.0) < 2.0, "%.2f%%" % st["duty_pct"])
            check("周期 = 200000 采样（=200MHz/1kHz）",
                  abs((st.get("period_samples_median") or 0) - 200000) < 400,
                  st.get("period_samples_median"))
        finally:
            try:
                dev.pwm_stop()
            except Exception:
                pass
    finally:
        dev.close()
    print("\n结论:", "200MHz 档可用（应加进 P.RATES）" if not FAIL else "见上面的失败项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
