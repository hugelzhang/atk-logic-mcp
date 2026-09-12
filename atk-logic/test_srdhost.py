# -*- coding: utf-8 -*-
"""sigrok 解码器宿主（`srdhost.py`）回归测试 —— 离线，不需要硬件。

跑法: python test_srdhost.py

覆盖：
  A 组  宿主自身语义（合成波形 + wait() 单元测试）：AND/OR 条件、`{}`/None、skip 超时、
        合成 UART、合成 I²C（不依赖官方样例，纯自证）
  B 组  官方 11 个样例（`C:\\Program Files\\ATK-Logic\\test\\*.atkdl`）照文件里的官方解码器配置
        逐条跑通并断言关键内容（UART/I²C+EEPROM/SPI+Flash/Modbus/CAN/PWM/WS2812/DHT11/SWD/USB PD/MIPI DSI）
  C 组  健壮性：协议名解析、链路推导、无配置存档、未知名 → 报错可读
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import atkdl                                                    # noqa: E402
import srdhost                                                  # noqa: E402

TESTDIR = r"C:/Program Files/ATK-Logic/test"
PASS = FAIL = 0


def check(name, got, want, unit=""):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  ✓ %s = %s%s" % (name, got, unit))
    else:
        FAIL += 1
        print("  ✗ %s = %s%s  (期望 %s)" % (name, got, unit, want))
    return got == want


def check_true(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  ✗ %s %s" % (name, detail))
    return bool(cond)


# ---------------------------------------------------------------- 合成波形

def synth_uart(data, srate_hz, baud, data_bits=8, stop_bits=1, idle_lead=40):
    """8N1，空闲高，LSB 先（与 test_offline.py 里的同一套约定）。"""
    bit = srate_hz / float(baud)
    s = bytearray([1] * idle_lead)
    for byte in data:
        s += bytearray([0] * int(round(bit)))
        for b in range(data_bits):
            s += bytearray([(byte >> b) & 1] * int(round(bit)))
        s += bytearray([1] * int(round(bit * stop_bits)))
    s += bytearray([1] * idle_lead)
    return bytes(s)


def synth_i2c(addr, data, srate=10_000_000, scl_hz=100_000):
    """START + 地址 + ACK + 数据 + ACK + STOP。"""
    half = srate // (scl_hz * 2)
    scl, sda = bytearray(), bytearray()

    def idle(n=2):
        scl.extend([1] * n * half)
        sda.extend([1] * n * half)

    def bit(v):
        scl.extend([0] * half)
        sda.extend([v] * half)
        scl.extend([1] * half)
        sda.extend([v] * half)

    idle()
    scl.extend([1] * half)
    sda.extend([1] * half)
    scl.extend([1] * half)
    sda.extend([0] * half)                       # SCL 高时 SDA 下降 = START
    scl.extend([0] * half)
    sda.extend([0] * half)
    for i in range(7, -1, -1):
        bit((addr >> i) & 1)
    bit(0)                                       # ACK
    for i in range(7, -1, -1):
        bit((data >> i) & 1)
    bit(0)                                       # ACK
    scl.extend([1] * half)
    sda.extend([0] * half)
    scl.extend([1] * half)
    sda.extend([1] * half)                       # STOP
    idle(4)
    return bytes(scl), bytes(sda)


def packet_values(res, ptype):
    """从 PD 的 python 包里取某类型的数据值。"""
    out = []
    for _ss, _es, data in res.python:
        if isinstance(data, (list, tuple)) and data and data[0] == ptype:
            v = data[2] if len(data) > 2 else (data[1] if len(data) > 1 else None)
            out.append(v[0] if isinstance(v, (list, tuple)) and v else v)
    return out


# ---------------------------------------------------------------- A 组：宿主语义

class Probe(srdhost.PdDecoderBase):
    """测试用假 PD：按脚本依次 wait()，把结果记下来。"""
    api_version = 3
    id = "probe"
    name = "Probe"
    inputs = ["logic"]
    outputs = []
    channels = ({"id": "a", "name": "A", "desc": "a"}, {"id": "b", "name": "B", "desc": "b"})
    optional_channels = ()
    options = ()
    annotations = (("probe", "Probe"),)
    binary = ()

    def start(self):
        self.out = self.register(srdhost.OUTPUT_ANN)

    def decode(self):
        for cond in self.CONDS:
            pins = self.wait(cond)
            self.CALLS.append((cond, self.samplenum, pins, tuple(self.matched)))


def group_a_engine():
    print("\n== A1 组：wait() 语义（引擎单元测试）==")
    # 0:  --____----__  a(SCL): [0,0,1,1,1,1,1,1,1,1,0,0,1,1]
    a = [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 1, 1]
    # 1:  _____-__----  b(SDA): 在 6 处下降，而 a(SCL) 仍为高 → 模拟 I²C START
    b = [1, 1, 1, 1, 1, 1, 0, 0, 1, 1, 1, 1, 1, 1]
    src = srdhost.LogicSource({0: a, 1: b}, len(a))
    check("AND 跨段: {0:h,1:f} 命中 SDA 下降点", list(srdhost._candidates(src, {0: "h", 1: "f"}, 0))[:1], [6])
    check("电平条件落在段中间也命中", list(srdhost._candidates(src, {0: "h"}, 3))[:1], [4])
    check("边沿条件", list(srdhost._candidates(src, {0: "r"}, 0))[:3], [2, 12])
    check("skip 超时", list(srdhost._candidates(src, {"skip": 5}, 0))[:1], [5])
    check("纯 skip 单条", list(srdhost._candidates(src, {"skip": 11}, 0))[:1], [11])

    # 用假 PD 验证 self.matched / pins / 前进语义
    run = srdhost.PdRun("probe", {"a": 0, "b": 1}, {}, 1_000_000, {0: a, 1: b}, cls=Probe)
    Probe.CONDS = [{}, {"skip": 5}, [{0: "r"}, {1: "f"}], None]
    Probe.CALLS = []
    run.run()
    calls = Probe.CALLS
    check("{} 取当前采样点（不前进）", calls[0][1], 0)
    check("{} 返回引脚值", calls[0][2], (0, 1))
    check("skip 5", calls[1][1], 5)
    check("OR 列表命中下标 1（SDA 下降）", list(calls[2][3]).index(True), 1)
    check("OR 命中位置", calls[2][1], 6)
    check("None 前进一格", calls[3][1], 7)


def group_a_synth():
    print("\n== A2 组：合成波形（不依赖官方样例）==")
    smp = synth_uart(b"ATK", 1_000_000, 115200)
    res = srdhost.run_chain(["uart"], {"uart": {"tx": 0}},
                            {"uart": {"baudrate": 115200, "format": "ascii"}},
                            1_000_000, {0: smp})
    vals = [v for v in packet_values(res[-1][2], "DATA") if isinstance(v, int)]
    check_true("UART 解出字节", vals[:3] == [ord("A"), ord("T"), ord("K")], "(%s)" % vals[:3])

    scl, sda = synth_i2c(0x50, 0xA5)
    res2 = srdhost.run_chain(["i2c"], {"i2c": {"scl": 0, "sda": 1}},
                             {"i2c": {"address_format": "shifted", "packets_format": "hex"}},
                             10_000_000, {0: scl, 1: sda})
    py = res2[-1][2].python
    types = [p[2][0] for p in py if isinstance(p[2], (list, tuple)) and p[2]]
    check_true("I²C 有 START", "START" in types, "(%s)" % types[:4])
    check_true("I²C 有 STOP", "STOP" in types)
    check("I²C 地址 0x50>>1", packet_values(res2[-1][2], "ADDRESS WRITE"), [0x28])
    check("I²C 数据 0xA5", packet_values(res2[-1][2], "DATA WRITE"), [0xA5])


# ---------------------------------------------------------------- B 组：官方样例

SAMPLES = [
    ("uart_tx_115200.atkdl", ["uart"], [("tx-packets", "ALIENTEK")]),
    ("eeprom_24c02_i2c.atkdl", ["i2c", "eeprom24xx"],
     [("packets", "0x50 WR"), ("ops", "Byte write")]),
    ("spi_flash_w25q128.atkdl", ["spi", "spiflash"],
     [("mosi-data-vals", "ff"), ("bits", "command")]),
    ("modbus.atkdl", ["uart", "modbus"], [("sc", "Slave ID: 1"), ("sc", "Read Coils")]),
    ("can_500K.atkdl", ["can"], [("fields", "Identifier"), ("packets", "0x98")]),
    ("pwm_10M_30_25.atkdl", ["pwm"],
     [("frequency-vals", "10.000 MHz"), ("duty-cycle-vals", "30.000000%")]),
    ("rgb_led_ws2812.atkdl", ["rgb_led_ws281x"], [("rgb-val", "RGB#")]),
    ("AM230x_DHT11.atkdl", ["am230x"],
     [("results", "Humidity: 55.0 %"), ("results", "Temperature: 30.0 °C"), ("results", "Checksum: OK")]),
    ("swd.atkdl", ["swd"], [("datas", "0x1ba01477"), ("reads", "IDCODE")]),
    ("USB PD.atkdl", ["usb_power_delivery"], [("payloads", "5V 3A"), ("types", "SOURCE CAP")]),
    ("MIPI_DSI_lP.atkdl", ["mipi_dsi"], [("LPData", "Escape mode entry")]),
]


def row_text(layer, row):
    """把某一行的注解文本拼起来（用于子串断言）。"""
    v = layer["annotations"].get(row)
    if not v:
        return ""
    return " | ".join("/".join(it["text"]) for it in v["items"])


def group_b():
    print("\n== B 组：官方 11 个样例（照文件里的官方解码器配置）==")
    if not os.path.isdir(TESTDIR):
        print("  !! 找不到官方样例目录 %s —— 跳过" % TESTDIR)
        return
    for fname, want_chain, checks in SAMPLES:
        path = os.path.join(TESTDIR, fname)
        if not os.path.exists(path):
            check_true("%s 存在" % fname, False, "(文件缺失)")
            continue
        t0 = time.time()
        try:
            chain, out = srdhost.from_atkdl(path, max_samples=2_000_000, annotation_limit=30)
        except Exception as e:                                   # noqa: BLE001
            check_true("%s 解码" % fname, False, "(%s: %s)" % (type(e).__name__, e))
            continue
        dt = time.time() - t0
        check("%s 协议链" % fname, chain, want_chain)
        layers = {l["protocol"]: l for l in out}
        total = sum(l["annotation_total"] for l in out)
        check_true("%s 有注解" % fname, total > 0, "(%d 条, %.2f s)" % (total, dt))
        for row, needle in checks:
            hit = any(needle in row_text(l, row) for l in out)
            check_true("%s [%s] 含 %r" % (fname, row, needle), hit,
                       "" if hit else "(实际: %s)" % [r for r in layers for r in layers[r]["annotations"]][:6])
        # 链路每一层都该有内容（顶层协议除外时也应有注解）
        for proto in want_chain:
            if proto == "uart" and want_chain != ["uart"]:
                continue                                          # 栈式链里 uart 只是中间层
            check_true("%s 层 %s 有输出" % (fname, proto),
                       layers.get(proto, {}).get("annotation_total", 0) > 0)


# ---------------------------------------------------------------- C 组：健壮性

def group_c():
    print("\n== C 组：健壮性 / 错误可读性 ==")
    check("解码器数量", len(srdhost.list_decoders()) >= 200, True, " 个")
    check("名字解析 UART", srdhost.resolve("UART"), "uart")
    check("名字解析 I²C", srdhost.resolve("I²C"), "i2c")
    check("名字解析 RGB LED WS2812+", srdhost.resolve("RGB LED WS2812+"), "rgb_led_ws281x")
    check("链路推导 Modbus", srdhost.chain_for("modbus"), ["uart", "modbus"])
    check("链路推导 24xx EEPROM", srdhost.chain_for("24xx EEPROM"), ["i2c", "eeprom24xx"])
    check("链路推导 SPI flash", srdhost.chain_for("SPI flash/EEPROM"), ["spi", "spiflash"])
    # 未知名
    try:
        srdhost.PdRun("no-such-protocol", {}, {}, 1_000_000, {})
        check_true("未知协议报错", False)
    except srdhost.PdError as e:
        check_true("未知协议报错", "no-such-protocol" in str(e), "(%s)" % str(e)[:60])
    # 我们自己写的存档没有解码器配置 → 应报可读错误
    demo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "波形存档",
                        "demo_pwm1k_1MHz.atkdl")
    if os.path.exists(demo):
        try:
            srdhost.from_atkdl(demo)
            check_true("无解码配置的存档报错", False)
        except srdhost.PdError as e:
            check_true("无解码配置的存档报错", "解码器配置" in str(e), "(%s)" % str(e)[:50])
    else:
        print("  - 跳过：找不到 %s" % demo)
    # 缺通道应报可读错误（pwm 需要 data 通道）
    try:
        srdhost.run_chain(["pwm"], {"pwm": {}}, {}, 1_000_000, {})
        check_true("缺通道报错", False, "(没报错)")
    except srdhost.PdError as e:
        check_true("缺通道报错", "data" in str(e), "(%s)" % str(e)[:70])


# ---------------------------------------------------------------- D 组：事务列表

TXN_SAMPLES = [
    ("eeprom_24c02_i2c.atkdl", ["Address write", "Byte write"]),
    ("modbus.atkdl", ["Slave ID: 1", "Read Coils"]),
    ("can_500K.atkdl", ["Identifier", "0x98"]),
    ("spi_flash_w25q128.atkdl", ["81", "2B"]),
    ("uart_tx_115200.atkdl", ["ALIENTEK"]),
    ("swd.atkdl", ["IDCODE"]),
    ("USB PD.atkdl", ["SOURCE CAP"]),
    ("AM230x_DHT11.atkdl", ["Humidity"]),
]


def group_d():
    print("\n== D 组：事务列表 transactions()（MCU 总线调试的呈现层）==")
    if not os.path.isdir(TESTDIR):
        print("  !! 找不到官方样例目录 —— 跳过")
        return
    for fname, needles in TXN_SAMPLES:
        path = os.path.join(TESTDIR, fname)
        if not os.path.exists(path):
            check_true("%s 存在" % fname, False, "(文件缺失)")
            continue
        try:
            chain, results, info = srdhost.run_from_atkdl(path, max_samples=2_000_000)
            tx = srdhost.transactions(results, max_items=15)
        except Exception as e:                                    # noqa: BLE001
            check_true("%s 事务列表" % fname, False, "(%s: %s)" % (type(e).__name__, e))
            continue
        first = tx["text"].split("\n")[0] if tx["text"] else ""
        check_true("%s 事务条数 > 0" % fname, tx["total"] > 0, "(%d 条; %s)" % (tx["total"], first[:60]))
        for n in needles:
            check_true("%s 事务含 %r" % (fname, n), n in tx["text"])
        check_true("%s 已滤掉位/颜色噪声" % fname,
                   "color:" not in tx["text"] and "\n        1\n" not in tx["text"])
        check_true("%s 按时间排序" % fname,
                   all(tx["items"][i]["sample"] <= tx["items"][i + 1]["sample"]
                       for i in range(len(tx["items"]) - 1)))


def main():
    print("sigrok 解码器宿主回归（离线，不需要硬件）")
    group_a_engine()
    group_a_synth()
    group_b()
    group_c()
    group_d()
    print("\n" + "=" * 62)
    print("结果: %d 项通过 / %d 项失败" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
