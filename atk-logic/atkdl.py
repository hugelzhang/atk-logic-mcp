# -*- coding: utf-8 -*-
"""ATK-Logic 厂商波形存档 `.atkdl` 的读写（格式由官方样例 + 厂商抓包实测逆向）。

文件 = ZIP，含：

- `channel.ini`   会话级：`SessionName` / `SamplingFrequency`(**kHz**) / `SamplingDepth`(采样点) /
                  `TriggerSamplingDepth` / `Decodes`(=解码结果条目名)
- `set.ini`       会话配置：`[会话名]` 段 + `channelsSet`(通道使能/触发类型) +
                  `settingData`(**`setHz` 才是权威采样率，单位 Hz**) / `pwmData` / 颜色 等
- `vernier.ini`   `Trigger,<通道哈希>,<触发点采样号>,`
- `<毫秒时间戳>`  解码器配置 JSON：`{协议名: {annotation_rows/channels/opt_channels/options/…}, "main": {…}}`
- `N/channel.ini` 通道级：第 1 行 `channel N`，第 2 行起始采样号，第 3 行**本通道有效采样数**，
                  第 4 行 `a,b` 标记对，其后大量 `0,0`
- `N/<off>-<idx>.bin` 原始采样分片，每片 1 MiB，**每字节 8 个采样、LSB 在前**；
                  分片按 `<idx>` 排序拼接，序号可能不连续（官方写盘会跳号）

要点（实测，别想当然）：
1. **采样率真值在 `set.ini` 的 `settingData.setHz`**（Hz）；`channel.ini` 的 `SamplingFrequency`
   是其 kHz 值（差 1000 倍，容易踩）。
2. `SamplingDepth == setHz × setTime(ms/1000)`（例：10MHz × 100ms = 1,000,000）自洽可校验。
3. 数据**可能长于有效采样数**（设备录满后继续吐流，上位机把收到的都存了）→ 读时按
   `N/channel.ini` 第 3 行截断（与在线采集的"按 depth 截断"同一回事）。
4. 官方文件行尾是 `\\r\\r\\n`（Windows 文本模式写 `\\r\\n` 的结果）——写文件时照抄，别"修好"。
"""
import json
import os
import re
import time
import zipfile

import atkproto as P

PART_BYTES = 1024 * 1024                 # 官方分片固定 1 MiB
EOL = "\r\r\n"                           # 官方行尾（照抄，别改成 \r\n）
TRIGGER_KEY = "ff26a3d9"                 # vernier.ini 里出现的通道哈希


# ---------------------------------------------------------------- 读

def _parse_kv(text):
    """把 `a=b` 行解析成 dict（容忍 \\r\\r\\n）。"""
    out = {}
    for line in re.split(r"[\r\n]+", text):
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _parse_ini_sections(text):
    """极简 INI：`[段]` + `k=v`（也返回无段落的键到 `""` 段）。"""
    sec, out = "", {}
    for line in re.split(r"[\r\n]+", text):
        line = line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            sec = line[1:-1]
            out.setdefault(sec, {})
        elif "=" in line:
            k, v = line.split("=", 1)
            out.setdefault(sec, {})[k.strip()] = v.strip()
    return out


class AtkDlSession(object):
    """读进来的 `.atkdl` 会话。"""

    def __init__(self, path):
        self.path = path
        self.name = ""
        self.sample_rate_hz = 0
        self.depth = 0
        self.trigger_depth = 0
        self.rate_khz = 0
        self.channels = {}               # ch -> {"bytes": b"", "valid": N, "ini": [...]}
        self.settings = {}               # set.ini 解析结果
        self.decode = None               # 解码器配置 JSON
        self.vernier = None              # (hash, sample_pos)

    # -- 采样 --------------------------------------------------------
    def raw_bytes(self, ch):
        return self.channels[ch]["bytes"]

    def valid_count(self, ch):
        return self.channels[ch]["valid"]

    def samples(self, ch, limit=None):
        """展开成 0/1 采样序列（bytearray），默认按有效采样数截断。"""
        out = P.bits_from_bytes(self.channels[ch]["bytes"])
        n = self.channels[ch]["valid"] or len(out)
        if limit and limit < n:
            n = limit
        return out[:n]

    def window(self, ch, start=0, count=None):
        """只展开 [start, start+count) 这段采样 —— 官方大样例(50MiB+)别整块展开。"""
        blob = self.channels[ch]["bytes"]
        total = self.channels[ch]["valid"] or (len(blob) * 8)
        count = total if count is None else min(count, total - start)
        if count <= 0:
            return bytearray()
        a, b = start >> 3, (start + count + 7) >> 3
        seg = blob[a:min(b, len(blob))]
        bits = P.bits_from_bytes(seg)
        off = start - (a << 3)
        return bits[off:off + count]

    def first_activity(self, ch, limit_bytes=None):
        """首个"不均匀字节"的位置（采样号）= 信号真正开始的地方；整段恒定返回 None。

        官方存档会把触发前的长空闲也存下来，直接从 0 开始解码往往什么都解不到。
        """
        blob = self.channels[ch]["bytes"]
        if limit_bytes:
            blob = blob[:limit_bytes]
        if not blob:
            return None
        ref = blob[0]
        if ref not in (0x00, 0xFF):
            return 0
        tbl = bytes(0 if i == ref else 1 for i in range(256))
        i = blob.translate(tbl).find(1)
        return None if i < 0 else i * 8

    def active_channels(self):
        return sorted(c for c, v in self.channels.items() if v["bytes"])

    def stats(self, ch, limit=None):
        return P.waveform_stats(self.samples(ch, limit), self.sample_rate_hz or 1)

    def summary(self):
        return {
            "path": self.path,
            "session": self.name,
            "sample_rate_hz": self.sample_rate_hz,
            "sample_rate_raw_khz": self.rate_khz,
            "depth": self.depth,
            "trigger_depth": self.trigger_depth,
            "active_channels": self.active_channels(),
            "channel_valid_samples": {c: v["valid"] for c, v in self.channels.items() if v["bytes"]},
            "decoders": list(self.decode.keys()) if isinstance(self.decode, dict) else [],
            "vernier": self.vernier,
        }


