# -*- coding: utf-8 -*-
"""离线验证：把刚存的 .atkdl 用 i2c → ssd1306 叠层链解一遍（不占设备、不需重启 Hermes）。

用途：验证 server.py 里"通道绑给链路最底层"的修正是对的。
"""
import sys, os
BASE = r"D:/MCP/01-atk-logic/atk-logic"
sys.path.insert(0, BASE)

import srdhost as S
import atkdl

PATH = sys.argv[1] if len(sys.argv) > 1 else r"C:/Users/27321/docs/波形存档/atk_bus_20260912_225351.atkdl"

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
