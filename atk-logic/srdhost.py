# -*- coding: utf-8 -*-
"""纯 Python 的 sigrok `libsigrokdecode` 宿主 —— 直接跑 ATK-Logic 自带的 208 个协议解码器。

背景：厂商上位机（ATK-Logic）的协议解码器就是 sigrok 的 Python PD（官方文档明说"无需修改"），
框架在 C 侧（`atk_libsigrokdecode`）。这里用纯 Python 复刻宿主框架，就能在我们自己的 MCP 里
用上 I²C / SPI / CAN / Modbus / 1-Wire / WS2812 / DHT11 … 200+ 个解码器。

实现的 API 面（按本地 208 个 PD 的实际用法统计出来的，不是猜的）：
  srd.Decoder / OUTPUT_ANN / OUTPUT_PYTHON / OUTPUT_BINARY / OUTPUT_LOGIC / OUTPUT_META / SRD_CONF_SAMPLERATE
  self.wait(cond)  cond ∈ None | {pin: spec} | [cond, cond…]（**列表 = OR**，命中情况放 self.matched[i]）
                   spec ∈ 'l'/'L' 低, 'h'/'H' 高, 'r' 上升, 'f' 下降, 'e' 任意边沿, {'skip': N} 走过 N 个采样
  self.put(ss, es, out_index, data) / self.register(out_type) → out_index / self.has_channel(idx)
  self.samplenum / self.matched / self.channels / self.options / self.samplerate
栈式解码：宿主把下层 PD 的 OUTPUT_PYTHON 包逐个喂给上层 PD 的 `decode(ss, es, data)`。
"""
import bisect
import importlib
import os
import sys
import types

DEFAULT_DECODERS_DIR = r"C:\Program Files\ATK-Logic\decoders"

# 输出类型（与 libsigrokdecode 一致）
OUTPUT_ANN, OUTPUT_PYTHON, OUTPUT_BINARY, OUTPUT_LOGIC, OUTPUT_META = range(5)
_OUT_NAMES = {OUTPUT_ANN: "ann", OUTPUT_PYTHON: "python", OUTPUT_BINARY: "binary",
              OUTPUT_LOGIC: "logic", OUTPUT_META: "meta"}


class PdError(RuntimeError):
    """PD 抛出的错误（缺通道/缺选项/数据不够等）统一包一层，附带 PD 名。"""


# ============================== sigrokdecode 模块 shim ==============================

def install_shim(decoders_dir=DEFAULT_DECODERS_DIR):
    """把假 `sigrokdecode` 模块塞进 sys.modules，并把解码器目录加进 sys.path。"""
    if decoders_dir not in sys.path:
        sys.path.insert(0, decoders_dir)
    if "sigrokdecode" in sys.modules:
        return sys.modules["sigrokdecode"]
    m = types.ModuleType("sigrokdecode")
    m.Decoder = PdDecoderBase
    m.OUTPUT_ANN = m.SRD_OUTPUT_ANN = OUTPUT_ANN
    m.OUTPUT_PYTHON = m.SRD_OUTPUT_PYTHON = OUTPUT_PYTHON
    m.OUTPUT_BINARY = m.SRD_OUTPUT_BINARY = OUTPUT_BINARY
    m.OUTPUT_LOGIC = m.SRD_OUTPUT_LOGIC = OUTPUT_LOGIC
    m.OUTPUT_META = m.SRD_OUTPUT_META = OUTPUT_META
    m.SRD_CONF_SAMPLERATE = "samplerate"          # 采样率是"特殊选项"，id 就是 samplerate
    m.SRD_CONF_CAPTUREFILE = "capturefile"
    m.RUNNING, m.PAUSED, m.STOPPED = 0, 1, 2
    m.SRD_ERR = 0
    for _n in ("SRD_ERR_ARG", "SRD_ERR_MALLOC", "SRD_ERR_PYTHON", "SRD_ERR_DECODERS_DIR",
               "SRD_ERR_DECODERS", "SRD_ERR_CHANNEL", "SRD_ERR_ANNCLASS", "SRD_ERR_TERM_REQ"):
        setattr(m, _n, 1)

    class _Log(object):
        def __getattr__(self, _):
            return lambda *a, **k: None

    m.log = _Log()
    sys.modules["sigrokdecode"] = m
    return m


