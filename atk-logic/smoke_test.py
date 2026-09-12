# -*- coding: utf-8 -*-
"""ATK-Logic 硬件自检 (真机, 不需要额外接线)。

覆盖: 设备连接 → PWM0 自环采集 → 频率/占空比/毛刺 → 档位一致性 → 触发模式 →
      多通道 → 全量存盘 → USB 重连 → UART 解码(离线合成; 若发现串口则做在线实测)。

用法:
    <venv-python> smoke_test.py            # 全部
    <venv-python> smoke_test.py --quick    # 跳过存盘/在线 UART

注意: 运行时**不要让别的东西占用设备** (Hermes / Claude 的 atk-logic MCP 会占用)。
      需要 CH0 接着逻辑分析仪自带的 PWM0 输出 (出厂跳线/一根杜邦线)。
"""
import os
import sys
import time
import glob
import json
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import atkproto as P

P.setup_backend([_HERE, os.path.join(_HERE, "..")])

QUICK = "--quick" in sys.argv
RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("  ✓ " if ok else "  ✗ ") + name + ((" — " + detail) if detail else ""))


def approx(a, b, tol):
    return a is not None and abs(a - b) <= tol


def grab(dev, ch=0, depth=200000, rate=1, trigger="rising", mode="buffer", extra=()):
    res = dev.acquire(rate_mhz=rate, depth=depth, channel=ch, trigger=trigger,
                      mode=mode, extra_channels=extra)
    st = res["stream"]
    return res, P.bits_from_bytes(st.channel_bytes(ch, depth))


