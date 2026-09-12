# -*- coding: utf-8 -*-
"""离线验证：把刚存的 .atkdl 用 i2c → ssd1306 叠层链解一遍（不占设备、不需重启 Hermes）。

用途：验证 server.py 里"通道绑给链路最底层"的修正是对的。
"""
import sys, os, glob
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../atk-logic
sys.path.insert(0, BASE)

import srdhost as S
import atkdl


def _pick_path():
    """必须给 .atkdl 路径 —— 本脚本只对 I²C 波形有意义 (CH0=SCL, CH1=SDA)。

    早先这里硬编码了一个 C:\\Users\\<user>\\docs\\波形存档\\xxx.atkdl（工具当时的默认落盘位置），
    现在改成必须显式传路径，省得拿错文件喂给 i2c 解码器。
    """
    if len(sys.argv) > 1:
        return sys.argv[1]
    cands = []
    for sub in ("docs/波形存档", "captures"):
        cands += glob.glob(os.path.join(BASE, *sub.split("/"), "*.atkdl"))
    lines = ["用法: python verify_stack_ssd1306.py <某个 .atkdl>",
             "      (只对 I²C 波形有意义: CH0=SCL, CH1=SDA)"]
    lines += ["  现有存档:"] + ["  - " + os.path.relpath(c, BASE) for c in sorted(cands)]
    sys.exit("\n".join(lines))


PATH = _pick_path()
print("分析文件:", PATH)

chain = [S.resolve("i2c"), S.resolve("ssd1306")]
print("链路:", " -> ".join(chain))

snap = atkdl.read_atkdl(PATH)
hw = [0, 1]
start = min([a for a in (snap.first_activity(h) for h in hw) if a is not None] or [0])
start = max(0, start - 2000)
samples = {h: snap.window(h, start, 100000) for h in hw}   # 叠层解码慢，窗口别开大
print("窗口起点:", start, " 采样率:", snap.sample_rate_hz)

cmaps = {chain[0]: {"scl": 0, "sda": 1}}          # 通道只给最底层
opts_map = {chain[-1]: {}}
results = S.run_chain(chain, cmaps, opts_map, snap.sample_rate_hz, samples)

tx = S.transactions(results, max_items=40)
print("\n=== 叠层解码：事务列表（i2c + ssd1306 两层）===")
print(tx["text"])
print("\n按协议统计:", tx["by_protocol"])


# ---- 回归：直接走 server.py 的 bus_decode（离线 path 分支，不占设备）----
print("\n=== 回归 server.bus_decode(stack=...) 的通道绑定 ===")
import server as SV

r = SV.bus_decode(path=PATH, live=False, protocol="ssd1306", stack="i2c",
                  channels="scl=0,sda=1", depth=100000, max_samples=100000)
if "error" in r:
    print("  ✗ 失败:", r["error"])
else:
    proto_set = {it["protocol"] for it in r.get("transactions", [])}
    print("  ✓ 链路:", r.get("chain"), " 解出层:", sorted(proto_set),
          " 事务数:", r.get("transaction_total"))
    assert "i2c" in proto_set and "ssd1306" in proto_set, "两层都要有输出"
    print("  ✓ 两层都有输出（i2c 总线上叠 ssd1306 解码）")