# ============================== wait() 条件引擎 ==============================

class LogicSource(object):
    """一个 PD 实例看到的逻辑通道：pin 序号 → 采样序列（0/1）。

    预计算每个 pin 的"跳变表"和"电平段表"，`wait()` 就能用二分跳着找，
    不用逐采样点扫（官方大样例动辄几百万点）。
    """

    def __init__(self, pins, n):
        self.n = n
        self.levels = pins                      # {pin: bytes/list, 长度 n}
        self.edges = {}                         # pin -> [(idx, 0|1)] idx 处电平变成该值
        self.runs = {}                          # pin -> [(start, level)]
        for p, seq in pins.items():
            e, r = [], []
            prev = None
            for i, v in enumerate(seq):
                v = 1 if v else 0
                if v != prev:
                    e.append((i, v))
                    r.append((i, v))
                    prev = v
            self.edges[p] = e
            self.runs[p] = r

    def level_at(self, pin, i):
        seq = self.levels.get(pin)
        if seq is None or i >= self.n:
            return None
        return 1 if seq[i] else 0

    def next_edges(self, pin, spec, start):
        """pin 上第 i>=start 个满足 spec 的边沿位置（生成器）。"""
        e = self.edges.get(pin) or []
        i = bisect.bisect_left(e, (start, -1))
        while i < len(e):
            yield e[i][0]
            i += 1

    def next_level_pos(self, pin, want, start):
        """第 i>=start 个电平为 want 的位置（生成器，按段产出）。"""
        r = self.runs.get(pin) or []
        i = bisect.bisect_left(r, (start, -1)) - 1
        if i >= 0 and r[i][1] == want and r[i][0] <= start:
            yield start
        while i + 1 < len(r):
            i += 1
            if r[i][1] == want:
                yield r[i][0]


def _spec_ok_at(src, pin, spec, idx):
    """在采样点 idx 上，pin 是否满足 spec（用于 AND 组合里的复核）。"""
    if spec == "skip":
        return True
    lv = src.level_at(pin, idx)
    if lv is None:
        return False
    if spec in ("l", "L", "0", 0):
        return lv == 0
    if spec in ("h", "H", "1", 1):
        return lv == 1
    prev = src.level_at(pin, idx - 1)
    if spec == "r":
        return prev == 0 and lv == 1
    if spec == "f":
        return prev == 1 and lv == 0
    if spec == "e":
        return prev is not None and prev != lv
    return False


def _all_ok(src, cond, idx):
    for k, v in cond.items():
        if k == "skip":
            continue
        if not _spec_ok_at(src, int(k), v, idx):
            return False
    return True


def _candidates(src, cond, cur):
    """产出 cond 可能命中的位置：**严格大于 cur**、递增、有限。

    AND 条件的真假只可能在"涉及引脚的段边界"上变化，所以候选位置 = 这些边界的归并
    （外加 cur+1，因为电平类条件可能落在段的中间）。这是 I²C 那种
    `{SCL:'h', SDA:'f'}`（SCL 一整段高电平期间 SDA 才下降）能否命中的关键。
    `{'skip': N}` 按**当前位置**起算（= cur + N），与 libsigrokdecode 一致。
    """
    import heapq
    if cond is None:
        return
    if isinstance(cond, dict) and not cond:
        yield cur
        return
    origin = cur + 1
    skip = None
    pins = []
    for k, v in (cond.items() if isinstance(cond, dict) else []):
        if k == "skip":
            skip = int(v)
        else:
            pins.append((int(k), v))
    skip_at = max(cur + skip, origin) if skip is not None else None
    if not pins:
        yield skip_at if skip_at is not None else origin
        return
    if (skip_at is None or origin >= skip_at) and _all_ok(src, cond, origin):
        yield origin
        return
    cursors, heap = [], []
    for p, _spec in pins:
        runs = src.runs.get(p) or []
        i = bisect.bisect_left(runs, (origin, -1))
        cursors.append((runs, i))
        if i < len(runs):
            heap.append((runs[i][0], len(cursors) - 1))
    heapq.heapify(heap)
    while heap:
        pos, ci = heapq.heappop(heap)
        runs, i = cursors[ci]
        i += 1
        cursors[ci] = (runs, i)
        if i < len(runs):
            heapq.heappush(heap, (runs[i][0], ci))
        if pos < origin:
            continue
        if skip_at is not None and pos < skip_at:
            continue
        if _all_ok(src, cond, pos):
            yield pos