def main():
    print("=" * 74)
    print("ATK-Logic 硬件自检")
    dev = P.AtkDevice()
    try:
        print("\n[1] 设备连接")
        info = dev.call(lambda d: "%s %s SN=%s" % (d.manufacturer, d.product,
                                                   d.serial_number or "N/A"))
        record("识别设备", True, info)

        print("\n[2] PWM0 自环采集 (1kHz 50%, 1MHz 档)")
        dev.wake()
        dev.pwm_start(1000, 50)
        time.sleep(0.2)
        res, s = grab(dev, 0, 200000, 1)
        st = P.waveform_stats(s, 1e6)
        record("采到 %d 采样" % st["points"], st["points"] == 200000, "complete=%s" % res["complete"])
        record("频率 ≈ 1000 Hz", approx(st["frequency_hz"], 1000.0, 5.0), "实得 %.2f Hz" % (st["frequency_hz"] or -1))
        record("占空比 ≈ 50%", approx(st["high_pct"], 50.0, 1.0), "实得 %.2f%%" % st["high_pct"])
        record("无毛刺/窄脉冲", st["glitch_edges"] == 0 and st["narrow_pulses"] == 0,
               "glitch=%d narrow=%d" % (st["glitch_edges"], st["narrow_pulses"]))
        record("周期稳定", st["period_samples_min"] == st["period_samples_max"] == 1000,
               "min/med/max = %s/%s/%s" % (st["period_samples_min"], st["period_samples_median"], st["period_samples_max"]))

        print("\n[3] 改 2kHz 复核")
        dev.pwm_start(2000, 50)
        time.sleep(0.2)
        res, s = grab(dev, 0, 200000, 1)
        st = P.waveform_stats(s, 1e6)
        record("频率 ≈ 2000 Hz", approx(st["frequency_hz"], 2000.0, 10.0), "实得 %.2f Hz" % (st["frequency_hz"] or -1))

        print("\n[4] 档位一致性 (同一信号 1MHz vs 25MHz)")
        f = {}
        dev.pwm_start(1000, 50)
        time.sleep(0.2)
        for rate in (1, 25):
            _, s = grab(dev, 0, 200000, rate)
            f[rate] = P.waveform_stats(s, rate * 1e6)["frequency_hz"]
        record("两档频率一致且 ≈1000Hz", approx(f[1], f[25], 2.0) and approx(f[1], 1000.0, 5.0),
               "1MHz=%.2fHz 25MHz=%.2fHz" % (f[1], f[25]))

        print("\n[5] 触发模式 (mode='trigger', ch0 上升沿)")
        res, s = grab(dev, 0, 100000, 1, trigger="rising", mode="trigger")
        st = P.waveform_stats(s, 1e6)
        record("等触发后完成采集", res["complete"] and st["points"] == 100000,
               "elapsed=%.2fs" % res["elapsed_s"])
        record("order-3 触发偏移有回值", res["stream"].trig_offset is not None,
               "offset=%s 采样 (语义未完全确定, 仅参考)" % res["stream"].trig_offset)
        record("触发模式仍能测出频率", approx(st["frequency_hz"], 1000.0, 10.0),
               "%.2f Hz" % (st["frequency_hz"] or -1))
        # 实测事实: 本机固件(FT2232+FPGA)不理会"等待触发"标志, 无跳变信号也照录满 → 断言记录下来
        dev.pwm_start(1000, 0)          # 占空比 0% → CH0 恒定低电平(无跳变)
        time.sleep(0.3)
        res2, s2 = grab(dev, 0, 50000, 1, trigger="rising", mode="trigger")
        st2 = P.waveform_stats(s2, 1e6)
        record("固化行为: 触发条件在本机固件不生效 (无跳变也录满)",
               res2["complete"] and st2["points"] == 50000 and st2["rising_edges"] == 0,
               "恒定低电平仍回 %d 采样 (与厂商协议文档不同, 已在 README 记录)" % st2["points"])
        dev.pwm_start(1000, 50)
        time.sleep(0.2)

        print("\n[6] 多通道一次采集 (CH0~CH3)")
        res = dev.acquire(rate_mhz=1, depth=100000, channel=0,
                          extra_channels=(1, 2, 3), trigger="rising", mode="buffer")
        stats = {}
        for c in (0, 1, 2, 3):
            smp = P.bits_from_bytes(res["stream"].channel_bytes(c, 100000))
            stats["CH%d" % c] = P.waveform_stats(smp, 1e6)
        record("4 通道都有 %d 采样" % 100000,
               all(v["points"] == 100000 for v in stats.values()),
               " ".join("%s:%.0fHz/%.1f%%" % (k, v["frequency_hz"] or 0, v["high_pct"]) for k, v in stats.items()))
        record("CH0 有信号, 其余为悬空噪声(不判定)", True,
               "CH1 高电平=%.1f%%" % stats["CH1"]["high_pct"])

        if not QUICK:
            print("\n[7] 全量存盘 (depth=80000, CH0+CH1)")
            tmp = tempfile.mkdtemp(prefix="atk_smoke_")
            res = dev.acquire(rate_mhz=1, depth=80000, channel=0, extra_channels=(1,),
                              trigger="rising", mode="buffer")
            ok = True
            for c in (0, 1):
                smp = P.bits_from_bytes(res["stream"].channel_bytes(c, 80000))
                csv = os.path.join(tmp, "ch%d.csv" % c)
                with open(csv, "w", encoding="utf-8") as f:
                    f.write("time_s,level\n")
                    for i, v in enumerate(smp):
                        f.write("%.9f,%d\n" % (i / 1e6, v))
                binp = os.path.join(tmp, "ch%d.bin" % c)
                with open(binp, "wb") as f:
                    f.write(res["stream"].channel_bytes(c, 80000))
                lines = sum(1 for _ in open(csv, encoding="utf-8")) - 1
                ok &= (lines == 80000 and os.path.getsize(binp) == 80000 // 8)
                print("     ch%d: csv %d 行, bin %d 字节" % (c, lines, os.path.getsize(binp)))
            record("CSV 行数/ BIN 字节数与深度一致", ok, tmp)

        print("\n[8] USB 句柄重连 (EIO 恢复路径)")
        dev.reconnect(wake=True)
        _, s = grab(dev, 0, 50000, 1)
        st = P.waveform_stats(s, 1e6)
        record("重连后仍能采集", approx(st["frequency_hz"], 1000.0, 20.0),
               "重连 %d 次, 频率 %.2f Hz" % (dev.reconnects, st["frequency_hz"] or -1))

        print("\n[9] UART 解码 (离线合成波形)")
        msg = b"SMOKE UART 115200 ok\r\n"
        bit = 25_000_000 / 115200.0
        wave = bytearray([1] * 20)
        for byte in msg:
            for k, bb in enumerate([0] + [(byte >> b) & 1 for b in range(8)] + [1]):
                a, z = int(round(k * bit)), int(round((k + 1) * bit))
                wave += bytearray([bb] * max(1, z - a))
        wave += bytearray([1] * 40)
        d = P.uart_decode(bytes(wave), 25_000_000, baud=115200)
        record("合成波形解码无误", bytes.fromhex(d["hex"].replace(" ", "")) == msg,
               "测得 %.0f bps, %d 字节" % (d["detected_baud"], d["bytes_decoded"]))

        if not QUICK:
            print("\n[10] UART 在线实测 (若 CH340 有接到某个通道)")
            try:
                import serial
                from serial.tools import list_ports
                ports = [p.device for p in list_ports.comports()]
                if not ports:
                    print("     未发现串口 → 跳过 (接上 CH340 TX→任一通道 可做真机 UART 验证)")
                else:
                    ser = serial.Serial(ports[0], 115200, timeout=0.5)
                    time.sleep(0.2)
                    res = dev.acquire(rate_mhz=25, depth=4_000_000, channel=0,
                                      trigger="falling", mode="buffer")
                    t0 = time.time()
                    while time.time() - t0 < 1.2:
                        ser.write(b"ATK-LOGIC-UART-REALTEST\r\n")
                        time.sleep(0.05)
                    ser.close()
                    hits = []
                    for c in range(P.CHANNELS):
                        smp = P.bits_from_bytes(res["stream"].channel_bytes(c, 4_000_000))
                        dec = P.uart_decode(smp, 25e6, baud=None, max_bytes=200)
                        if dec.get("text") and "ATK-LOGIC-UART-REALTEST" in dec["text"]:
                            hits.append((c, dec["detected_baud"], dec["text"][:40]))
                    record("在线 UART 解码命中", bool(hits),
                           ("CH%s 测得 %.0f bps: %r" % hits[0]) if hits else
                           "端口 %s 已发数据但 16 通道都没解出该串 → 大概率 CH340 没接到通道上" % ports[0])
            except ImportError:
                print("     venv 无 pyserial → 跳过在线 UART 测试 (pip install pyserial)")
            except Exception as e:
                print("     在线 UART 测试异常: %s" % e)

    finally:
        try:
            dev.pwm_stop(0)
        except Exception:
            pass
        try:
            dev.stop()
        except Exception:
            pass
        dev.close()

    print("\n" + "=" * 74)
    bad = [r for r in RESULTS if not r[1]]
    print("结果: %d 项通过 / %d 项失败" % (len(RESULTS) - len(bad), len(bad)))
    for name, _, detail in bad:
        print("  ✗ %s — %s" % (name, detail))
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except P.DeviceOccupied as e:
        print("\n设备被占用，自检未开始：\n%s" % e)
        sys.exit(2)
