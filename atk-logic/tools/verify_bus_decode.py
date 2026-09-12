# -*- coding: utf-8 -*-
"""验证 bus_decode（MCU 总线调试工具）：照存档解、手工通道解、probe 摸通道、真机采集解。

用法（真机部分需设备空闲）：python tools/verify_bus_decode.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server                                                   # noqa: E402

TESTDIR = r"C:/Program Files/ATK-Logic/test"
FAIL = 0


def check(name, ok, detail=""):
    global FAIL
    print(("  ✓ " if ok else "  ✗ ") + name + ("  " + str(detail) if detail else ""))
    if not ok:
        FAIL += 1


def main():
    print("== 1) 照存档里官方配置解（离线）==")
    r = server.bus_decode(path=os.path.join(TESTDIR, "eeprom_24c02_i2c.atkdl"), live=False,
                          max_items=12)
    check("无 error", "error" not in r, r.get("error", ""))
    check("链路 i2c→eeprom24xx", r.get("chain") == ["i2c", "eeprom24xx"], r.get("chain"))
    check("事务里含 Byte write", "Byte write" in (r.get("text") or ""))
    print("     事务列表（前 6 行）:")
    for line in (r.get("text") or "").split("\n")[:6]:
        print("       " + line)

    print("\n== 2) 手工通道解（离线）==")
    r2 = server.bus_decode(path=os.path.join(TESTDIR, "eeprom_24c02_i2c.atkdl"), live=False,
                           protocol="i2c", channels="scl=7,sda=6",
                           options="address_format=shifted", max_samples=600000, max_items=8)
    check("解出 i2c 事务", (r2.get("transaction_total") or 0) > 0,
          "chain=%s total=%s" % (r2.get("chain"), r2.get("transaction_total")))

    print("\n== 3) probe 摸通道（离线，UART 样例 → CH0 应被认成 UART）==")
    r3 = server.bus_decode(path=os.path.join(TESTDIR, "uart_tx_115200.atkdl"), live=False,
                           probe=True, max_samples=600000)
    probe = (r3.get("probe") or {}).get(0, {})
    check("CH0 识别为 UART 候选", "uart_guess" in probe, probe.get("looks_like"))
    if "uart_guess" in probe:
        check("自动波特率 ≈115200", 112000 <= (probe["uart_guess"]["baud"] or 0) <= 119000,
              probe["uart_guess"]["baud"])
        check("试解出 ALIENTEK", "ALIENTEK" in probe["uart_guess"]["text"])

    print("\n== 4) 真机：probe 全 16 通道 + PWM0 采一次解事务 ==")
    try:
        import atkproto as P
        dev = P.AtkDevice()
        dev.pwm_start(1000, 50)
        dev.close()
    except Exception as e:                                        # noqa: BLE001
        print("  跳过真机部分（设备不可用）: %s" % e)
        print("\n结果:", "全部通过" if not FAIL else "%d 项失败" % FAIL)
        return 1 if FAIL else 0
    try:
        r4 = server.bus_decode(live=True, probe=True, depth=400000, sample_rate_mhz=1,
                               trigger="any", save=False)
        pr = r4.get("probe") or {}
        check("真机 probe 有结果", bool(pr), "通道数 %d" % len(pr))
        ch0 = pr.get(0, {})
        check("CH0 是 PWM0 1kHz", abs((ch0.get("freq_hz") or 0) - 1000) < 5, ch0.get("freq_hz"))
        r5 = server.bus_decode(live=True, protocol="pwm", channels="data=0", depth=400000,
                               sample_rate_mhz=1, trigger="any", max_items=6)
        check("真机 PWM 解码出事务", (r5.get("transaction_total") or 0) > 0,
              "total=%s" % r5.get("transaction_total"))
        check("自动存了 .atkdl 证据", bool(r5.get("saved_atkdl")) and
              os.path.exists(r5.get("saved_atkdl") or ""), r5.get("saved_atkdl"))
        print("     事务(前 2 行):")
        for line in (r5.get("text") or "").split("\n")[:2]:
            print("       " + line)
    finally:
        try:
            dev2 = P.AtkDevice()
            dev2.pwm_stop()
            dev2.close()
        except Exception:
            pass
    print("\n结果:", "全部通过" if not FAIL else "%d 项失败" % FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