# ============================== Decoder 基类（PD 子类化它） ==============================

class PdDecoderBase(object):
    """PD 的基类：只提供宿主 API，具体逻辑在各 PD 自己的 start()/decode() 里。"""

    api_version = 3

    # --- 宿主 API（host 在实例上注入 self._host） ---
    def wait(self, cond=None):
        return self._host._host_wait(cond)

    def put(self, ss, es, out, data):
        return self._host._host_put(ss, es, out, data)

    def register(self, out_type, proto_id="", meta=None):
        return self._host._host_register(out_type, proto_id, meta)

    def has_channel(self, ch):
        return self._host._host_has_channel(ch)

    def get_sample_rate(self):
        return self._samplerate


# ============================== 一次解码运行 ==============================

class PdResult(object):
    def __init__(self):
        self.annotations = []      # [(ss, es, ann_idx, texts)]
        self.binary = []           # [(ss, es, bin_idx, bytes)]
        self.python = []           # [(ss, es, data)]
        self.meta = {}
        self.outputs = []          # [(idx, type, proto_id)]
        self.rows = {}             # ann_idx -> (row_id, row_desc)
        self.binary_meta = {}      # bin_idx -> (id, name)


class PdRun(object):
    """跑一个 PD 实例。"""

    def __init__(self, name, channels, options, samplerate, samples_by_hw, decoders_dir=DEFAULT_DECODERS_DIR,
                 max_samples=None, cls=None):
        """channels: {通道id: 硬件通道号}; samples_by_hw: {硬件通道号: 0/1 序列}
        cls: 直接用给定的 PD 类（测试用），否则按名字从解码器目录加载。"""
        self.name = name
        self.decoders_dir = decoders_dir
        install_shim(decoders_dir)
        if cls is None:
            cls = load_decoder_class(name, decoders_dir)
        self.cls = cls
        self.result = PdResult()
        self.samplerate = samplerate
        self.dec = None
        self.channels = channels            # {通道id: 硬件通道号}
        # 依 PD 声明顺序给通道分配**内部序号**（PD 里 wait({0:'e'}) / has_channel(0) 用的就是这个）
        order = []
        for meta_key in ("channels", "optional_channels", "opt_channels"):
            for c in (getattr(cls, meta_key, ()) or ()):
                if c["id"] not in order:
                    order.append(c["id"])
        self.internal_idx = {cid: i for i, cid in enumerate(order)}
        self.idx_of = {}                    # 已绑定的: 通道id -> 内部序号
        self.hw_of = {}                     # 已绑定的: 通道id -> 硬件通道号
        for cid in order:
            if cid in channels:
                self.idx_of[cid] = self.internal_idx[cid]
                self.hw_of[cid] = channels[cid]
        # 条件引擎按**内部序号**索引（PD 的 wait({pin: spec}) 里 pin 就是内部序号）
        src_pins = {}
        for cid, internal in self.idx_of.items():
            hw = self.hw_of[cid]
            if hw in samples_by_hw:
                seq = samples_by_hw[hw]
                src_pins[internal] = seq[:max_samples] if max_samples else seq
        n = max((len(v) for v in src_pins.values()), default=0)
        self.src = LogicSource(src_pins, n)
        self.wait_calls = 0
        self.options = dict(self._default_options(cls))
        self.options.update(self._coerce(options or {}, cls))
        self.options.setdefault("samplerate", samplerate)
        # 叶子解码器（吃 logic 的）一个通道都没绑 → 直接给可读错误，别静悄悄返回空结果
        declares = bool(getattr(cls, "channels", ()) or getattr(cls, "optional_channels", ()))
        if declares and not self.idx_of and "logic" in (getattr(cls, "inputs", ()) or ()):
            want = [c["id"] for c in (getattr(cls, "channels", ()) or ())]
            want += [c["id"] for c in (getattr(cls, "optional_channels", ()) or ())]
            raise PdError("%s 没有绑定任何通道；它需要 %s（如 channels={'%s': 0}）"
                          % (name, "/".join(want) or "?", want[0] if want else "?"))

    # ---- 元数据/选项 ----
    @staticmethod
    def _default_options(cls):
        out = {}
        for o in (getattr(cls, "options", ()) or ()):
            out[o["id"]] = o.get("default")
        return out

    @staticmethod
    def _coerce(opts, cls):
        """按 PD 声明的默认值类型把字符串转成 int/float/str。"""
        types_by_id = {}
        for o in (getattr(cls, "options", ()) or ()):
            d = o.get("default")
            types_by_id[o["id"]] = type(d)
        out = {}
        for k, v in opts.items():
            if isinstance(v, str):
                t = types_by_id.get(k)
                try:
                    if t is int:
                        v = int(v)
                    elif t is float:
                        v = float(v)
                    elif t is bool:
                        v = v.lower() in ("1", "true", "yes")
                except (TypeError, ValueError):
                    pass
            out[k] = v
        return out

    def metadata(self):
        cls = self.cls
        anns = list(getattr(cls, "annotations", ()) or ())
        rows = {}
        for row in (getattr(cls, "annotation_rows", ()) or ()):
            rid, rdesc, classes = row[0], row[1], (row[2] if len(row) > 2 else ())
            for c in classes:
                rows[int(c)] = (rid, rdesc)
        for i, a in enumerate(anns):
            if i not in rows:
                rows[i] = ("", a[1] if len(a) > 1 else str(a))
        return {
            "id": getattr(cls, "id", None), "name": getattr(cls, "name", None),
            "longname": getattr(cls, "longname", None), "desc": getattr(cls, "desc", None),
            "inputs": list(getattr(cls, "inputs", ()) or ()),
            "outputs": list(getattr(cls, "outputs", ()) or ()),
            "channels": [dict(c) for c in (getattr(cls, "channels", ()) or ())],
            "optional_channels": [dict(c) for c in (getattr(cls, "optional_channels", ()) or ())],
            "options": [dict(o) for o in (getattr(cls, "options", ()) or ())],
            "annotations": [list(a) for a in anns],
            "annotation_rows": [list(r) for r in (getattr(cls, "annotation_rows", ()) or ())],
            "binary": [list(b) for b in (getattr(cls, "binary", ()) or ())],
        }

    # ---- 宿主回调 ----
    @staticmethod
    def _class_key(val, decls):
        """注解/二进制类可以是**下标**，也可以是**名字**（厂商 fork 的 PD 会用 'DATA' 这种字符串）。"""
        if isinstance(val, int):
            return val
        for i, d in enumerate(decls or ()):
            if isinstance(d, (list, tuple)) and d:
                if d[0] == val or (len(d) > 1 and d[1] == val):
                    return i
        return val                      # 认不出来就原样当键（不丢数据）

    def _host_put(self, ss, es, out, data):
        t = self.out_types.get(out, None)
        r = self.result
        if t == OUTPUT_ANN:
            if isinstance(data, (list, tuple)) and len(data) >= 2:
                idx, texts = data[0], data[1]
                idx = self._class_key(idx, getattr(self.cls, "annotations", ()))
                if not isinstance(texts, (list, tuple)):
                    texts = [str(texts)]
                r.annotations.append((int(ss), int(es), idx,
                                      [str(x) for x in texts]))
            return
        if t == OUTPUT_BINARY:
            if isinstance(data, (list, tuple)) and len(data) >= 2:
                idx = self._class_key(data[0], getattr(self.cls, "binary", ()))
                blob = data[1] if data[1] is not None else b""
                r.binary.append((int(ss), int(es), idx, bytes(blob)))
            return
        if t == OUTPUT_PYTHON:
            r.python.append((int(ss), int(es), data))
            return
        if t == OUTPUT_META:
            if isinstance(data, (list, tuple)) and len(data) >= 2:
                r.meta[str(data[0])] = data[1]
            return

    def _host_register(self, out_type, proto_id="", meta=None):
        idx = len(self.result.outputs)
        self.result.outputs.append((idx, out_type, proto_id))
        self.out_types[idx] = out_type
        if out_type == OUTPUT_BINARY:
            self.result.binary_meta[idx] = (proto_id, meta)
        return idx

    def _host_has_channel(self, ch):
        return ch in self.idx_of.values()

    def _host_wait(self, cond):
        src = self.src
        dec = self.dec
        start = dec.samplenum
        self.wait_calls += 1
        if self.wait_calls > 200_000_000:
            raise PdError("%s wait() 调用超过 2 亿次，疑似死循环" % self.name)
        # None → 前进一个采样点；{} → 立即返回**当前**采样点的引脚值（SPI 用它抓初值）
        if cond is None:
            if start + 1 >= src.n:
                self._exhausted = True
                raise StopIteration
            dec.samplenum = start + 1
            dec.matched = ()
            return self._pins_at(dec.samplenum)
        if isinstance(cond, dict) and not cond:
            dec.matched = ()
            return self._pins_at(start)
        origin = start + 1               # 其余条件一律从下一个采样点起找，保证前进（否则会原地死循环）
        if origin >= src.n:
            self._exhausted = True
            raise StopIteration
        conds = cond if isinstance(cond, list) else [cond]
        best, best_i = None, None
        for i, c in enumerate(conds):
            for idx in _candidates(src, c, start):
                idx = max(idx, origin)
                if best is None or idx < best:
                    best, best_i = idx, i
                break
        if best is None or best >= src.n:
            # 匹配点越过数据末尾 = 流结束（真机宿主同样结束，让 PD 的 decode() 自然返回）
            self._exhausted = True
            raise StopIteration
        dec.samplenum = best
        dec.matched = tuple(i == best_i for i in range(len(conds)))
        return self._pins_at(best)

    def _pins_at(self, idx):
        """按 PD 声明的通道顺序返回引脚值；没绑定的通道给 None（PD 会自己 has_channel 判断）。"""
        pins = []
        for cid in sorted(self.internal_idx, key=lambda c: self.internal_idx[c]):
            if cid in self.hw_of:
                v = self.src.level_at(self.internal_idx[cid], idx)
                pins.append(v if v is not None else None)
            else:
                pins.append(None)
        return tuple(pins)

    # ---- 跑 ----
    def run(self, max_iterations=20_000_000, lower_python=None):
        """lower_python: 栈式解码时下层 PD 的 python 包 [(ss, es, data)]。"""
        dec = self.cls()
        dec._host = self
        dec._samplerate = self.samplerate
        dec.channels = dict(self.idx_of)
        dec.options = self.options
        dec.samplenum = 0
        dec.matched = ()
        dec.samplerate = self.samplerate
        self.dec = dec
        self.out_types = {}
        self._exhausted = False
        # 老 API：宿主通过 metadata(key, value) 把采样率等配置告诉 PD（别覆盖成 dict！）
        md = getattr(dec, "metadata", None)
        if callable(md):
            try:
                md("samplerate", self.samplerate)
            except Exception:                                    # noqa: BLE001
                pass
        try:
            dec.start()
        except Exception as e:                                   # noqa: BLE001
            raise PdError("%s.start() 失败: %s: %s" % (self.name, type(e).__name__, e))
        if lower_python is not None:
            # 栈式 PD：宿主逐包调用 decode(ss, es, data)
            for ss, es, data in lower_python:
                try:
                    dec.decode(ss, es, data)
                except StopIteration:
                    break
                except Exception as e:                           # noqa: BLE001
                    raise PdError("%s.decode() 失败: %s: %s" % (self.name, type(e).__name__, e))
        else:
            n = 0
            while not self._exhausted:
                n += 1
                if n > max_iterations:
                    raise PdError("%s 迭代次数超限（死循环？）" % self.name)
                try:
                    dec.decode()
                except StopIteration:
                    break
                except Exception as e:                           # noqa: BLE001
                    raise PdError("%s.decode() 失败: %s: %s" % (self.name, type(e).__name__, e))
        # 汇总可读注释
        res = self.result
        rows = {}
        for row in (getattr(self.cls, "annotation_rows", ()) or ()):
            rid, rdesc, classes = row[0], row[1], (row[2] if len(row) > 2 else ())
            for c in classes:
                rows[int(c)] = (rid, rdesc)
        for i, a in enumerate(getattr(self.cls, "annotations", ()) or ()):
            rows.setdefault(i, ("", a[1] if len(a) > 1 else str(a)))
        res.rows = rows
        return res