def read_atkdl(path):
    """读 `.atkdl`（官方样例或厂商软件存的波形）。"""
    s = AtkDlSession(path)
    z = zipfile.ZipFile(path)
    parts, chan_ini, decode, raw = {}, {}, None, {}
    for info in z.infolist():
        name = info.filename
        if name.endswith("/"):
            continue
        m = re.match(r"^(\d+)/(\d+)-(\d+)\.bin$", name)
        if m:
            ch, off, idx = (int(x) for x in m.groups())
            parts.setdefault(ch, []).append((idx, off, z.read(name)))
            continue
        m = re.match(r"^(\d+)/channel\.ini$", name)
        if m:
            chan_ini[int(m.group(1))] = z.read(name).decode("utf-8", "replace")
            continue
        raw[name] = z.read(name)
    # 顶层 ini
    top = _parse_kv(raw.get("channel.ini", b"").decode("utf-8", "replace"))
    s.name = top.get("SessionName", "")
    s.rate_khz = int(top.get("SamplingFrequency") or 0)
    s.depth = int(top.get("SamplingDepth") or 0)
    s.trigger_depth = int(top.get("TriggerSamplingDepth") or 0)
    s.settings = _parse_ini_sections(raw.get("set.ini", b"").decode("utf-8", "replace"))
    sd = s.settings.get(s.name, {})
    if "settingData" in sd:
        try:
            s.settings["settingData"] = json.loads(sd["settingData"])
        except Exception:
            pass
    set_hz = (s.settings.get("settingData") or {}).get("setHz") if isinstance(s.settings.get("settingData"), dict) else None
    s.sample_rate_hz = int(set_hz) if set_hz else s.rate_khz * 1000      # setHz 权威, 退回 kHz*1000
    if "vernier.ini" in raw:
        v = raw["vernier.ini"].decode("utf-8", "replace").strip()
        f = [x for x in v.split(",") if x != ""]
        if len(f) >= 3:
            s.vernier = (f[1], int(f[2]))
    # 解码结果 JSON（条目名 = 毫秒时间戳, 也在 channel.ini 的 Decodes 里）
    want = top.get("Decodes", "")
    for name, blob in raw.items():
        if name in ("channel.ini", "set.ini", "vernier.ini"):
            continue
        if want and name != want:
            continue
        try:
            decode = json.loads(blob.decode("utf-8", "replace"))
        except Exception:
            continue
    s.decode = decode
    # 通道数据
    for ch, lst in parts.items():
        lst.sort(key=lambda t: (t[0], t[1]))
        s.channels[ch] = {"bytes": b"".join(b for _, _, b in lst), "valid": 0,
                          "ini": chan_ini.get(ch, "").split("\n")}
    for ch, ini in chan_ini.items():
        s.channels.setdefault(ch, {"bytes": b"", "valid": 0, "ini": []})
        lines = [l.strip() for l in ini.replace("\r\n", "\n").split("\n") if l.strip()]
        s.channels[ch]["ini"] = lines
        if len(lines) >= 3 and lines[2].isdigit():
            s.channels[ch]["valid"] = int(lines[2])          # 第 3 行 = 有效采样数
    return s


# ---------------------------------------------------------------- 写

