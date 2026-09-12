# -*- coding: utf-8 -*-
"""批量跑官方 11 个样例（照文件里存的官方解码器配置），看每层解出什么。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import srdhost                                                 # noqa: E402

TESTDIR = r"C:/Program Files/ATK-Logic/test"


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    files = sorted(f for f in os.listdir(TESTDIR) if f.lower().endswith(".atkdl"))
    for f in files:
        if only and only.lower() not in f.lower():
            continue
        path = os.path.join(TESTDIR, f)
        print("=" * 78)
        print("### %s" % f)
        t0 = time.time()
        try:
            chain, out = srdhost.from_atkdl(path, max_samples=1_500_000, annotation_limit=8)
        except Exception as e:                                   # noqa: BLE001
            print("  !! %s: %s" % (type(e).__name__, e))
            continue
        dt = time.time() - t0
        print("  链路: %s    用时 %.2f s" % (" → ".join(chain), dt))
        for layer in out:
            rows = layer["annotations"]
            total = layer["annotation_total"]
            print("  --- %s (通道 %s) 注解 %d 条" % (layer["protocol"], layer["channels"], total))
            for row, v in list(rows.items())[:6]:
                texts = " | ".join("/".join(it["text"]) for it in v["items"][:5])
                print("      [%s] %d 条: %s" % (row or "-", v["count"], texts[:150]))
            if layer["binary"]:
                b = list(layer["binary"].items())[:3]
                print("      binary: %s" % [(k, v["name"], v["bytes"]) for k, v in b])
    return 0


if __name__ == "__main__":
    sys.exit(main())
