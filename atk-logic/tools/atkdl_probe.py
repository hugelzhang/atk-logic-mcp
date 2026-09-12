# -*- coding: utf-8 -*-
"""读官方 ATK-Logic 样例波形 .atkdl (ZIP) 并验证我们的 UART 解码器。

用法: python tools/atkdl_probe.py "C:/Program Files/ATK-Logic/test/uart_tx_115200.atkdl"
"""
import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import atkproto as P                                    # noqa: E402


def read_atkdl(path):
    z = zipfile.ZipFile(path)
    chans = {}
    meta_json = None
    for info in z.infolist():
        name = info.filename
        if name.endswith("/"):
            continue
        if name.endswith("channel.ini"):
            txt = z.read(name).decode("utf-8", "replace")
            lines = [l.strip() for l in txt.replace("\r\n", "\n").split("\n") if l.strip()]
            ch = int(lines[0].split()[-1]) if lines and lines[0].lower().startswith("channel") else None
            chans.setdefault(ch, {})["ini"] = lines
        elif name.endswith(".bin"):
            ch = int(name.split("/")[0]) if "/" in name else None
            chans.setdefault(ch, {}).setdefault("parts", []).append((name, z.read(name)))
        else:
            try:
                meta_json = json.loads(z.read(name).decode("utf-8", "replace"))
            except Exception:
                pass
    return chans, meta_json


def main():
    path = sys.argv[1]
    chans, meta = read_atkdl(path)
    print("=" * 72)
    print("样例:", os.path.basename(path), " 通道数:", len([c for c in chans if c is not None]))
    for ch in sorted(c for c in chans if c is not None):
        ini = chans[ch].get("ini", [])
        parts = chans[ch].get("parts", [])
        total = sum(len(d) for _, d in parts)
        print("  CH%-2d ini=%s  数据 %d 字节(%d 采样)  分片 %d" %
              (ch, ini[:6], total, total * 8, len(parts)))
    if meta:
        top = list(meta.keys())
        print("  官方解码结果 JSON 顶层:", top[:8])
        for proto in top:
            node = meta[proto]
            if isinstance(node, dict):
                print("    [%s] keys=%s" % (proto, list(node.keys())[:8]))
    # 找一个有活动的通道, 用我们的解码器跑
    target = None
    srate = None
    for ch in sorted(c for c in chans if c is not None):
        data = b"".join(d for _, d in chans[ch].get("parts", []))
        if not data:
            continue
        ones = sum(bin(b).count("1") for b in data[:4096])
        if 0 < ones < len(data[:4096]) * 8:            # 有高有低 = 有活动
            target, target_data = ch, data
            ini = chans[ch].get("ini", [])
            for l in ini:
                if l.replace(".", "").isdigit() and len(l) > 5:
                    srate = int(l)
                    break
            break
    if target is None:
        print("没找到有活动的通道"); return 1
    print("\n用有活动的 CH%d 验证我们的解码器 (采样率字段=%s)" % (target, srate))
    samples = P.bits_from_bytes(target_data)
    st = P.waveform_stats(samples, srate or 1_000_000)
    print("  统计: 采样=%d 高电平=%.2f%% 上升沿=%d 频率=%s" %
          (st["points"], st["high_pct"], st["rising_edges"], st["frequency_hz"]))
    for baud in (115200, 9600, None):
        dec = P.uart_decode(samples, srate or 1_000_000, baud=baud, max_bytes=64)
        print("  baud=%s → 测得 %.0f bps, 解出 %s 字节, text=%r" %
              (baud, dec.get("detected_baud", 0), dec.get("bytes_decoded"),
               (dec.get("text") or "")[:60]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
