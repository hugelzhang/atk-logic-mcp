# -*- coding: utf-8 -*-
"""试跑 sigrok PD（离线，用 .atkdl 里存的官方解码器配置）。

用法: python tools/srd_try.py <协议名> [<样例.atkdl>] [--stack 下层协议] [--samples N]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import atkdl                                                    # noqa: E402
import srdhost                                                 # noqa: E402

TESTDIR = r"C:/Program Files/ATK-Logic/test"


def saved_config(session, proto):
    """从 .atkdl 的解码器 JSON 里取该协议的通道/选项配置。"""
    d = (session.decode or {}).get(proto)
    if not isinstance(d, dict):
        return {}, {}
    cmap = {}
    for c in (d.get("channels") or []) + (d.get("opt_channels") or []):
        v = str(c.get("value", "-")).strip()
        if v.isdigit():
            cmap[c["id"]] = int(v)
    opts = {}
    for o in (d.get("options") or []):
        opts[o["id"]] = o.get("value")
    return cmap, opts


def main():
    proto = sys.argv[1]
    path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(TESTDIR, "uart_tx_115200.atkdl")
    stack = []
    n = 300000
    if "--stack" in sys.argv:
        stack = [x for x in sys.argv[sys.argv.index("--stack") + 1].split(",") if x]
    if "--samples" in sys.argv:
        n = int(sys.argv[sys.argv.index("--samples") + 1])
    s = atkdl.read_atkdl(path)
    print("样例 %s  采样率 %d Hz  深度 %s  活动通道 %s" %
          (os.path.basename(path), s.sample_rate_hz, s.depth, s.active_channels()))
    print("文件里的解码器:", list((s.decode or {}).keys()))

    protocols = stack + [proto]
    cmaps, opts = {}, {}
    for p in protocols:
        c, o = saved_config(s, {"uart": "UART", "i2c": "I²C", "modbus": "Modbus", "spi": "SPI"}.get(p, p))
        cmaps[p], opts[p] = c, o
        print("  %-8s 通道=%s 选项=%s" % (p, c, {k: v for k, v in list(o.items())[:4]}))
    chans = sorted({hw for c in cmaps.values() for hw in c.values()})
    samples = {hw: s.window(hw, 0, n) for hw in chans}
    print("参与解码的硬件通道: %s（各取前 %d 采样）" % (chans, n))
    res = srdhost.run_chain(protocols, cmaps, opts, s.sample_rate_hz, samples)
    out = srdhost.summarize(res, max_items=30)
    for item in out:
        print("\n=== %s ===" % item["protocol"])
        print("  通道:", item["channels"])
        for row, v in item["annotations"].items():
            print("  [%s] %s 条:" % (row, v["count"]))
            for it in v["items"][:14]:
                print("      %s %s" % (it["t"], it["text"][:3]))
        if item["binary"]:
            print("  binary:", json.dumps(item["binary"], ensure_ascii=False)[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
