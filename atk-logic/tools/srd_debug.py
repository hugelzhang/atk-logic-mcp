# -*- coding: utf-8 -*-
"""调试 sigrok PD：打印前 N 次 wait() 的条件/命中/引脚值，定位解码器没出东西的原因。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import atkdl                                                    # noqa: E402
import srdhost                                                 # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from srd_try import saved_config                                # noqa: E402

TESTDIR = r"C:/Program Files/ATK-Logic/test"


def main():
    proto = sys.argv[1] if len(sys.argv) > 1 else "uart"
    name = sys.argv[2] if len(sys.argv) > 2 else "uart_tx_115200.atkdl"
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 200000
    log_n = int(sys.argv[4]) if len(sys.argv) > 4 else 40
    s = atkdl.read_atkdl(os.path.join(TESTDIR, name))
    cmap, opts = saved_config(s, {"uart": "UART", "i2c": "I²C", "spi": "SPI",
                                  "modbus": "Modbus"}.get(proto, proto))
    chans = sorted(set(cmap.values()))
    samples = {hw: s.window(hw, 0, n) for hw in chans}
    print("通道映射 %s  选项 %s  采样率 %d  窗口 %d" % (cmap, opts, s.sample_rate_hz, n))

    orig = srdhost.PdRun._host_wait
    state = {"n": 0}

    def spy(self, cond):
        r = orig(self, cond)
        state["n"] += 1
        if state["n"] <= log_n:
            print("  wait#%-3d cond=%-46s -> idx=%-8d pins=%s matched=%s" %
                  (state["n"], str(cond)[:46], self.dec.samplenum, r, self.dec.matched))
        return r

    srdhost.PdRun._host_wait = spy
    try:
        res = srdhost.run_chain([proto], {proto: cmap}, {proto: opts}, s.sample_rate_hz, samples)
    except srdhost.PdError as e:
        print("!! PD 报错:", e)
        print("   wait 调用次数:", state["n"])
        return 1
    srdhost.PdRun._host_wait = orig
    print("wait 总次数:", state["n"])
    print("注解:", len(res[-1][2].annotations), " python 包:", len(res[-1][2].python))
    for ss, es, idx, texts in res[-1][2].annotations[:25]:
        print("   [%d:%d] idx=%d %s" % (ss, es, idx, texts[:2]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
