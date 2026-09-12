# -*- coding: utf-8 -*-
"""合成 I²C 波形喂给 sigrok 的 i2c PD，验证 wait() 引擎（不依赖官方样例）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import srdhost                                                 # noqa: E402


def synth_i2c(addr, data, srate=10_000_000, scl_hz=100_000, pullup_low=False):
    """返回 (scl, sda) 两条 0/1 序列：START + addr(8bit) + ACK + data + ACK + STOP。"""
    half = srate // (scl_hz * 2)                 # 半个 SCL 周期的采样数
    scl, sda = [], []

    def idle(n=2):
        for _ in range(n * half):
            scl.append(1)
            sda.append(1)

    def bit(v):
        # SCL 低半周期建立数据，高半周期被采样
        for _ in range(half):
            scl.append(0)
            sda.append(v)
        for _ in range(half):
            scl.append(1)
            sda.append(v)

    def start():
        for _ in range(half):
            scl.append(1)
            sda.append(1)
        for _ in range(half):                    # SCL 高时 SDA 下降 = START
            scl.append(1)
            sda.append(0)
        for _ in range(half):
            scl.append(0)
            sda.append(0)

    def stop():
        for _ in range(half):                    # SCL 高时 SDA 上升 = STOP
            scl.append(1)
            sda.append(0)
        for _ in range(half):
            scl.append(1)
            sda.append(1)

    idle()
    start()
    for i in range(7, -1, -1):                   # 地址 MSB 先出
        bit((addr >> i) & 1)
    bit(0)                                       # ACK
    for i in range(7, -1, -1):
        bit((data >> i) & 1)
    bit(0)                                       # ACK
    stop()
    idle(4)
    return scl, sda


def main():
    scl, sda = synth_i2c(0x50, 0xA5)
    print("合成 I²C: %d 采样, 地址 0x50, 数据 0xA5" % len(scl))
    res = srdhost.run_chain(["i2c"], {"i2c": {"scl": 0, "sda": 1}},
                            {"i2c": {"address_format": "shifted", "packets_format": "hex"}},
                            10_000_000, {0: scl, 1: sda})
    r = res[-1][2]
    print("注解 %d  python 包 %d" % (len(r.annotations), len(r.python)))
    rows = r.rows
    for ss, es, idx, texts in r.annotations[:26]:
        print("   [%s] %d:%d %s" % (rows.get(idx, ("", ""))[1], ss, es, texts[:2]))
    for p in r.python[:8]:
        print("   py %d:%d %s" % (p[0], p[1], str(p[2])[:90]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
