# -*- coding: utf-8 -*-
"""排查 9600bps@1MHz 解码不一致的原因 (合成波形位宽取整 vs 理想位宽)。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import atkproto as P

msg = "ATK-Logic 逻辑分析仪 UART 测试 ok".encode("gbk")

def synth_uart_exact(data, srate_hz, baud, lead=20, trail=40):
    """按浮点边界生成 (位宽不取整): 每个位用 round(边界) 切分, 消除取整误差。"""
    s = bytearray([1] * lead)
    bp = srate_hz / float(baud)
    for idx, byte in enumerate(data):
        edges = [idx * 0 + 0]
        bits = [0] + [(byte >> b) & 1 for b in range(8)] + [1]
        for k, bit in enumerate(bits):
            a = int(round((k) * bp))
            b = int(round((k + 1) * bp))
            s += bytearray([bit] * max(1, b - a))
    s += bytearray([1] * trail)
    return bytes(s)

for label, gen in (("取整位宽(测试用)", None), ("浮点边界", synth_uart_exact)):
    wave = synth_uart_exact(msg, 1_000_000, 9600) if gen else None
    if wave is None:
        bit = 1_000_000 / 9600.0
        s = bytearray([1] * 20)
        for byte in msg:
            s += bytearray([0] * int(round(bit)))
            for b in range(8):
                s += bytearray([(byte >> b) & 1] * int(round(bit)))
            s += bytearray([1] * int(round(bit)))
        wave = bytes(s) + bytearray([1] * 40)
    for baud_arg in (9600, None):
        d = P.uart_decode(wave, 1_000_000, baud=baud_arg)
        got = bytes.fromhex(d.get("hex", "").replace(" ", ""))
        first_bad = next((i for i in range(min(len(got), len(msg))) if got[i] != msg[i]), None)
        print(f"[{label}] baud={baud_arg}: 解码 {len(got)}/{len(msg)} 字节, 帧错误={d.get('framing_errors')}, "
              f"位宽={d.get('bit_width_samples')}, 首个不符位置={first_bad}")
        if first_bad is not None:
            print(f"    期望 {msg[max(0,first_bad-3):first_bad+4].hex(' ')}  实得 {got[max(0,first_bad-3):first_bad+4].hex(' ')}")
