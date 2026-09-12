# -*- coding: utf-8 -*-
"""从 USBPcap/Wireshark 抓包中还原 ATK 逻辑分析仪的 USB 流量（协议考古用）。

用途：把\"厂商上位机实际发了什么、设备回了什么\"变成可读文本，用来定 order-3 语义、
触发配置的真实用法、RLE 载荷等未解项。

用法:
    # 1) 抓包 (USBPcap 是根集线器过滤驱动, 不会独占设备; 但只在设备空闲时抓最干净)
    USBPcapCMD.exe -d \\.\\USBPcap1 --devices <USB地址> -o tools/usbcap/x.pcap
    # 2) 还原
    <venv-python> tools/usbcap_extract.py tools/usbcap/x.pcap
    # 可选: --raw 打印未解析的原始块

依赖: tshark.exe（读 pcap 不需要 Npcap, 只有实时抓包才需要）; 本脚本不需要 pyusb。
"""
import os
import re
import sys
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
import atkproto as P                                # noqa: E402

TSHARK = os.environ.get("TSHARK", r"C:\Program Files\Wireshark\tshark.exe")
VID, PID = P.VID, P.PID


def tshark_rows(pcap, addr=None):
    """取每个 USB 记录的 端点/传输类型/长度/载荷。

    注意: USBPcap 按 **USB 包**(512B) 记录, 不是按 URB; 且不注入了描述符时
    tshark 无法按 idVendor/idProduct 过滤(设备在抓包前已枚举) → 用设备地址过滤。
    """
    flt = "usb.endpoint_address" if addr is None else "usb.device_address==%d" % addr
    cmd = [TSHARK, "-r", pcap, "-Y", flt,
           "-T", "fields", "-e", "frame.number", "-e", "usb.endpoint_address",
           "-e", "usb.transfer_type", "-e", "usb.data_len", "-e", "usb.capdata",
           "-e", "usb.urb_type", "-e", "usb.device_address"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        print("tshark 失败:\n%s" % (r.stderr or r.stdout))
        sys.exit(1)
    rows = []
    for line in r.stdout.splitlines():
        f = line.split("\t")
        if len(f) < 5:
            continue
        num, ep, ttype, dlen, cap = f[0], f[1], f[2], f[3], f[4]
        if not ep:
            continue
        epv = int(ep, 0) if ep.lower().startswith("0x") else int(ep)
        rows.append({"num": num, "ep": epv, "type": ttype, "len": dlen,
                     "cap": cap.replace(":", ""), "urb": f[5] if len(f) > 5 else "",
                     "addr": f[6] if len(f) > 6 else ""})
    return rows


def detect_address(pcap):
    """挑出逻辑分析仪的 USB 地址: 用端点 0x02/0x81 且**不是** HID(0x81 每 6 字节一包) 的那个。"""
    cmd = [TSHARK, "-r", pcap, "-T", "fields", "-e", "usb.device_address",
           "-e", "usb.endpoint_address"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    per = {}
    for line in r.stdout.splitlines():
        f = line.split("\t")
        if len(f) < 2 or not f[0]:
            continue
        per.setdefault(int(f[0]), set()).add(f[1])
    best = None
    for addr, eps in sorted(per.items()):
        if "0x02" in eps:                     # 有 OUT 0x02 = 我们的设备
            best = addr
            break
    return best, per


def group_blocks(rows, ep):
    """把同一端点的连续 512B 包拼回 2048B 块 (USBPcap 逐包记录)。"""
    blocks = []
    buf = b""
    for r in rows:
        if r["ep"] != ep:
            continue
        blob = to_bytes(r["cap"])
        if not blob:
            continue
        buf += blob
        while len(buf) >= 2048:
            blocks.append((r["num"], buf[:2048]))
            buf = buf[2048:]
    if buf:
        blocks.append((rows[-1]["num"] if rows else "?", buf))
    return blocks


def to_bytes(hexstr):
    h = re.sub(r"[^0-9a-fA-F]", "", hexstr or "")
    return bytes.fromhex(h) if len(h) % 2 == 0 and h else b""


def describe_command(blob):
    """把 OUT 方向的一块数据按已知格式解读。"""
    out = []
    if len(blob) < 8:
        out.append("    (短包 %d 字节) %s" % (len(blob), blob[:32].hex(" ")))
        return out
    # 裸命令: [0x0A][code][data...]
    if blob[0] == 0x0A:
        code = blob[1]
        names = {0x80: "EnterBootloader", 0x81: "GetMCUVersion", 0x84: "RestartMCU",
                 0x87: "SetResetState", 0x88: "GetResetState"}
        out.append("    裸命令 0x%02X %s data=%s" % (code, names.get(code, "?"),
                                                    blob[2:8].hex(" ")))
        return out
    # 帧命令: [8×00][0x0A][code][len-1][data...][0x0B][CRC32-LE]，发前交织
    try:
        plain = P.deinterleave(blob)
    except Exception:
        plain = blob
    i = plain.find(b"\x0a")
    if i < 0:
        out.append("    无法识别: %s" % blob[:32].hex(" "))
        return out
    code = plain[i + 1] if i + 1 < len(plain) else None
    names = {P.CMD_GET_DEVICE_DATA: "GetDeviceData", P.CMD_PARAM_SETTING: "ParameterSetting",
             P.CMD_SIMPLE_TRIGGER: "SimpleTrigger", P.CMD_STOP: "Stop",
             P.CMD_PWM: "PWM", P.CMD_EXIT: "Exit"}
    ln = plain[i + 2] if i + 2 < len(plain) else 0
    data = plain[i + 3:i + 2 + ln]
    out.append("    帧命令 code=0x%02X %s data(%d)=%s"
               % (code, names.get(code, "?"), len(data), data.hex(" ")))
    if code == P.CMD_PARAM_SETTING and len(data) >= 13:
        rates = P.RATES
        hz = rates[data[2] - 1] if 0 < data[2] <= len(rates) else "?"
        out.append("       flags=0x%02X(Buffer=%d,RLE=%d) 阈值=%.1fV hzIndex=%d(%sMHz) "
                   "深度=%d 触发深度=%d"
                   % (data[0], bool(data[0] & 0x80), bool(data[0] & 0x40),
                      (data[1] & 0x7f) / 10.0, data[2], hz,
                      int.from_bytes(data[3:8], "little"),
                      int.from_bytes(data[8:13], "little")))
    if code == P.CMD_SIMPLE_TRIGGER and len(data) >= 9:
        for k in range(len(data) - 1):
            b = data[k]
            if b:
                out.append("       通道对%d (CH%d,CH%d): 0x%02X → CH%d%s CH%d%s"
                           % (k, 2 * k, 2 * k + 1, b,
                              2 * k, _trig_text(b, True), 2 * k + 1, _trig_text(b, False)))
        out.append("       立即采集标志=%d" % data[-1])
    if code == P.CMD_PWM and data:
        if data[0] in (0x11, 0x21) and len(data) >= 9:
            m = int.from_bytes(data[1:5], "little")
            c = int.from_bytes(data[5:9], "little")
            out.append("       PWM%d 启动 maxHz=%d dutyCnt=%d → %.1fHz %.2f%%"
                       % (0 if data[0] == 0x11 else 1, m, c,
                          200000000.0 / m if m else 0, 100.0 * c / m if m else 0))
        else:
            out.append("       PWM 停止/其他: %s" % data.hex(" "))
    return out


def _trig_text(b, even):
    en = (b & 0x80) if even else (b & 0x08)
    if not en:
        return "未使能"
    r = (b & 0x10) if even else (b & 0x01)
    f = (b & 0x20) if even else (b & 0x02)
    h = (b & 0x40) if even else (b & 0x04)
    parts = [n for n, v in (("上升", r), ("下降", f), ("高电平", h)) if v]
    return "+".join(parts) if parts else "低电平"


def summarize_in(blob):
    """把 IN 方向的块按帧解析, 给出 order 统计与 order-3 内容。"""
    n2 = len(blob) - len(blob) % 2048
    if n2 < 2048:
        return "    (短响应 %d 字节) %s" % (len(blob), blob[:24].hex(" "))
    frames = P.parse_frames(P.deinterleave(blob[:n2]))
    if not frames:
        return "    (%d 字节, 未解析出帧)" % len(blob)
    from collections import Counter
    cnt = Counter(o for o, _ in frames)
    out = ["    %d 字节 → %d 帧, order 统计 %s" % (len(blob), len(frames), dict(cnt))]
    for o, p in frames:
        if o == 3 and len(p) >= 7:
            out.append("      order-3: %s (int=%d)"
                       % (p[:7].hex(" "), int.from_bytes(p[2:7], "little")))
        if o == 1 and len(p) >= 2:
            out.append("      order-1: ch=%d 保留字节=%d 样本=%d 字节" % (p[0], p[1], len(p) - 2))
    return "\n".join(out[:12])


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    pcap = sys.argv[1]
    addr = None
    if "--addr" in sys.argv:
        addr = int(sys.argv[sys.argv.index("--addr") + 1])
    if addr is None:
        addr, per = detect_address(pcap)
        print("自动识别的设备地址: %s    (该包里各地址的端点: %s)" % (addr, per))
    rows = tshark_rows(pcap, addr)
    out_rows = [r for r in rows if r["ep"] == P.EP_OUT]
    in_rows = [r for r in rows if r["ep"] == P.EP_IN]
    print("=" * 74)
    print("抓包: %s   地址 %s" % (pcap, addr))
    print("记录 %d 条: OUT(0x%02X) %d 条 / IN(0x%81X) %d 条" % (len(rows), P.EP_OUT,
                                                              len(out_rows), P.EP_IN,
                                                              len(in_rows)))
    print("=" * 74)

    print("\n--- 主机 → 设备 (EP 0x%02X): 厂商软件发了什么 ---" % P.EP_OUT)
    for r in out_rows:
        blob = to_bytes(r["cap"])
        if not blob:
            continue
        lines = describe_command(blob)
        if "--only-cfg" in sys.argv and not any(
                ("ParameterSetting" in l or "SimpleTrigger" in l or "flags=" in l
                 or "通道对" in l or "立即采集" in l) for l in lines):
            continue
        print("  #%s len=%d" % (r["num"], len(blob)))
        for line in lines:
            print(line)

    if "--out-only" in sys.argv:
        return 0

    print("\n--- 设备 → 主机 (EP 0x%02X): 设备回了什么 ---" % P.EP_IN)
    frames_all = []
    n_short = n_block = 0
    for r in in_rows:
        blob = to_bytes(r["cap"])
        if not blob:
            continue
        if len(blob) >= 2048:
            n_block += 1
            n2 = len(blob) - len(blob) % 2048        # 每条记录自身 2048 对齐
            frames_all += P.parse_frames(P.deinterleave(blob[:n2]))
        else:
            n_short += 1
            frames_all += P.parse_frames(blob)       # 短响应(如 MCU 版本)不解交织
    print("  IN 记录: %d 条 16KB 级块 + %d 条短响应" % (n_block, n_short))
    from collections import Counter
    print("  帧数 %d, order 统计 %s" % (len(frames_all), dict(Counter(o for o, _ in frames_all))))
    if not frames_all:
        return 0
    # order-3 (触发偏移) 全量列出 —— 这是"厂商怎么解释触发位置"的直接证据
    off3 = [(i, int.from_bytes(p[2:7], "little"), p.hex(" "))
            for i, (o, p) in enumerate(frames_all) if o == 3 and len(p) >= 7]
    print("  order-3 帧 %d 个:" % len(off3))
    for i, v, raw in off3[:4]:
        print("     第 %d 帧: p[2:7]=%d 完整载荷(%dB)=%s" % (i, v, len(raw) // 3, raw))
    # order-1 通道分布
    per_ch = Counter()
    for o, p in frames_all:
        if o == 1 and len(p) >= 2:
            per_ch[p[0]] += len(p) - 2
    print("  order-1 各通道样本字节: %s" % dict(sorted(per_ch.items())[:8]))
    for i, (o, p) in enumerate(frames_all[:6]):
        print("    [%d] order=%d len=%d raw=%s" % (i, o, len(p), p[:24].hex(" ")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
