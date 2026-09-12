# -*- coding: utf-8 -*-
"""端到端验证：离线 load_waveform + 真机 save_capture(fmt="atkdl") 回读。

跑法（需设备空闲）: python tools/verify_atkdl_e2e.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import atkdl                                                    # noqa: E402
import atkproto as P                                            # noqa: E402
import server                                                   # noqa: E402

FAIL = 0


def check(name, ok, detail=""):
    global FAIL
    print(("  ✓ " if ok else "  ✗ ") + name + ("  " + str(detail) if detail else ""))
    if not ok:
        FAIL += 1


print("== 1) 离线 load_waveform（不占设备）==")
r = server.load_waveform(path=r"C:/Program Files/ATK-Logic/test/uart_tx_115200.atkdl",
                         channel=-1, max_samples=600000, uart_baud=-1)
check("没有 error", "error" not in r, r.get("error", ""))
check("采样率 10MHz", r.get("sample_rate_hz") == 10_000_000, r.get("sample_rate_hz"))
check("会话名", r.get("session") == "DL16 Plus", r.get("session"))
check("截断标记", r.get("excerpt", {}).get("truncated") is True, r.get("excerpt"))
check("官方解码器列表", "UART" in (r.get("decoders") or []), r.get("decoders"))
u = r.get("uart") or {}
check("UART 解码含 ALIENTEK", "ALIENTEK" in (u.get("text") or ""), repr((u.get("text") or "")[:24]))
check("UART 波特率 ≈115200", 112000 <= (u.get("detected_baud") or 0) <= 119000, u.get("detected_baud"))
print("     统计:", {k: r["stats"][k] for k in ("points", "high_pct", "rising_edges") if k in r["stats"]})

print("\n== 2) 真机采集 → 写 .atkdl → 回读 ==")
dev = P.AtkDevice()
try:
    dev.pwm_start(1000, 50)
    out = os.path.join(tempfile.gettempdir(), "atk_export_test.atkdl")
    if os.path.exists(out):
        os.remove(out)
    txt = server.save_capture(channel=0, depth=200000, sample_rate_mhz=1, fmt="atkdl",
                              out_file=out, tag="e2e")
    print("     save_capture 返回:", txt.replace("\n", " | ")[:160])
    check("文件已写出", os.path.exists(out), out)
    s = atkdl.read_atkdl(out)
    st = s.stats(0, limit=200000)
    print("     回读:", s.summary())
    check("回读采样率", s.sample_rate_hz == 1_000_000, s.sample_rate_hz)
    check("回读深度", s.depth == 200000, s.depth)
    check("回读活动通道", s.active_channels() == [0], s.active_channels())
    check("PWM 频率 ≈1000Hz", abs(st["frequency_hz"] - 1000.0) < 1.0, "%.2f Hz" % st["frequency_hz"])
    check("占空比 ≈50%", abs(st["duty_pct"] - 50.0) < 1.0, "%.2f%%" % st["duty_pct"])
    check("重采样后 UART 表可读", isinstance(s.settings.get("settingData"), dict),
          s.settings.get("settingData"))
finally:
    dev.pwm_stop()
    dev.close()

print("\n结果:", "全部通过" if not FAIL else "%d 项失败" % FAIL)
sys.exit(1 if FAIL else 0)