def load_decoder_class(name, decoders_dir=DEFAULT_DECODERS_DIR):
    install_shim(decoders_dir)
    if name not in sys.modules.get("__srd_cache__", {}):
        cache = sys.modules.setdefault("__srd_cache__", {})
        if name not in cache:
            try:
                cache[name] = importlib.import_module(name + ".pd")
            except ImportError as e:
                raise PdError("加载解码器 %r 失败: %s（目录 %s）" % (name, e, decoders_dir))
        sys.modules["__srd_cache__"] = cache
    cls = getattr(sys.modules["__srd_cache__"][name], "Decoder", None)
    if cls is None:
        raise PdError("解码器 %r 里没有 Decoder 类" % name)
    return cls


def list_decoders(decoders_dir=DEFAULT_DECODERS_DIR):
    out = []
    for d in sorted(os.listdir(decoders_dir)):
        if os.path.isfile(os.path.join(decoders_dir, d, "pd.py")):
            out.append(d)
    return out


_INDEX_CACHE = {}


def pd_index(decoders_dir=DEFAULT_DECODERS_DIR):
    """{PD 的 name/id → 目录名}。厂商存档 JSON 里的键就是 PD 的 name（如 '24xx EEPROM'）。"""
    if _INDEX_CACHE.get(decoders_dir):
        return _INDEX_CACHE[decoders_dir]
    import re
    idx = {}
    for d in list_decoders(decoders_dir):
        try:
            src = open(os.path.join(decoders_dir, d, "pd.py"), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for key in ("id", "name", "longname"):
            m = re.search(r"^\s{4}%s = ['\"]([^'\"]+)['\"]" % key, src, re.M)
            if m:
                idx.setdefault(m.group(1), d)
    _INDEX_CACHE[decoders_dir] = idx
    return idx


def resolve(name, decoders_dir=DEFAULT_DECODERS_DIR):
    """把用户/存档里的协议名（PD 的 name / id / 目录名）解析成目录名。"""
    idx = pd_index(decoders_dir)
    if name in idx:
        return idx[name]
    low = name.strip().lower()
    for k, v in idx.items():
        if k.lower() == low:
            return v
    for k, v in idx.items():
        if low in k.lower() or low == v.lower():
            return v
    return name                     # 当目录名直接用


def chain_for(target, decoders_dir=DEFAULT_DECODERS_DIR, available=None):
    """按 PD 的 inputs 元数据推导解码链（如 modbus ← uart ← logic）。"""
    chain, seen = [], set()
    cur = resolve(target, decoders_dir)
    while cur and cur not in seen:
        seen.add(cur)
        if available is not None and cur not in available:
            raise PdError("存档里没有 %s 的解码结果/配置，无法参与链路" % cur)
        chain.insert(0, cur)
        try:
            cls = load_decoder_class(cur, decoders_dir)
        except PdError:
            break
        nxt = None
        for inp in (getattr(cls, "inputs", ()) or ()):
            if inp == "logic":
                continue
            try:
                cand = resolve(inp, decoders_dir)
            except PdError:
                continue
            if available is None or cand in available:
                nxt = cand
                break
        cur = nxt
    return chain


def run_from_atkdl(path, protocol=None, stack=None, max_samples=4_000_000,
                   decoders_dir=DEFAULT_DECODERS_DIR, start=None, margin=4000, retries=3):
    """照 `.atkdl` 里存的官方解码器配置跑链路，返回 (chain, results, info)。

    results 是 run_chain 的原始结果（给 transactions()/summarize() 用）；
    info = {start_sample, span, available_samples}。
    """
    import atkdl as _atkdl
    s = _atkdl.read_atkdl(path)
    cfg = {k: v for k, v in (s.decode or {}).items() if isinstance(v, dict) and k != "main"}
    if not cfg:
        raise PdError("这个 .atkdl 里没有解码器配置（Decodes 为空）")
    avail = {resolve(k, decoders_dir) for k in cfg}
    if stack:
        chain = [resolve(x, decoders_dir) for x in stack if x]
        if protocol:
            chain.append(resolve(protocol, decoders_dir))
    elif protocol:
        chain = chain_for(protocol, decoders_dir, available=avail or None)
    else:
        # 挑"最上层"：inputs 指向其它已配协议的优先
        top = None
        for name in cfg:
            d = resolve(name, decoders_dir)
            try:
                cls = load_decoder_class(d, decoders_dir)
            except PdError:
                continue
            ins = [i for i in (getattr(cls, "inputs", ()) or ()) if i != "logic"]
            if ins and (top is None or True):
                top = d
                break
        chain = chain_for(top or resolve(list(cfg)[0], decoders_dir), decoders_dir, available=avail)
    key_of_dir = {}
    for k in cfg:
        key_of_dir[resolve(k, decoders_dir)] = k
    cmaps, opts = {}, {}
    for d in chain:
        node = cfg.get(key_of_dir.get(d, ""), {})
        cmap = {}
        for c in (node.get("channels") or []) + (node.get("opt_channels") or []):
            v = str(c.get("value", "-")).strip()
            if v.lstrip("-").isdigit():
                cmap[c["id"]] = int(v)
        o = {}
        for item in (node.get("options") or []):
            o[item["id"]] = item.get("value")
        cmaps[d], opts[d] = cmap, o
    hw = sorted({h for c in cmaps.values() for h in c.values()})
    if not hw:
        raise PdError("存档里的解码器配置没有绑定任何通道")
    if start is None:
        acts = [s.first_activity(h) for h in hw]
        acts = [a for a in acts if a is not None]
        start = max(0, min(acts) - margin) if acts else 0
    total_avail = max((s.valid_count(h) or len(s.raw_bytes(h)) * 8) for h in hw)
    span, results = max_samples, None
    for _ in range(max(0, int(retries)) + 1):
        samples = {h: s.window(h, start, span) for h in hw}
        results = run_chain(chain, cmaps, opts, s.sample_rate_hz, samples, decoders_dir)
        got = sum(len(r[2].annotations) for r in results)
        capped = span < (total_avail - start)
        if got or not capped or span >= 200_000_000:
            break
        span = min(span * 4, 200_000_000)        # 慢协议(DHT11 一帧 >120ms)自动放大窗口再试
    return chain, results, {"start_sample": start, "span": span,
                            "available_samples": total_avail,
                            "sample_rate_hz": s.sample_rate_hz}


def from_atkdl(path, protocol=None, stack=None, max_samples=4_000_000,
               decoders_dir=DEFAULT_DECODERS_DIR, annotation_limit=200,
               start=None, margin=4000, retries=3):
    """照 `.atkdl` 里存的**官方解码器配置**跑解码（通道/选项/链路全部照抄文件）。

    protocol 省略 → 用存档里"最上层"的那个协议（inputs 指向其它协议的那个）。
    start=None → 自动从"首个跳变前 margin 个采样"开始（官方存档含触发前长空闲，
                  从 0 开始往往什么都解不到）。
    返回 (协议链, summarize 结果)。
    """
    chain, results, info = run_from_atkdl(path, protocol=protocol, stack=stack,
                                          max_samples=max_samples, decoders_dir=decoders_dir,
                                          start=start, margin=margin, retries=retries)
    out = summarize(results, max_items=annotation_limit)
    for layer in out:
        layer["window"] = dict(info)
    return chain, out


# ============================== 对外：跑一串解码器 ==============================

def chains_for(protocols, metadata_of):
    """把 ['uart','modbus'] 这类链按 inputs/outputs 关系排好（返回 [(name, [lower_names])]）。"""
    order, used = [], set()
    for name in protocols:
        order.append(name)
    return order


def run_chain(protocols, channel_maps, options_by_proto, samplerate, samples_by_channel,
              decoders_dir=DEFAULT_DECODERS_DIR, max_samples=None):
    """按顺序跑一串 PD，后面的吃前面吐的 python 包（栈式）。

    protocols: ['uart', 'modbus']（先下后上）
    channel_maps: {协议名: {通道id: 硬件通道号}}
    options_by_proto: {协议名: {选项id: 值}}
    samples_by_channel: {硬件通道号: 0/1 序列}
    """
    lower_python, last_run, results = None, None, []
    for idx, name in enumerate(protocols):
        cmap = channel_maps.get(name) or {}
        run = PdRun(name, cmap, options_by_proto.get(name) or {}, samplerate,
                    samples_by_channel, decoders_dir, max_samples=max_samples)
        res = run.run(lower_python=lower_python)
        results.append((name, run, res))
        lower_python = res.python if res.python else None
        last_run = run
        # 只有"中间层"必须吐 python 包（顶层 outputs 可以是空的，比如 spiflash/modbus）
        if lower_python is None and idx < len(protocols) - 1:
            raise PdError("%s 没有输出 python 包，无法喂给上层 %s（检查通道/选项）"
                          % (name, protocols[idx + 1]))
    return results


def summarize(results, max_items=200):
    """把 run_chain 的结果整理成可 JSON 化的 dict（给 MCP 工具返回）。"""
    out = []
    for name, run, res in results:
        meta = run.metadata()
        bin_decls = [list(b) for b in (getattr(run.cls, "binary", ()) or ())]
        ann_by_row = {}
        for ss, es, idx, texts in res.annotations:
            hit = res.rows.get(idx) if isinstance(idx, int) else None
            rid, rdesc = hit if hit else ((str(idx) if not isinstance(idx, int) else ""),
                                          (str(idx) if not isinstance(idx, int) else str(idx)))
            key = rid or rdesc
            ann_by_row.setdefault(key, {"row": rdesc, "items": []})
            if len(ann_by_row[key]["items"]) < max_items:
                ann_by_row[key]["items"].append({"t": [ss, es], "text": texts})
            ann_by_row[key]["count"] = ann_by_row[key].get("count", 0) + 1
        bins = {}
        for ss, es, idx, blob in res.binary:
            if isinstance(idx, int) and idx < len(bin_decls):
                bid, bname = bin_decls[idx][0], (bin_decls[idx][1] if len(bin_decls[idx]) > 1 else None)
            else:
                bid, bname = str(idx), None
            e = bins.setdefault(bid, {"name": bname, "chunks": 0, "bytes": 0, "sample": None})
            e["chunks"] += 1
            e["bytes"] += len(blob)
            if e["sample"] is None and blob:
                e["sample"] = blob[:64].hex(" ")
        out.append({
            "protocol": name,
            "meta": {k: meta[k] for k in ("id", "name", "inputs", "outputs")},
            "channels": {c: run.hw_of.get(c) for c in run.hw_of},
            "options": {k: v for k, v in run.options.items() if k != "samplerate"},
            "annotations": {k: {"row": v["row"], "count": v["count"], "items": v["items"]}
                            for k, v in ann_by_row.items()},
            "annotation_total": len(res.annotations),
            "binary": bins,
            "python_packets": len(res.python),
            "meta_outputs": res.meta,
        })
    return out


# 只留"有信息量"的注解：纯位值 / 颜色标记 / 单字符高低电平均过滤掉
_NOISE_TEXTS = {"0", "1", "H", "L", "h", "l", "-"}


def transactions(results, max_items=40):
    """把解码结果压成**按时间排序的事务列表**（给 MCU 总线调试看，而不是按行分组的 JSON）。

    返回 {items, text, total, by_protocol, by_row}；text 可直接贴给用户。
    """
    items = []
    for name, run, res in results:
        rate = run.samplerate or 1
        for ss, es, idx, texts in res.annotations:
            txt = (texts[0] if texts else "").strip()
            if not txt or txt in _NOISE_TEXTS or txt.startswith("color:"):
                continue
            row = res.rows.get(idx) if isinstance(idx, int) else None
            items.append({"sample": ss, "t_ms": round(ss / rate * 1000.0, 4),
                          "protocol": name, "row": (row[1] if row else str(idx)),
                          "text": txt})
    items.sort(key=lambda d: d["sample"])
    total = len(items)
    shown = items[:max_items]
    by_protocol, by_row = {}, {}
    for d in items:
        by_protocol[d["protocol"]] = by_protocol.get(d["protocol"], 0) + 1
    for d in shown:
        key = "%s/%s" % (d["protocol"], d["row"])
        by_row[key] = by_row.get(key, 0) + 1
    lines = ["%10.4f ms  [%-14s] %s" % (d["t_ms"], d["protocol"], d["text"]) for d in shown]
    if total > len(shown):
        lines.append("... 另有 %d 条（把 max_items 调大）" % (total - len(shown)))
    return {"items": items, "text": "\n".join(lines), "total": total,
            "shown": len(shown), "by_protocol": by_protocol, "by_row": by_row}
