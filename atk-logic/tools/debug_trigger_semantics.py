# -*- coding: utf-8 -*-
"""判定: ① 窄脉冲计数是否来自窗口边界半脉冲; ② mode=buffer/trigger 的真实差别。
手法: PWM0 占空比 0% → CH0 被驱动成恒定低电平(无跳变) → "等触发"应该永远等不到。"""
import os, sys, time
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)
import atkproto as P
P.setup_backend([_HERE, os.path.join(_HERE, "..")])

dev = P.AtkDevice()
dev.wake()

def stats(depth=100000, rate=1, mode="buffer", trig="rising", timeout=3.0):
    res = dev.acquire(rate_mhz=rate, depth=depth, channel=0, trigger=trig,
                      mode=mode, timeout_s=timeout)
    s = P.bits_from_bytes(res["stream"].channel_bytes(0, depth))
    st = P.waveform_stats(s, rate * 1e6)
    return res, st

print("=" * 72)
print("[A] 窄脉冲位置 (1kHz 方波, depth=200000): 看是否在窗口首尾")
dev.pwm_start(1000, 50); time.sleep(0.3)
res, st = stats(200000, 1, "buffer")
s = P.bits_from_bytes(res["stream"].channel_bytes(0, 200000))
# 手动算脉冲宽度
widths, cur, cnt = [], s[0], 1
for v in s[1:]:
    if v == cur: cnt += 1
    else: widths.append((cur, cnt)); cur, cnt = v, 1
widths.append((cur, cnt))
narrow = [(i, lv, w) for i, (lv, w) in enumerate(widths) if w < 500]
print(f"  脉冲总数={len(widths)} 窄脉冲(宽度<500采样)={narrow[:6]}")
print(f"  首个脉冲={widths[0]} 末个脉冲={widths[-1]}  (首尾被窗口截断即为'半脉冲')")
print(f"  统计字段: narrow_pulses={st['narrow_pulses']} glitch_edges={st['glitch_edges']} "
      f"freq={st['frequency_hz']} period(min/med/max)={st['period_samples_min']}/{st['period_samples_median']}/{st['period_samples_max']}")

print("\n[B] PWM0 占空比 0% → CH0 恒定低电平 (无跳变信号)")
dev.pwm_start(1000, 0); time.sleep(0.3)
res, st = stats(100000, 1, "buffer")
print(f"  buffer 模式: complete={res['complete']} 采样={st['points']} 高电平={st['high_pct']}% "
      f"上升沿={st['rising_edges']} offset={res['stream'].trig_offset}")
res, st = stats(100000, 1, "trigger", "rising", timeout=4.0)
print(f"  trigger 模式(上升沿, 无跳变): complete={res['complete']} 采样={st['points']} "
      f"offset={res['stream'].trig_offset} → {'仍在等触发(无数据) ✓ 说明触发模式真的在等条件' if st['points'] == 0 else '仍回数据 → 触发标志无效'}")
res, st = stats(100000, 1, "trigger", "low", timeout=4.0)
print(f"  trigger 模式(低电平触发, 条件已满足): complete={res['complete']} 采样={st['points']} offset={res['stream'].trig_offset}")

print("\n[C] 触发深度语义: trigger 模式下把触发深度设到 1/4 与 3/4 看 order-3 偏移")
dev.pwm_start(1000, 50); time.sleep(0.3)
for frac in (0.25, 0.75):
    dev.wake()
    d = 100000
    payload = bytes([0x00, 0x0F, 1]) + d.to_bytes(5, 'little') + int(d * frac).to_bytes(5, 'little')
    dev.send(0x11, payload); time.sleep(0.25); dev.drain(0.2)
    dev.send(0x12, P.trigger_bytes({0: "R"}, immediate=False))
    full = dev.read(4.0)
    n2 = len(full) - len(full) % 2048
    fr = P.parse_frames(P.deinterleave(full[:n2])) if n2 else []
    off = next((int.from_bytes(p[2:7], 'little') for o, p in fr if o == 3 and len(p) >= 7), None)
    nch0 = sum(len(p) - 2 for o, p in fr if o == 1 and len(p) >= 2 and p[0] == 0)
    print(f"  触发深度={int(d*frac)} (深度 {d}): order3 偏移={off}, CH0 字节={nch0} ({nch0*8} 采样)")

dev.pwm_stop(0); dev.stop(); dev.close()
print("\n完成")
