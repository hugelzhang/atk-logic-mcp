# -*- coding: utf-8 -*-
"""解剖官方 .atkdl 样例：条目结构 / channel.ini 字段 / 官方解码结果 JSON。

用法: python tools/atkdl_dump.py [样例目录]
"""
import json
import os
import sys
import zipfile

TESTDIR = r"C:/Program Files/ATK-Logic/test"


def dump(path):
    z = zipfile.ZipFile(path)
    infos = z.infolist()
    print("=" * 78)
    print("### %s  (%.1f KB, %d 条目)" % (os.path.basename(path),
                                          os.path.getsize(path) / 1024.0, len(infos)))
    dirs = sorted({i.filename.rstrip("/") for i in infos
                   if i.filename.endswith("/") and i.filename.rstrip("/")}, key=lambda s: int(s))
    bins, inis, others = {}, {}, []
    for i in infos:
        n = i.filename
        if n.endswith("/"):
            continue
        if "/" in n:
            ch, sub = n.split("/", 1)
            if sub.endswith(".bin"):
                bins.setdefault(int(ch), []).append((sub, i.file_size))
            elif sub.endswith(".ini"):
                inis[int(ch)] = z.read(n).decode("utf-8", "replace")
            else:
                others.append(n)
        else:
            others.append(n)
    print("通道目录: %s   其它条目: %s" % (dirs, others))
    for ch in sorted(inis):
        lines = [l for l in inis[ch].replace("\r\n", "\n").split("\n")]
        nonempty = [l for l in lines if l.strip()]
        data = sum(sz for _, sz in bins.get(ch, []))
        print("  CH%-2d 数据=%-9d 分片=%d  ini(%d行): %s" %
              (ch, data, len(bins.get(ch, [])), len(nonempty), nonempty[:5]))
        if ch == 0 and len(nonempty) > 6:
            print("        ini 其余行: %s ... 末行: %s" % (nonempty[5:9], nonempty[-1]))
    for n in others:
        raw = z.read(n)
        print("  [其它] %s  %d 字节" % (n, len(raw)))
        try:
            j = json.loads(raw.decode("utf-8", "replace"))
        except Exception as e:
            print("       非 JSON: %r (%s)" % (raw[:80], e))
            continue
        print("       顶层 keys: %s" % list(j.keys()))
        for k, v in j.items():
            if not isinstance(v, dict):
                print("       [%s] = %r" % (k, v))
                continue
            print("       [%s] keys=%s" % (k, list(v.keys())))
            for kk in ("channels", "opt_channels", "options", "annotation_rows"):
                if kk in v:
                    print("          %s = %s" % (kk, json.dumps(v[kk], ensure_ascii=False)[:300]))
            ann = v.get("annotations")
            if isinstance(ann, list):
                flat = []
                for row in ann:
                    if isinstance(row, list):
                        flat.extend(row)
                    else:
                        flat.append(row)
                print("          annotations: %d 条, 前 3: %s" %
                      (len(flat), json.dumps(flat[:3], ensure_ascii=False)[:400]))
                for a in flat:
                    if isinstance(a, dict) and "type" in a:
                        print("          annotation 字段名: %s" % list(a.keys()))
                        break
            if "binary" in v:
                b = v["binary"]
                print("          binary 类型=%s 长度=%s 前 200: %s" %
                      (type(b).__name__, len(b) if hasattr(b, "__len__") else "?",
                       json.dumps(b, ensure_ascii=False)[:200]))


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else TESTDIR
    files = sorted(f for f in os.listdir(d) if f.lower().endswith(".atkdl"))
    print("样例目录 %s 共 %d 个" % (d, len(files)))
    for f in files:
        dump(os.path.join(d, f))
    return 0


if __name__ == "__main__":
    sys.exit(main())
