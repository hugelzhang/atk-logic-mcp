# -*- coding: utf-8 -*-
"""
用 pywinauto 自动操作 Zadig 给 ATK-Logic-Analyzer (VID_1A86 PID_FFCC) 装 WinUSB 驱动。
必须以管理员运行（install_driver.bat 会请求提权）。
设备名自动模糊匹配 (ATK / Logic / Analyzer / CH559 / 1A86)。
"""
import os
import time
import subprocess
import traceback
from pywinauto import Application

HERE = os.path.dirname(os.path.abspath(__file__))
ZADIG = os.path.join(HERE, "zadig.exe")
LOG = os.path.join(HERE, "zadig_auto.log")

DEVICE_KEYWORDS = ("ATK", "Logic", "Analyzer", "CH559", "1A86")


def log(msg):
    line = str(msg)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def dismiss_dialogs(app):
    """关闭弹出的标准对话框（首次运行提示、安装结果提示）"""
    try:
        for w in app.windows():
            try:
                if w.is_visible() and w.class_name() == "#32770":
                    log("  [dialog] " + w.window_text())
                    btn = w.child_window(class_name="Button")
                    btn.click()
                    time.sleep(1)
            except Exception:
                pass
    except Exception:
        pass


def pick_device(combo):
    """从设备下拉框挑出逻辑分析仪（精确优先，失败则模糊匹配）"""
    for it in combo.items():
        if any(k.lower() in it.lower() for k in DEVICE_KEYWORDS):
            return it
    return None


def main():
    if not os.path.exists(ZADIG):
        log("ERROR: zadig.exe not found next to this script: %s" % ZADIG)
        return
    subprocess.Popen([ZADIG])
    time.sleep(4)

    app = Application(backend="win32").connect(title_re=".*Zadig.*", timeout=20)
    win = app.top_window()
    win.wait("visible", timeout=10)
    log("Window: " + win.window_text())

    dismiss_dialogs(app)
    time.sleep(1)

    # 1) Options -> List All Devices
    try:
        win.menu_select("Options->List All Devices")
        log("STEP1 OK: List All Devices")
    except Exception as e:
        log("STEP1 menu error: " + str(e))
    time.sleep(2)

    combos = win.children(class_name="ComboBox")
    log("Combo count: %d" % len(combos))
    if len(combos) >= 1:
        dev = combos[0]
        target = None
        try:
            target = pick_device(dev)
        except Exception as e:
            log("  combo read err: %s" % e)
        if target:
            dev.select(target)
            log("STEP2 OK: selected %s" % target)
        else:
            log("STEP2 WARN: no ATK device found in list (check USB + List All Devices)")
    time.sleep(1)

    # 2) 驱动下拉框选 WinUSB
    if len(combos) >= 2:
        drv = combos[1]
        try:
            drv.select("WinUSB")
            log("STEP3 OK: driver=WinUSB")
        except Exception:
            try:
                for it in drv.items():
                    if "WinUSB" in it:
                        drv.select(it)
                        log("STEP3 OK (fuzzy): " + it)
                        break
            except Exception as e2:
                log("STEP3 driver select err: " + str(e2))
    time.sleep(1)

    # 3) 点击 Install
    btns = [b for b in win.children(class_name="Button")]
    target_btn = None
    for b in btns:
        t = b.window_text()
        if any(k in t for k in ("Install", "Replace", "Reinstall")):
            target_btn = b
            log("STEP4: click " + t)
            break
    if target_btn is not None:
        target_btn.click()
        time.sleep(10)
        dismiss_dialogs(app)
        time.sleep(2)
        log("FLOW COMPLETE - WinUSB installed")
    else:
        log("ERROR: install button not found")


if __name__ == "__main__":
    try:
        main()
        log("SCRIPT COMPLETE")
    except Exception:
        log("SCRIPT ERROR:\n" + traceback.format_exc())