def pack_samples(samples):
    """0/1 采样序列 → 打包字节（每字节 8 采样, LSB 在前）。长度补到 8 的整数倍。"""
    out = bytearray((len(samples) + 7) // 8)
    for i, v in enumerate(samples):
        if v:
            out[i >> 3] |= 1 << (i & 7)
    return bytes(out)


def _channel_ini(ch, valid, start=0):
    lines = ["channel %d" % ch, str(start), str(valid), "0,0"]
    lines += ["0,0"] * (319)                                    # 官方固定 323 行
    return EOL.join(lines) + EOL


def write_atkdl(path, channels, sample_rate_hz, depth=None, name="DL16 Plus",
                settings=None, decode=None, progress=None):
    """写 `.atkdl`（结构与官方样例对齐）。

    channels: {通道号: 原始打包字节(bytes) 或 0/1 采样序列(可迭代)}
    sample_rate_hz: 采样率(Hz)。depth 省略时取各通道最大有效采样数。
    settings: 可选 dict，覆盖 set.ini 的 settingData 字段（isBuffer/selectHzIndex/…）。
    decode: 可选解码器配置 JSON（原样写入并登记到 channel.ini 的 Decodes）。
    返回写出的统计 dict。
    """
    import io
    packed = {}
    for ch, data in channels.items():
        if isinstance(data, (bytes, bytearray, memoryview)):
            packed[ch] = bytes(data)
        else:
            packed[ch] = pack_samples(list(data))
    if not packed:
        raise ValueError("channels 为空")
    valid = {ch: len(b) * 8 for ch, b in packed.items()}
    depth = int(depth or max(valid.values()))
    rate_khz = int(round(sample_rate_hz / 1000.0))
    stamp = str(int(time.time() * 1000))
    sd = {"RLE": False, "intervalTime": 0, "isBuffer": True, "model": 0,
          "selectHzIndex": 0, "selectThresholdLevelIndex": 2, "selectTimeIndex": 0,
          "setHz": int(sample_rate_hz), "setTime": int(depth * 1000 / sample_rate_hz) if sample_rate_hz else 0,
          "thresholdLevel": 2, "triggerPosition": 50}
    if settings:
        sd.update(settings)
    chans_set = [{"enable": ch in packed, "id": ch, "triggerType": 0} for ch in range(16)]
    set_ini = EOL.join([
        "", "[%s]" % name, "isFirst=false", "isInstantly=false", "isOne=true",
        "isSimpleTrigger=true",
        "channelsSet=" + json.dumps(chans_set, separators=(",", ":")),
        "settingData=" + json.dumps(sd, separators=(",", ":")),
        'pwmData=[{"duty":50,"hz":20,"unit":2},{"duty":50,"hz":20,"unit":2}]',
        'favoritesList=["UART"]',
        "channelsDataColor=" + json.dumps(["#9252e8"] * 16, separators=(",", ":")),
        "channelHeightMultiple=1", "isMouseMeasure=true", ""])
    top_ini = EOL.join(["SessionName=%s" % name,
                        "SamplingFrequency=%d" % rate_khz,
                        "SamplingDepth=%d" % depth,
                        "TriggerSamplingDepth=%d" % (depth // 2),
                        "Decodes=%s" % (stamp if decode else ""), ""])
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("channel.ini", top_ini)
        z.writestr("set.ini", set_ini)
        z.writestr("vernier.ini", "Trigger,%s,%d,%s" % (TRIGGER_KEY, depth // 2, EOL))
        if decode is not None:
            z.writestr(stamp, json.dumps(decode, ensure_ascii=False))
        for ch, blob in sorted(packed.items()):
            z.writestr("%d/" % ch, b"")
            z.writestr("%d/channel.ini" % ch, _channel_ini(ch, valid[ch]))
            for i, off in enumerate(range(0, max(len(blob), 1), PART_BYTES)):
                z.writestr("%d/%d-%d.bin" % (ch, off, i), blob[off:off + PART_BYTES])
            if progress:
                progress(ch)
        # 官方样例是 16 个通道**每个都有 channel.ini**（没有数据的 valid=0）；
        # 少了它厂商软件会弹 "读取通道N配置文件失败, 已经跳过" —— 必须补齐。
        for ch in range(16):
            if ch in packed:
                continue
            z.writestr("%d/" % ch, b"")
            z.writestr("%d/channel.ini" % ch, _channel_ini(ch, 0))
    return {"path": path, "sample_rate_hz": int(sample_rate_hz), "depth": depth,
            "channels": {ch: valid[ch] for ch in valid},
            "bytes": {ch: len(b) for ch, b in packed.items()},
            "decoders": list(decode.keys()) if isinstance(decode, dict) else []}


def stream_to_atkdl(stream, channels, sample_rate_mhz, depth, path,
                    threshold_v=1.5, trigger="rising", mode="buffer", decode=None):
    """把在线采集的 FrameStream 直接存成 `.atkdl`（给 save_capture 用）。"""
    chans, valid = {}, {}
    for ch in channels:
        samples, _ = P.samples_of(stream, ch, depth, sample_rate_mhz * 1_000_000)
        valid[ch] = len(samples)
        chans[ch] = pack_samples(samples)
    res = write_atkdl(path, chans, sample_rate_mhz * 1_000_000, depth=depth, decode=decode)
    res["channel_valid_samples"] = valid
    res["source"] = {"sample_rate_mhz": sample_rate_mhz, "depth": depth,
                     "threshold_v": threshold_v, "trigger": trigger, "mode": mode}
    return res
