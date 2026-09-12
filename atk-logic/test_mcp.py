# 端到端测试 atk-logic MCP 桥 (开箱即跑, 路径自动定位)
#
# 用法: <本包 venv python> test_mcp.py
# 注意: 运行时不要让别的东西占用设备 (Hermes/Claude 自己的 atk-logic MCP 会占用);
#       需要 CH0 接着逻辑分析仪自带的 PWM0 输出。
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")
_VENV_PY = os.path.join(HERE, "..", ".venv", "Scripts", "python.exe")
PY = _VENV_PY if os.path.exists(_VENV_PY) else sys.executable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

FAIL = []


def fmt(resp):
    parts = [getattr(c, "text", "") for c in resp.content]
    return "\n".join(p for p in parts if p) or str(resp)


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ((" — " + detail) if detail else ""))
    if not ok:
        FAIL.append(name)


async def main():
    # env 必须显式继承：MCP SDK 默认只转发 PATH 等少量变量，
    # 否则 ATK_ALLOW_MULTI=1 这类逃生口传不进子进程（互斥照旧拦住）
    params = StdioServerParameters(command=PY, args=[SERVER], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = [t.name for t in (await session.list_tools()).tools]
            print("工具:", tools)
            check("工具清单完整",
                  all(t in tools for t in ("identify", "status", "reset", "pwm", "capture",
                                           "capture_multi", "save_capture", "decode_uart")),
                  "%d 个" % len(tools))

            print("\n== identify / status ==")
            st = fmt(await session.call_tool("status", {}))
            if "占用" in st:
                print(st.strip())
                print("\n设备被其他进程占用（通常是 Hermes/Claude 的 atk-logic MCP 在跑）。"
                      "先停掉它再跑本测试 —— 这是 v2 跨进程互斥在起作用。")
                return 2
            print(fmt(await session.call_tool("identify", {})).strip())
            print(st.strip())

            print("\n== pwm 1kHz/50% + capture CH0 ==")
            print(fmt(await session.call_tool("pwm", {"hz": 1000, "duty_pct": 50})).strip())
            r = await session.call_tool("capture", {"channel": 0, "depth": 200000,
                                                    "sample_rate_mhz": 1, "trigger": "rising",
                                                    "max_points": 50})
            import json
            cap = json.loads(fmt(r))
            check("采到 200000 采样", cap.get("points") == 200000, "points=%s" % cap.get("points"))
            check("频率 ≈1000Hz", abs((cap.get("frequency_hz") or 0) - 1000) < 5,
                  "%.2f Hz" % (cap.get("frequency_hz") or -1))
            check("占空比 ≈50%", abs((cap.get("high_pct") or 0) - 50) < 1,
                  "%.2f%%" % (cap.get("high_pct") or -1))
            check("无毛刺", cap.get("glitch_edges") == 0 and not cap.get("narrow_pulses"),
                  "glitch=%s narrow=%s" % (cap.get("glitch_edges"), cap.get("narrow_pulses")))
            check("有电平变化点", len(cap.get("transitions") or []) > 0,
                  "%d 个变化点" % len(cap.get("transitions") or []))

            print("\n== capture_multi CH0~CH3 ==")
            r = await session.call_tool("capture_multi", {"channels": "0,1,2,3", "depth": 100000,
                                                          "sample_rate_mhz": 1})
            multi = json.loads(fmt(r))
            got = multi.get("channels", {})
            check("4 通道统计都有点数", all(v.get("points") == 100000 for v in got.values()),
                  ", ".join("%s:%sHz" % (k, v.get("frequency_hz")) for k, v in got.items()))

            print("\n== save_capture 全量存盘 ==")
            tmp = tempfile.mkdtemp(prefix="atk_mcp_")
            out = fmt(await session.call_tool("save_capture", {"channel": 0, "depth": 80000,
                                                               "sample_rate_mhz": 1,
                                                               "out_dir": tmp}))
            print(out.strip())
            csvs = [f for f in os.listdir(tmp) if f.endswith(".csv")]
            check("生成 CSV", bool(csvs), ", ".join(csvs))
            if csvs:
                path = os.path.join(tmp, csvs[0])
                n = sum(1 for _ in open(path, encoding="utf-8")) - 2   # 头两行注释+表头
                check("CSV 为全量 80000 点", n == 80000, "%d 行" % n)

            print("\n== decode_uart (CH1 悬空, 只验证调用链与自动波特率不崩) ==")
            r = await session.call_tool("decode_uart", {"channel": 1, "depth": 200000,
                                                        "sample_rate_mhz": 25, "max_bytes": 64})
            dec = json.loads(fmt(r))
            check("decode_uart 有返回且无异常",
                  "error" in dec or "bytes_decoded" in dec,
                  str(dec.get("bytes_decoded") or dec.get("error"))[:60])

            print("\n== 离线工具: list_protocols / decode_protocol / load_waveform ==")
            tools2 = [t.name for t in (await session.list_tools()).tools]
            check("新工具已注册",
                  all(t in tools2 for t in ("decode_protocol", "list_protocols", "load_waveform",
                                            "bus_decode")),
                  "共 %d 个工具" % len(tools2))
            protos = fmt(await session.call_tool("list_protocols", {"filter": "i2c"}))
            check("list_protocols 能找到 i2c", "i2c" in protos, protos.strip()[:60])
            OFFICIAL = r"C:\Program Files\ATK-Logic\test"
            uart_sample = os.path.join(OFFICIAL, "uart_tx_115200.atkdl")
            if os.path.exists(uart_sample):
                # 离线: 不占设备
                lw = json.loads(fmt(await session.call_tool(
                    "load_waveform", {"path": uart_sample, "channel": -1,
                                      "max_samples": 600000, "uart_baud": -1})))
                check("load_waveform 读出 10MHz",
                      lw.get("sample_rate_hz") == 10_000_000,
                      str(lw.get("sample_rate_hz") or lw.get("error"))[:60])
                dp = json.loads(fmt(await session.call_tool(
                    "decode_protocol", {"protocol": "uart", "path": uart_sample})))
                rows = dp["layers"][-1]["annotations"] if "layers" in dp else {}
                txt = " ".join("/".join(i["text"]) for v in rows.values() for i in v["items"])
                check("decode_protocol 照存档配置解出 ALIENTEK", "ALIENTEK" in txt,
                      str(dp.get("chain") or dp.get("error")))
                modbus_sample = os.path.join(OFFICIAL, "modbus.atkdl")
                if os.path.exists(modbus_sample):
                    dp2 = json.loads(fmt(await session.call_tool(
                        "decode_protocol", {"protocol": "modbus", "path": modbus_sample,
                                            "max_samples": 3000000})))
                    rows2 = " ".join("/".join(i["text"]) for L in dp2.get("layers", [])
                                     for v in L["annotations"].values() for i in v["items"])
                    check("decode_protocol 栈式 uart→modbus 解出 Slave ID",
                          "Slave ID" in rows2, str(dp2.get("chain")))
            else:
                print("  - 跳过: 找不到官方样例目录")

            print("\n== 200MHz 档位 (capture) ==")
            # depth 要覆盖 ≥2 个周期（200MHz 下 200000 采样只有 1ms = 1 个 1kHz 周期）
            r = await session.call_tool("capture", {"channel": 0, "depth": 600000,
                                                    "sample_rate_mhz": 200, "max_points": 0})
            cap200 = json.loads(fmt(r))
            check("200MHz 采集周期 = 200000 采样",
                  (cap200.get("period_samples_median") == 200000),
                  "period=%s freq=%s" % (cap200.get("period_samples_median"),
                                         cap200.get("frequency_hz")))

            print("\n== RLE 压缩采集 (flags bit6) ==")
            rl = json.loads(fmt(await session.call_tool(
                "capture", {"channel": 0, "depth": 400000, "sample_rate_mhz": 1,
                            "trigger": "any", "max_points": 0, "rle": True})))
            check("RLE 采集回压缩信息", rl.get("rle") is True and (rl.get("rle_pairs") or 0) > 0,
                  "pairs=%s wire=%s" % (rl.get("rle_pairs"), rl.get("wire_bytes")))
            check("RLE 压缩率 > 50%", (rl.get("compression_pct") or 0) > 50,
                  "%s%%" % rl.get("compression_pct"))
            check("RLE 展开后统计与不压缩一致 (1000Hz/50%)",
                  abs((rl.get("frequency_hz") or 0) - 1000) < 2
                  and abs((rl.get("duty_pct") or 0) - 50) < 1,
                  "freq=%s duty=%s" % (rl.get("frequency_hz"), rl.get("duty_pct")))

            print("\n== bus_decode (MCU 总线调试: 事务列表) ==")
            bd = json.loads(fmt(await session.call_tool(
                "bus_decode", {"path": os.path.join(OFFICIAL, "eeprom_24c02_i2c.atkdl"),
                               "live": False, "max_items": 8})))
            check("bus_decode 照存档解出事务", "Byte write" in (bd.get("text") or ""),
                  str(bd.get("chain") or bd.get("error")))
            bd2 = json.loads(fmt(await session.call_tool(
                "bus_decode", {"live": True, "protocol": "pwm", "channels": "data=0",
                               "depth": 400000, "sample_rate_mhz": 1, "trigger": "any",
                               "max_items": 4})))
            check("bus_decode 真机解出事务并自动存 .atkdl",
                  (bd2.get("transaction_total") or 0) > 0
                  and os.path.exists(bd2.get("saved_atkdl") or ""),
                  "total=%s saved=%s" % (bd2.get("transaction_total"), bd2.get("saved_atkdl")))

            print("\n== reset (句柄重建) ==")
            print(fmt(await session.call_tool("reset", {})).strip())
            r = await session.call_tool("capture", {"channel": 0, "depth": 20000,
                                                    "sample_rate_mhz": 1, "max_points": 0})
            cap = json.loads(fmt(r))
            check("reset 后仍能采集", cap.get("points") == 20000, "points=%s" % cap.get("points"))

            print("\n== pwm stop ==")
            print(fmt(await session.call_tool("pwm", {"stop": True})).strip())

    print()
    if FAIL:
        print("失败项: %s" % FAIL)
        return 1
    print("MCP 端到端测试全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
