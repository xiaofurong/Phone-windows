#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ccd_toggle.py — 用 CCD API（QueryDisplayConfig / SetDisplayConfig）禁用/启用某台物理显示器
的显示路径。用于实现 Windows 11「设置→显示→断开此显示器」的程序化版本。

背景：XV320QU LM（Acer 副显，独显 RTX4060 输出）接的 HDMI1 始终有 Windows 信号，
导致显示器 auto-source 无法跳到 HDMI2（电视盒子）。通过在 Windows 侧禁用该显示路径，
让 HDMI1 无信号 → 显示器 auto-source 自动跳到 HDMI2（盒子），从而绕开 DDC 固件对
HDMI2 切换值(18)不支持的限制。

用法：
  python ccd_toggle.py probe <model>          # 只读：打印匹配的路径(不改变状态)
  python ccd_toggle.py off <model>            # 禁用该显示器路径 (HDMI1 断信号)
  python ccd_toggle.py on <model>             # 重新启用该显示器路径
  python ccd_toggle.py list                    # 列出所有路径及其关联显示器型号
model 可为型号子串，如 "XV320QU"。
"""
import sys
import re
import ctypes
from ctypes import (Structure, c_int, c_uint32, c_uint16, c_wchar, c_byte,
                    byref, sizeof, POINTER, cast, c_void_p)

user32 = ctypes.windll.user32
# 设 DPI aware，避免 GetDpiForMonitor/枚举受虚拟化影响
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


# ---------------- CCD 结构 ----------------
class LUID(Structure):
    _fields_ = [("LowPart", c_uint32), ("HighPart", c_int)]


class PATH_SRC(Structure):
    _fields_ = [("adapterId", LUID), ("id", c_uint32),
                ("modeInfoIdx", c_uint32), ("statusFlags", c_uint32)]


class RATIONAL(Structure):
    _fields_ = [("Numerator", c_uint32), ("Denominator", c_uint32)]


class PATH_TGT(Structure):
    _fields_ = [("adapterId", LUID), ("id", c_uint32), ("modeInfoIdx", c_uint32),
                ("outputTechnology", c_uint32), ("rotation", c_uint32), ("scaling", c_uint32),
                ("refreshRate", RATIONAL), ("scanLineOrdering", c_uint32),
                ("targetAvailable", c_uint32), ("statusFlags", c_uint32)]


class PATH_INFO(Structure):
    _fields_ = [("sourceInfo", PATH_SRC), ("targetInfo", PATH_TGT), ("flags", c_uint32)]


class DEVINFO_HDR(Structure):
    _fields_ = [("type", c_int), ("size", c_uint32), ("adapterId", LUID), ("id", c_uint32)]


# DISPLAYCONFIG_DEVICE_INFO_TYPE
DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2


class SRC_NAME(Structure):
    _fields_ = [("header", DEVINFO_HDR), ("viewGdiDeviceName", c_wchar * 32)]


class TARGET_DEVICE_NAME(Structure):
    _fields_ = [
        ("header", DEVINFO_HDR),
        ("flags", c_uint32),            # DISPLAYCONFIG_TARGET_DEVICE_NAME_FLAGS (monitorUsage...)
        ("outputTechnology", c_uint32),
        ("edidManufactureId", c_uint16),
        ("edidProductCodeId", c_uint16),
        ("connectorInstance", c_uint32),
        ("monitorFriendlyDeviceName", c_wchar * 64),
        ("monitorDevicePath", c_wchar * 128),
    ]


# QDC / SDC flags
QDC_ALL_PATHS = 0x00000001
QDC_ONLY_ACTIVE_PATHS = 0x00000002
QDC_DATABASE_CURRENT = 0x00000004

SDC_TOPOLOGY_INTERNAL = 0x00000001
SDC_TOPOLOGY_CLONE = 0x00000002
SDC_TOPOLOGY_EXTEND = 0x00000004
SDC_TOPOLOGY_EXTERNAL = 0x00000008
DISPLAYCONFIG_PATH_ACTIVE = 0x00000001
DISPLAYCONFIG_PATH_SUPPORT_VIRTUAL_MODE = 0x00000008
# 让 SetDisplayConfig 自己重建 mode 的哨兵值
DISPLAYCONFIG_PATH_MODE_IDX_INVALID = 0xFFFFFFFF

SDC_TOPOLOGY_SUPPLIED = 0x00000010
SDC_USE_SUPPLIED_DISPLAY_CONFIG = 0x00000020
SDC_VALIDATE = 0x00000040
SDC_APPLY = 0x00000080
SDC_NO_OPTIMIZATION = 0x00000100
SDC_SAVE_TO_DATABASE = 0x00000200
SDC_ALLOW_CHANGES = 0x00000400
SDC_ALLOW_PATH_ORDER_CHANGES = 0x00002000

QDC_VIRTUAL_MODE_AWARE = 0x00000010

# DISPLAYCONFIG_OUTPUT_TECHNOLOGY 取值(内置屏/笔记本面板的标记位)
DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INTERNAL = 0x80000000


ERROR_SUCCESS = 0
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_INVALID_PARAMETER = 87


def _query_paths(flag):
    """枚举显示路径(带 VIRTUAL_MODE_AWARE + 缓冲区不足重试)。返回 (paths数组, count)。"""
    flags = flag | QDC_VIRTUAL_MODE_AWARE
    while True:
        np = c_uint32(0)
        nm = c_uint32(0)
        if user32.GetDisplayConfigBufferSizes(flags, byref(np), byref(nm)) != 0:
            return None, 0
        if np.value == 0:
            return [], 0
        paths = (PATH_INFO * np.value)()
        modes = (c_byte * (nm.value * 64))()
        ret = user32.QueryDisplayConfig(flags, byref(np), paths, byref(nm), modes, None)
        if ret == ERROR_INSUFFICIENT_BUFFER:
            continue  # 显示器拓扑在两次调用间变了，重试
        if ret != ERROR_SUCCESS:
            return None, 0
        return paths, np.value


def _path_desc(path):
    """返回该路径的 GDI source 名 + 显示器型号(EDID) + target 描述。"""
    # source gdi name
    src_name = ""
    sn = SRC_NAME()
    sn.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
    sn.header.size = sizeof(SRC_NAME)
    sn.header.adapterId = path.sourceInfo.adapterId
    sn.header.id = path.sourceInfo.id
    if user32.DisplayConfigGetDeviceInfo(byref(sn)) == 0:
        src_name = sn.viewGdiDeviceName.rstrip("\x00")

    # target monitor friendly name (EDID 型号)
    model = ""
    tn = TARGET_DEVICE_NAME()
    tn.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
    tn.header.size = sizeof(TARGET_DEVICE_NAME)
    tn.header.adapterId = path.targetInfo.adapterId
    tn.header.id = path.targetInfo.id
    if user32.DisplayConfigGetDeviceInfo(byref(tn)) == 0:
        model = tn.monitorFriendlyDeviceName.rstrip("\x00")

    return src_name, model


def _path_devpath(path):
    """返回该路径 target 的 monitor device path(含 PNP 型号, 如 \\?\\DISPLAY#CMN1629#...)。
    内置屏(笔记本面板)通常没有 EDID friendly name(model 为空)，但 device path 里带面板 PNP ID，
    用于识别/区分内置屏。"""
    tn = TARGET_DEVICE_NAME()
    tn.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
    tn.header.size = sizeof(TARGET_DEVICE_NAME)
    tn.header.adapterId = path.targetInfo.adapterId
    tn.header.id = path.targetInfo.id
    if user32.DisplayConfigGetDeviceInfo(byref(tn)) == 0:
        return tn.monitorDevicePath.rstrip("\x00")
    return ""


def _path_pnp(path):
    """从 device path 提取 PNP 面板型号(如 CMN1629 / AOCB450 / ACR0A2A)。
    内置屏没有 EDID friendly name(model 为空)，靠 PNP ID 识别身份。"""
    dp = _path_devpath(path)
    m = re.search(r"DISPLAY#([0-9A-Za-z]+)#", dp)
    return m.group(1) if m else ""


def list_paths():
    """列出当前数据库(含所有可能拓扑)的路径。"""
    paths, n = _query_paths(QDC_ALL_PATHS)
    if paths is None:
        print("QueryDisplayConfig 失败")
        return []
    out = []
    for i in range(n):
        p = paths[i]
        src, model = _path_desc(p)
        active = "ACTIVE" if (p.flags & DISPLAYCONFIG_PATH_ACTIVE) else "off"
        # 用 (adapterId,srcId,targetId) 定位
        tag = (p.sourceInfo.adapterId.LowPart, p.sourceInfo.adapterId.HighPart,
               p.sourceInfo.id, p.targetInfo.id)
        out.append({"idx": i, "src": src, "model": model, "active": active, "flags": p.flags, "tag": tag})
    return out


def find_indices_by_model(model_sub, active_only=False):
    """返回与型号子串匹配的路径下标列表。"""
    idxs = []
    paths, n = _query_paths(QDC_ALL_PATHS)
    if paths is None:
        return []
    for i in range(n):
        src, model = _path_desc(paths[i])
        if active_only and not (paths[i].flags & DISPLAYCONFIG_PATH_ACTIVE):
            continue
        if model_sub.lower() in model.lower() or model_sub.lower() in src.lower():
            idxs.append(i)
    return idxs


def _filter_paths(paths_list):
    """
    精简路径集供 SetDisplayConfig 提交（参照 sgrottel/ToggleDisplay 的做法）：
    1. 去掉 targetAvailable=0 的僵尸路径；
    2. enabled 优先；
    3. 去掉"有 enabled 同名同 target"的 disabled 替代路径（克隆/扩展占位）；
    4. 去掉仅 source.id 不同、其余全同的 disabled 冗余路径。
    paths_list 为 list[PATH_INFO]。返回 list[PATH_INFO]。
    """
    # 1. targetAvailable
    kept = [p for p in paths_list if p.targetInfo.targetAvailable]

    def is_en(p):
        return bool(p.flags & DISPLAYCONFIG_PATH_ACTIVE)

    # 2. enabled 排前（保持其余相对顺序稳定即可，无需严格排序）
    kept.sort(key=lambda p: (0 if is_en(p) else 1))

    # 3. 去掉 enabled 显示器/target 的 disabled 替代
    enabled_displays = set()
    enabled_targets = set()
    for p in kept:
        if is_en(p):
            src, _ = _path_desc(p)
            if src:
                enabled_displays.add(src)
            enabled_targets.add((p.targetInfo.adapterId.LowPart,
                                 p.targetInfo.adapterId.HighPart,
                                 p.targetInfo.id))
    out = []
    for p in kept:
        if is_en(p):
            out.append(p)
            continue
        src, _ = _path_desc(p)
        tkey = (p.targetInfo.adapterId.LowPart,
                p.targetInfo.adapterId.HighPart,
                p.targetInfo.id)
        if src and src in enabled_displays:
            continue
        if tkey in enabled_targets:
            continue
        out.append(p)

    # 4. 去掉仅 source.id 不同的 disabled 冗余（比较时把 sourceInfo.id 归零）
    seen = set()
    filtered = []
    for p in out:
        if is_en(p):
            filtered.append(p)
            continue
        c = PATH_INFO()
        c = p  # 浅拷贝引用即可用于读取；此处仅做去重比较
        # 构造忽略 source.id 的键
        key = (
            p.sourceInfo.adapterId.LowPart, p.sourceInfo.adapterId.HighPart,
            0, p.sourceInfo.modeInfoIdx, p.sourceInfo.statusFlags,
            p.targetInfo.adapterId.LowPart, p.targetInfo.adapterId.HighPart,
            p.targetInfo.id, p.targetInfo.modeInfoIdx, p.targetInfo.outputTechnology,
            p.targetInfo.rotation, p.targetInfo.scaling,
            p.targetInfo.refreshRate.Numerator, p.targetInfo.refreshRate.Denominator,
            p.targetInfo.scanLineOrdering, p.targetInfo.targetAvailable,
            p.targetInfo.statusFlags, p.flags & ~DISPLAYCONFIG_PATH_ACTIVE,
        )
        if key in seen:
            continue
        seen.add(key)
        filtered.append(p)
    return filtered


def _apply(paths_list):
    """双阶段 SetDisplayConfig：先 VALIDATE 再 APPLY（ToggleDisplay 标准做法）。"""
    for p in paths_list:
        p.sourceInfo.modeInfoIdx = DISPLAYCONFIG_PATH_MODE_IDX_INVALID
        p.targetInfo.modeInfoIdx = DISPLAYCONFIG_PATH_MODE_IDX_INVALID
    n = len(paths_list)
    arr = (PATH_INFO * n)() if n else (PATH_INFO * 1)()
    for j, p in enumerate(paths_list):
        arr[j] = p
    base = SDC_TOPOLOGY_SUPPLIED | SDC_ALLOW_PATH_ORDER_CHANGES
    ret = user32.SetDisplayConfig(n, arr, 0, None, SDC_VALIDATE | base)
    if ret != 0:
        return ret, "validate"
    ret = user32.SetDisplayConfig(n, arr, 0, None, SDC_APPLY | base)
    return ret, "apply"


def set_active(model_sub, want_active):
    """
    禁用/启用匹配型号的显示路径。
    用 QDC_ALL_PATHS 取全量路径(含各种克隆/扩展拓扑)，对目标路径改 ACTIVE 位，
    FilterPaths 精简后，SDC_TOPOLOGY_SUPPLIED 双阶段提交。
    """
    paths, n = _query_paths(QDC_ALL_PATHS)
    if paths is None or n == 0:
        return "无显示路径"

    paths_list = [paths[i] for i in range(n)]
    matched = 0
    for p in paths_list:
        src, model = _path_desc(p)
        if model_sub.lower() not in model.lower() and model_sub.lower() not in src.lower():
            continue
        matched += 1
        cur = bool(p.flags & DISPLAYCONFIG_PATH_ACTIVE)
        if want_active and not cur:
            p.flags |= DISPLAYCONFIG_PATH_ACTIVE
            print(f"  [ACT] {src} | {model}: 启用")
        elif not want_active and cur:
            p.flags &= ~DISPLAYCONFIG_PATH_ACTIVE
            print(f"  [OFF] {src} | {model}: 禁用")
        else:
            state = "已是目标状态"
            print(f"  [---] {src} | {model}: {state}")

    if matched == 0:
        return f"未找到含 '{model_sub}' 的路径"

    # 精简并提交
    clean = _filter_paths(paths_list)
    ret, stage = _apply(clean)
    if ret != 0:
        return f"SetDisplayConfig({stage}) 失败，错误码 {ret} (0x{ret & 0xffffffff:x})"
    return f"OK：{'禁用' if not want_active else '启用'} {matched} 个匹配显示器路径"


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]
    if cmd == "list":
        for it in list_paths():
            print(f"  [{it['idx']}] {it['src']:<12} | {it['model']:<24} | {it['active']} | flags=0x{it['flags']:x}")
    elif cmd == "probe":
        if len(args) < 2:
            print("用法: probe <model子串>")
            return
        for it in list_paths():
            if args[1].lower() in it["model"].lower() or args[1].lower() in it["src"].lower():
                print(f"  匹配: [{it['idx']}] {it['src']} | {it['model']} | {it['active']}")
    elif cmd in ("off", "on"):
        if len(args) < 2:
            print(f"用法: {cmd} <model子串>")
            return
        want = (cmd == "on")
        # 该显示路径可能同时有 ACTIVE 和 inactive 两份(扩展/克隆拓扑)，都处理
        print(set_active(args[1], want))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
