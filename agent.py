#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PC Control Agent (Windows 端) - 增强版
监听 0.0.0.0:8765，被 fnOS 中转 Hub 转发调用。
  GET  /status           -> 状态
  POST /volume           -> 系统音量
  GET  /audio            -> 音频输出设备列表(供切换器)
  POST /audio/set        -> 切换默认音频输出设备
  POST /app              -> 启动应用
  POST /brightness       -> 逐屏亮度
  POST /input            -> 外显输入源(支持 VCP)
  POST /monitor/rename   -> 重命名显示器(持久化到 config.json)
  POST /monitor/scale    -> 缩放比例(SetDisplayConfig GPU 缩放)
  POST /monitor/tvswitch -> 电视盒软切换(断/连显示路径, 供接盒子的屏切电视/电脑)
  POST /vinput/mouse     -> 虚拟鼠标(绝对move/相对move/drag/click/scroll, 限定目标屏)
  POST /vinput/key       -> 虚拟键盘组合键/单键
  POST /vinput/text      -> 输入文本(KEYEVENTF_UNICODE, 支持中文/emoji)
  POST /vinput/pos       -> 读当前光标位置
  GET  /box/status       -> 电视盒子(Android)连接状态/分辨率/前台App
  POST /box/key          -> 盒子按键(keyevent: 方向/OK/返回/主页/媒体键等)
  POST /box/tap          -> 盒子触摸点按(归一化坐标或绝对像素)
  POST /box/swipe        -> 盒子滑动(滚动/翻页)
  POST /box/text         -> 盒子文本(ASCII=shell input text; 中文需盒子装 ADBKeyBoard)
依赖：pycaw + monitorcontrol + ToggleDisplay.exe(电视软切换) + PowerShell。
虚拟鼠标/键盘用纯 ctypes(user32.SendInput/SetCursorPos), 无额外依赖。
盒子遥控用 adb(默认 E:/platform-tools/adb.exe, 环境变量 PCBOX_ADB/PCBOX_SERIAL 可改)。
"""
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

DEFAULT_CONFIG = {
    "token": "qyx2026",
    "host": "0.0.0.0",
    "port": 8765,
    "volume_step": 6,
    "apps": {
        "Chrome": "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "Edge": "msedge",
        "文件资源管理器": "explorer",
        "终端": "wt",
        "网易云音乐": "C:/Program Files (x86)/Netease/CloudMusic/cloudmusic.exe",
        "Spotify": "spotify:",
    },
    # 信号源名 -> DDC/CI VCP 值（外显读不到 EDID 时兜底）
    "inputs": {"HDMI1": 17, "HDMI2": 18, "DP1": 15, "DP2": 16, "VGA": 1, "DVI1": 3},
    # 显示器型号 -> 额外输入源（DDC 只报告"激活的输入"，未激活的需要手动指定）
    "monitor_extra_inputs": {},
    # 显示器型号 -> 用户自定义名称（持久化，重启不丢）
    "monitor_labels": {},
    # 显示器 id(型号/internal) -> GDI device name（如 \\.\DISPLAY1），用于 SetDisplayConfig 缩放
    "monitor_devices": {},
    # 支持的缩放比例（百分比）
    "scale_options": [100, 125, 150, 175, 200],
}

_lock = threading.Lock()

try:
    import comtypes
    def _ensure_co():
        try: comtypes.CoInitialize()
        except Exception: pass
except Exception:
    def _ensure_co(): pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 过滤 _ 开头(说明字段),避免污染配置
        data = {k: v for k, v in data.items() if not k.startswith("_")}
        cfg.update(data)
    except Exception as e:
        print("[warn] 读 config.json 失败, 用默认:", e, file=sys.stderr)
    return cfg


def save_config():
    """把内存中的 CFG 写回 config.json（持久化）。"""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CFG, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print("[error] 写 config.json 失败:", e, file=sys.stderr)
        return False


CFG = load_config()
TOKEN = str(CFG.get("token", ""))
STEP = int(CFG.get("volume_step", 6))


# ---------------- 音量 (pycaw) ----------------
def _vol_api():
    _ensure_co()
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().EndpointVolume


def vol_get():
    try:
        return round(_vol_api().GetMasterVolumeLevelScalar() * 100)
    except Exception:
        return None


def vol_set(pct):
    pct = max(0, min(100, int(pct)))
    _vol_api().SetMasterVolumeLevelScalar(pct / 100.0, None)
    return vol_get()


def vol_step(delta):
    cur = vol_get()
    if cur is None: cur = 50
    return vol_set(cur + delta)


def vol_mute():
    v = _vol_api(); m = not v.GetMute(); v.SetMute(m, None); return m


# ---------------- 音频输出设备 (枚举 + 切默认) ----------------
# 枚举当前机器全部"活动"的播放端点, 并标出当前默认。切换默认走
# 非文档化接口 IPolicyConfig::SetDefaultEndpoint。
# CLSID/IID 参考 AudioSwitch/com-policy-config 等在 Win10/11 实测通过的组合:
#   CLSID_CPolicyConfigClient = {870af99c-171d-4f9e-af0d-e63df40c2bc9}
#   IID   (Win10/11 用)       = {f8679f50-850a-41cf-9c72-430f290290c8}
_AUDIO_POLICY = None  # 惰性构造的 IPolicyConfig COM 对象(进程级复用)

def _audio_list_devices():
    """返回活动播放端点列表: [{id, name, default}]。default 只一个为 True。"""
    from pycaw.pycaw import AudioUtilities
    _ensure_co()
    default_id = None
    try:
        default_id = AudioUtilities.GetSpeakers().id
    except Exception:
        pass
    out = []
    for d in AudioUtilities.GetAllDevices():
        sid = getattr(d, "id", "") or ""
        # 渲染端点 id 以 {0.0.0. 开头; 采集端点 {0.0.1. 开头(排除)
        if not sid.startswith("{0.0.0."):
            continue
        try:
            if d.state.value != 1:   # DEVICE_STATE_ACTIVE
                continue
        except Exception:
            if "Active" not in str(d.state):
                continue
        out.append({
            "id": sid,
            "name": str(getattr(d, "FriendlyName", "") or ""),
            "default": (sid == default_id),
        })
    return out


def _audio_policy():
    global _AUDIO_POLICY
    if _AUDIO_POLICY is None:
        _ensure_co()
        import comtypes.client as cc
        from comtypes import CLSCTX_INPROC_SERVER, COMMETHOD, GUID, HRESULT
        from ctypes import wintypes, c_void_p, c_ulong
        IID = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
        def _m(name, *ps):
            return COMMETHOD([], HRESULT, name, *ps)
        class _PolicyConfig(comtypes.IUnknown):
            _iid_ = IID
            _methods_ = [
                _m("GetMixFormat", (["in"], c_void_p, "a"), (["in"], c_void_p, "b")),
                _m("GetDeviceFormat", (["in"], c_void_p, "a"), (["in"], c_void_p, "b"), (["in"], c_void_p, "c")),
                _m("ResetDeviceFormat", (["in"], c_void_p, "a")),
                _m("SetDeviceFormat", (["in"], c_void_p, "a"), (["in"], c_void_p, "b"), (["in"], c_void_p, "c")),
                _m("GetProcessingPeriod", (["in"], c_void_p, "a"), (["in"], c_void_p, "b"), (["in"], c_void_p, "c"), (["in"], c_void_p, "d")),
                _m("SetProcessingPeriod", (["in"], c_void_p, "a"), (["in"], c_void_p, "b")),
                _m("GetShareMode", (["in"], c_void_p, "a"), (["in"], c_void_p, "b")),
                _m("SetShareMode", (["in"], c_void_p, "a"), (["in"], c_void_p, "b")),
                _m("GetPropertyValue", (["in"], c_void_p, "a"), (["in"], c_void_p, "b"), (["in"], c_void_p, "c"), (["in"], c_void_p, "d")),
                _m("SetPropertyValue", (["in"], c_void_p, "a"), (["in"], c_void_p, "b"), (["in"], c_void_p, "c"), (["in"], c_void_p, "d")),
                _m("SetDefaultEndpoint", (["in"], wintypes.LPCWSTR, "devid"), (["in"], c_ulong, "role")),
                _m("SetEndpointVisibility", (["in"], wintypes.LPCWSTR, "devid"), (["in"], wintypes.BOOL, "vis")),
            ]
        CLSID = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")
        _AUDIO_POLICY = cc.CreateObject(CLSID, interface=_PolicyConfig,
                                        clsctx=CLSCTX_INPROC_SERVER)
    return _AUDIO_POLICY


def audio_list():
    """GET /audio -> {devices:[...], default_id}（供前端切换器渲染）。"""
    return {"devices": _audio_list_devices()}


def audio_set(device_id):
    """把默认播放设备切到指定端点。返回 {ok, devices, default_id}。"""
    devices = _audio_list_devices()
    if not any(d["id"] == device_id for d in devices):
        raise ValueError("目标音频设备不在当前活动列表")
    policy = _audio_policy()
    hr = policy.SetDefaultEndpoint(device_id, 0)   # ERole.eConsole
    if hr != 0:
        raise RuntimeError("切换默认音频设备失败, HRESULT=0x%08X" % (hr & 0xFFFFFFFF))
    time.sleep(0.15)
    return {"ok": True, "devices": _audio_list_devices()}


# ---------------- 显示器枚举 ----------------
_enum_cache = {"ts": 0.0, "regs": None}

# 跨枚举持久化的"已知显示器"表：即使某次 DDC 读失败(显示器停在空输入/DDC 失效)，
# 仍能凭上次成功枚举拿到的句柄与输入表把它保留在列表里，从而允许软件"切回主输入"做恢复。
_KNOWN = {}   # model -> {"mon": monitorcontrol句柄, "inputs": {name: vcp}, "safe": [name,...]}
ENUM_CACHE_TTL = 12.0

# 磁盘持久化: 重启 agent 后 _KNOWN 清空会导致"死信"显示器(如停在空口的 Q27)彻底失联
# (报"未知显示器"、无法软件切回)。known_monitors.json 保存 inputs/safe, 重启后可继续恢复。
_KNOWN_FILE = os.path.join(HERE, "known_monitors.json")
_KNOWN_PERSIST = {}   # mid -> {"inputs": {name: vcp}, "safe": [name,...]}


def _valid_mid(mid):
    """型号串合法性检查: 显示器 DDC 半死态下 caps 可能返回嵌套乱码
    (如 'Q27(prot(monitor)type(LCD)model(...') —— 这类 mid 不能作为身份持久化。"""
    s = str(mid)
    return bool(s) and len(s) <= 24 and "(" not in s and ")" not in s


def _known_load():
    try:
        with open(_KNOWN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for mid, v in data.items():
            if mid == "internal" or not isinstance(v, dict) or not _valid_mid(mid):
                continue
            if re.match(r"^显示器\d+$", mid):
                continue   # 兜底名非稳定身份, 不加载(防幻影死信条目)
            inputs = {}
            for k, vv in (v.get("inputs") or {}).items():
                try:
                    inputs[str(k)] = int(vv)
                except Exception:
                    pass
            _KNOWN_PERSIST[mid] = {"inputs": inputs,
                                   "safe": [str(s) for s in (v.get("safe") or [])]}
    except Exception:
        pass


def _known_save():
    try:
        data = {}
        for mid, kn in _KNOWN.items():
            if mid == "internal" or not kn.get("inputs"):
                continue
            data[mid] = {"inputs": kn["inputs"], "safe": kn.get("safe") or []}
        for mid, v in _KNOWN_PERSIST.items():    # 保留历史已知, 不因一次失败丢失
            data.setdefault(mid, v)
        with open(_KNOWN_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


_known_load()


def _td_internal_enabled():
    """ToggleDisplay LIST 里内置屏(<internal>/CMN1629)的启用状态。
    True=启用(会被枚举) / False=禁用(不可能出现在枚举里) / None=未知。"""
    try:
        code, out = _td_run(["LIST"])
        if code != 0:
            return None
        for line in out.splitlines():
            low = line.lower()
            if "<internal>" in low or "cmn1629" in low:
                return "[disabled]" not in low
    except Exception:
        pass
    return None


def _enum_monitors():
    """返回显示器清单。分类策略：能成功建立 DDC(VCP) 通路的屏 = 外显；读不到 VCP 的可能是
    内置屏，也可能是"DDC 失效的已知外显"(如切到空口黑屏的 Q27: 写通读不通/半死态)。
    对后者用磁盘持久化的身份表把它认回来(保留句柄/输入表)，而不是误判成内置屏丢掉身份。
    不依赖 monitorcontrol 的返回顺序，也不依赖进程级 WMI 计数缓存。"""
    now = time.time()
    if _enum_cache["regs"] is not None and now - _enum_cache["ts"] < ENUM_CACHE_TTL:
        return _enum_cache["regs"]
    from monitorcontrol import get_monitors
    regs = []
    mons = list(get_monitors())
    ext_seq = 0
    caps_fail = []          # DDC 读不到 caps 的句柄: 待分派(内置屏 or 死信外显)
    for mon in mons:
        caps = None
        try:
            with mon as m:
                caps = m.get_vcp_capabilities()
        except Exception:
            caps = None
        if caps is None:
            caps_fail.append((mon, None))
            continue
        model = caps.get("model")
        if not (model and _valid_mid(model)):
            # DDC 通但型号乱码/空(如关屏后重握手期间的半恢复态) -> 待用持久化身份认回,
            # 避免命名成"显示器1"这种幻影。带 caps 以便认回时优先用实时输入表。
            caps_fail.append((mon, caps))
            continue
        # DDC 通且型号有效 -> 正常外显
        ext_seq += 1
        mid = str(model)
        info = {"mon": mon, "kind": "external",
                "id": mid, "label": _label_for(mid),
                "inputs": {}, "supported_input": False,
                "device": _device_for(mid)}
        ins = {}
        for s in caps.get("inputs", []):
            ins[str(s.name)] = int(s.value)
        if ins:
            info["inputs"] = ins
            info["supported_input"] = True
        # DDC 报告为主；仅当 DDC 读不到输入源时才用 config 兜底
        merged = dict(info["inputs"])
        if not merged:
            fallback = {k: int(v) for k, v in CFG.get("inputs", {}).items()}
            merged.update(fallback)
        extra = (CFG.get("monitor_extra_inputs") or {}).get(info["id"], {})
        for k, v in extra.items(): merged[k] = int(v)
        if merged:
            info["inputs"] = merged
            info["supported_input"] = True
        # 持久化已知外显(句柄+输入表)，供 DDC 失效时做"切回主输入"恢复。
        _KNOWN[mid] = {"mon": mon, "inputs": dict(info["inputs"]),
                       "safe": list(_allowed_inputs(mid, info["inputs"]).keys())}
        _KNOWN_PERSIST[mid] = {"inputs": dict(info["inputs"]),
                               "safe": list(_KNOWN[mid]["safe"])}
        regs.append(info)
    # 分派 caps 失败的句柄:
    # 内置屏路径被禁用(桌面机常态)时, 枚举到的 caps-fail 屏不可能是内置屏 -> 必是 DDC 失效的已知外显。
    ext_ids = {r["id"] for r in regs if r["kind"] == "external"}
    missing_known = [mid for mid in _KNOWN_PERSIST
                     if mid != "internal" and mid not in ext_ids]
    int_enabled = _td_internal_enabled()
    for mon, caps in caps_fail:
        if int_enabled is not True and missing_known:
            # 死信/半恢复外显: 认回身份, 保留句柄供恢复(DDC 半死态下写常通)。
            # 若实时 caps 里有输入表则优先用, 否则用持久化表。
            mid = missing_known.pop(0)
            persist = _KNOWN_PERSIST[mid]
            inputs = dict(persist["inputs"])
            if caps is not None:
                try:
                    ins = {str(s.name): int(s.value) for s in caps.get("inputs", [])}
                    if ins:
                        inputs = ins
                except Exception:
                    pass
            regs.append({"mon": mon, "kind": "external", "id": mid,
                         "label": _label_for(mid),
                         "inputs": inputs,
                         "supported_input": bool(inputs),
                         "device": _device_for(mid), "_ddc_dead": True})
            _KNOWN[mid] = {"mon": mon, "inputs": inputs,
                           "safe": list(persist.get("safe") or [])}
        else:
            # 内置屏(或不支持 DDC/无法识别的屏)
            regs.append({"mon": mon, "kind": "internal", "id": "internal",
                         "label": _label_for("internal"), "inputs": {},
                         "supported_input": False, "device": _device_for("internal")})
    _known_save()
    regs.sort(key=lambda x: (0 if x["kind"] == "internal" else 1, x["id"]))
    _enum_cache["ts"] = time.time()
    _enum_cache["regs"] = regs
    return regs


def _label_for(mid):
    """从 config 读用户自定义名称。"""
    labels = CFG.get("monitor_labels") or {}
    if mid in labels and labels[mid]:
        return str(labels[mid])
    if mid == "internal":
        return "内置显示器"
    return mid


def _device_for(mid):
    """从 config 读该显示器对应的 GDI device name。"""
    devices = CFG.get("monitor_devices") or {}
    return devices.get(mid)


def _allowed_inputs(mid, inputs):
    """按 config 的 monitor_input_allow 白名单裁剪可选输入源。

    背景：一台屏通过 DDC 的 capability 会列出其所有物理输入口(如 DP1/HDMI1)，
    但其中可能有空口(没接信号线)。切到空口 -> 显示器失去信号而黑屏，且因停在
    无源输入上 DDC 失效、无法用软件切回(只能按机身 OSD)。Q27/XV320 都踩过。
    白名单只声明"实际接了设备、允许切换"的口；未配置白名单的屏默认仅允许
    保留 capability 中的 DP 类输入，杜绝切到空 HDMI。
    """
    allow = (CFG.get("monitor_input_allow") or {}).get(mid)
    if allow is not None:                       # 显式配置: 空列表=禁一切切换
        if not allow:
            return {}
        return {k: v for k, v in inputs.items() if k in allow}
    # 兜底启发式: 只暴露 DP/DisplayPort 类输入(本机显卡走 DP 时最稳)，过滤 HDMI/DVI 空口
    return {k: v for k, v in inputs.items()
            if k.upper().startswith("DP") or "DISPLAYPORT" in k.upper()}


def _find_monitor(mid):
    for info in _enum_monitors():
        if info["id"] == mid:
            return info
    return None


# ---------------- DDC 冷却与重试 ----------------
DDC_COOLDOWN = {}
DDC_COOLDOWN_S = 12
DDC_TRY_SLEEP = 0.6
# 单次 DDC I2C 操作的硬超时(秒)。Q27 停在空口/半死态时"写通读挂", 读操作会一直等 I2C
# 超时(数十秒), 若不加硬超时, 后台刷新线程会长期卡死, /status 无法秒回 -> hub 转发超时 -> 前端误判"Windows 离线"。
DDC_OP_TIMEOUT = 5.0


def _in_cooldown(mid):
    return mid in DDC_COOLDOWN and time.time() < DDC_COOLDOWN[mid]


def _enter_cooldown(mid):
    DDC_COOLDOWN[mid] = time.time() + DDC_COOLDOWN_S


def _run_ddc(mon, fn, tries=3, mid=None):
    if mid is not None and _in_cooldown(mid):
        raise RuntimeError("冷却中(显示器 DDC 刚失败)")
    last = None
    for i in range(tries):
        # 用守护线程 + join(硬超时) 包住 DDC 调用: monitorcontrol 的 I2C 读在半死态下会永久阻塞,
        # 超时后放弃该次调用(线程留在后台, daemon 不阻止退出), 避免拖死调用方。
        box = {}
        def _call():
            try:
                with mon as m:
                    box["r"] = fn(m)
            except Exception as e:
                box["e"] = e
        t = threading.Thread(target=_call, daemon=True)
        t.start()
        t.join(DDC_OP_TIMEOUT)
        if t.is_alive():
            last = TimeoutError("DDC 操作超时(%.0fs)" % DDC_OP_TIMEOUT)
            continue                       # 已等满超时, 不再额外 sleep
        if "e" in box:
            last = box["e"]
            time.sleep(DDC_TRY_SLEEP * (i + 1))
            continue
        return box["r"]
    if mid is not None:
        _enter_cooldown(mid)
    raise last


# ---------------- 亮度 ----------------
def _wmi_brightness_get():
    try:
        ps = "(Get-WmiObject -Namespace root\\wmi -Class WmiMonitorBrightness).CurrentBrightness"
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.isdigit(): return int(line)
    except Exception:
        pass
    return None


def _wmi_brightness_set(v):
    try:
        v = max(0, min(100, int(v)))
        ps = ("Get-WmiObject -Namespace root\\wmi -Class WmiMonitorBrightnessMethods | "
              "ForEach-Object { $_.WmiSetBrightness(1, %d) }" % v)
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0: return True
    except Exception:
        pass
    raise RuntimeError("内置屏亮度设置失败")


def monitor_brightness_get(info):
    if info["kind"] == "internal":
        return _wmi_brightness_get()
    if _in_cooldown(info["id"]):
        return None
    try:
        return _run_ddc(info["mon"], lambda m: m.get_luminance(), tries=2, mid=info["id"])
    except Exception:
        return None


def monitor_brightness_set(mid, value):
    value = max(0, min(100, int(value)))
    if mid in (None, "all", ""):
        for info in _enum_monitors():
            _set_one_brightness(info, value)
        return True
    info = _find_monitor(mid)
    if not info: raise ValueError("未知显示器: " + str(mid))
    _set_one_brightness(info, value)
    return True


def _set_one_brightness(info, value):
    if info["kind"] == "internal":
        _wmi_brightness_set(value)
    else:
        _run_ddc(info["mon"], lambda m: m.set_luminance(value), tries=3, mid=info["id"])
        DDC_COOLDOWN.pop(info["id"], None)


# ---------------- 输入源 ----------------
def _ccd_reenable(mid):
    """重建(重新启用)该显示器的 Windows 显示路径，保持原路径顺序(不动主显)。
    用于显示器停在空输入/无信号导致被 Windows 剔除枚举时，先把它"拉回"可控状态，
    随后即可用 DDC 切回安全输入。返回结果字符串。"""
    try:
        import ccd_toggle as C
    except Exception as e:
        return "ccd 模块不可用: " + str(e)
    try:
        paths, n = C._query_paths(C.QDC_ALL_PATHS)
        if paths is None or n == 0:
            return "无显示路径"
        paths_list = [paths[i] for i in range(n)]
        matched = 0
        for p in paths_list:
            src, model = C._path_desc(p)
            if mid.lower() not in model.lower() and mid.lower() not in src.lower():
                continue
            matched += 1
            if not (p.flags & C.DISPLAYCONFIG_PATH_ACTIVE):
                p.flags |= C.DISPLAYCONFIG_PATH_ACTIVE
        if matched == 0:
            return "未找到含 '%s' 的路径" % mid
        clean = C._filter_paths(paths_list)
        for p in clean:
            p.sourceInfo.modeInfoIdx = C.DISPLAYCONFIG_PATH_MODE_IDX_INVALID
            p.targetInfo.modeInfoIdx = C.DISPLAYCONFIG_PATH_MODE_IDX_INVALID
        arr = (C.PATH_INFO * max(len(clean), 1))()
        for j, p in enumerate(clean):
            arr[j] = p
        base = C.SDC_TOPOLOGY_SUPPLIED   # 不含 ORDER_CHANGES，保持原顺序/主显
        ret = C.user32.SetDisplayConfig(len(clean), arr, 0, None, C.SDC_VALIDATE | base)
        if ret != 0:
            return "SetDisplayConfig(validate) 失败 0x%x" % (ret & 0xffffffff)
        ret = C.user32.SetDisplayConfig(len(clean), arr, 0, None, C.SDC_APPLY | base)
        if ret != 0:
            return "SetDisplayConfig(apply) 失败 0x%x" % (ret & 0xffffffff)
        return "OK 已重建显示路径"
    except Exception as e:
        return "ccd 异常: " + str(e)


def monitor_input_recover(mid):
    """把显示器切回其第一个安全(已接线)输入。实测验证的恢复套路(Q27 停在空口 HDMI1 黑屏、
    DDC 半死态: capabilities 读失败但 set_input_source 写得通)：
      1) 直接 DDC 写(半死态常写通)；
      2) 失败则 ToggleDisplay 断->连显示路径，强迫 DP 重新握手唤醒显示器 DDC，再 DDC 写。
    身份/输入表来自 _KNOWN(内存) 或 _KNOWN_PERSIST(磁盘, agent 重启不丢)。返回 (vcp, method, target)。"""
    kn = _KNOWN.get(mid) or {}
    persist = _KNOWN_PERSIST.get(mid) or {}
    info = _find_monitor(mid)
    if info and info.get("inputs"):
        inputs = dict(info["inputs"])
    else:
        inputs = dict(kn.get("inputs") or persist.get("inputs") or {})
    if not inputs:
        raise ValueError("未知显示器或无输入表: " + str(mid))
    safe_list = kn.get("safe") or persist.get("safe") or []
    target = None
    for s in safe_list:
        if s in inputs:
            target = s; break
    if target is None:
        safe_map = _allowed_inputs(mid, inputs)
        if safe_map:
            target = list(safe_map.keys())[0]
    if not target or target not in inputs:
        raise ValueError("该显示器没有已知安全输入，无法自动恢复(请用机身 OSD 切回)")
    vcp = int(inputs[target])

    # 1) 直接 DDC 写(半死态: 写常通, 即使 caps/读失败)
    DDC_COOLDOWN.pop(mid, None)
    mon = (info or {}).get("mon") or kn.get("mon")
    if mon is not None:
        try:
            _run_ddc(mon, lambda m: m.set_input_source(vcp), tries=2, mid=mid)
            DDC_COOLDOWN.pop(mid, None)
            return vcp, "ddc", target
        except Exception:
            pass

    # 2) 唤醒循环: ToggleDisplay 断->连路径, 强迫 DP 重新握手唤醒 DDC(XV320 软切换同款机制)
    try:
        st = _td_status(mid)
        if st is not True:
            _td_run(["ENABLE", mid])       # 确保从启用态开始
            time.sleep(1.0)
        _td_run(["DISABLE", mid])          # 断 DP 信号
        time.sleep(2.0)
        _td_run(["ENABLE", mid])            # 重连 -> DP 重新握手, 唤醒显示器 DDC
        time.sleep(3.0)
    except Exception:
        pass
    _enum_cache["ts"] = 0.0
    _enum_cache["regs"] = None

    # 3) 重枚举后再 DDC 写
    info2 = _find_monitor(mid)
    mon2 = (info2 or {}).get("mon") or (kn.get("mon"))
    if mon2 is not None:
        DDC_COOLDOWN.pop(mid, None)
        _run_ddc(mon2, lambda m: m.set_input_source(vcp), tries=3, mid=mid)
        DDC_COOLDOWN.pop(mid, None)
        return vcp, "toggle+ddc", target
    raise ValueError("唤醒后仍无法定位 %s，请按机身 OSD 切回 %s" % (mid, target))


def _ccd_set_active(mid, want_active):
    """启用/禁用匹配显示器的 Windows 显示路径(CCD)。禁用=Windows 停止向该显示器发信号
    (相当于关屏, 但显示器仍停在 DP1, DDC 始终活着, 故可 100% 软件恢复)。
    用 SDC_TOPOLOGY_SUPPLIED(不含 ORDER_CHANGES) 以保持其余屏顺序/主显。返回 (ok, msg)。"""
    try:
        import ccd_toggle as C
    except Exception as e:
        return False, "ccd 模块不可用: " + str(e)
    try:
        paths, n = C._query_paths(C.QDC_ALL_PATHS)
        if paths is None or n == 0:
            return False, "无显示路径"
        pl = [paths[i] for i in range(n)]
        matched = 0
        for p in pl:
            src, model = C._path_desc(p)
            if mid.lower() not in model.lower() and mid.lower() not in src.lower():
                continue
            matched += 1
            if want_active:
                p.flags |= C.DISPLAYCONFIG_PATH_ACTIVE
            else:
                p.flags &= ~C.DISPLAYCONFIG_PATH_ACTIVE
        if matched == 0:
            return False, "未找到含 '%s' 的路径" % mid
        clean = C._filter_paths(pl)
        for p in clean:
            p.sourceInfo.modeInfoIdx = C.DISPLAYCONFIG_PATH_MODE_IDX_INVALID
            p.targetInfo.modeInfoIdx = C.DISPLAYCONFIG_PATH_MODE_IDX_INVALID
        arr = (C.PATH_INFO * max(len(clean), 1))()
        for j, p in enumerate(clean):
            arr[j] = p
        base = C.SDC_TOPOLOGY_SUPPLIED   # 不含 ORDER_CHANGES, 保持顺序/主显
        ret = C.user32.SetDisplayConfig(len(clean), arr, 0, None, C.SDC_VALIDATE | base)
        if ret != 0:
            return False, "SetDisplayConfig(validate) 失败 0x%x" % (ret & 0xffffffff)
        ret = C.user32.SetDisplayConfig(len(clean), arr, 0, None, C.SDC_APPLY | base)
        if ret != 0:
            return False, "SetDisplayConfig(apply) 失败 0x%x" % (ret & 0xffffffff)
        return True, ("已启用" if want_active else "已禁用") + " %d 个匹配路径" % matched
    except Exception as e:
        return False, "ccd 异常: " + str(e)


def monitor_blank(mid):
    """可靠关屏: ToggleDisplay 禁用该显示器的 Windows 路径(显示器停在 DP1, DDC 活着,
    可 100% 软件恢复)。ToggleDisplay 是项目内 XV320 软切换验证过的成熟机制, 优先用之。"""
    try:
        code, out = _td_run(["DISABLE", mid])
        ok_td = (code == 0)
    except Exception as e:
        ok_td = False
        out = str(e)
    if not ok_td:
        ok, msg = _ccd_set_active(mid, False)   # 回退 CCD
        if not ok:
            raise ValueError("关屏失败: %s / %s" % (out.strip() if out else "", msg))
    _enum_cache["ts"] = 0.0
    _enum_cache["regs"] = None
    return "已关屏(路径已禁用, 点「亮屏」恢复)"


def monitor_unblank(mid):
    """可靠亮屏: ToggleDisplay 重启用显示路径 + 确保输入在安全源(DP1)。"""
    try:
        code, out = _td_run(["ENABLE", mid])
        ok_td = (code == 0)
    except Exception as e:
        ok_td = False
        out = str(e)
    if not ok_td:
        ok, msg = _ccd_set_active(mid, True)    # 回退 CCD
        if not ok:
            raise ValueError("亮屏失败: %s / %s" % (out.strip() if out else "", msg))
    time.sleep(2.0)
    _enum_cache["ts"] = 0.0
    _enum_cache["regs"] = None
    # 路径重启用后, 确保显示器醒来且输入在安全源(DP1)上:
    # 1) VCP 0xD6 软开(待机中的屏立即唤醒)  2) 输入切回安全源(防漂移)
    try:
        info = _find_monitor(mid)
        if info:
            DDC_COOLDOWN.pop(info["id"], None)
            def _wake(m):
                try: m.set_power_mode("on")
                except Exception: pass
            if info.get("inputs"):
                safe_map = _allowed_inputs(info["id"], info["inputs"])
                if safe_map:
                    vcp = list(safe_map.values())[0]
                    _run_ddc(info["mon"],
                             lambda m: (_wake(m), m.set_input_source(vcp)),
                             tries=2, mid=info["id"])
                else:
                    _run_ddc(info["mon"], _wake, tries=2, mid=info["id"])
            else:
                _run_ddc(info["mon"], _wake, tries=2, mid=info["id"])
            DDC_COOLDOWN.pop(info["id"], None)
    except Exception:
        pass
    return "已亮屏"


def _ensure_path_on(mid):
    """确保 Windows 显示路径启用(等效 Win11"扩展桌面到此显示器")。幂等。
    关屏后路径被禁用, 此时点"DP1"等安全输入应自动恢复路径, 否则仅 DDC 切输入源不会亮屏。"""
    try:
        code, _ = _td_run(["ENABLE", mid])
        if code == 0:
            return True
    except Exception:
        pass
    try:
        ok, _ = _ccd_set_active(mid, True)
        return ok
    except Exception:
        return False


def monitor_input_set(mid, src=None, vcp=None, force=False):
    info = _find_monitor(mid)
    if not info:
        # 显示器可能停在空输入/DDC 失效而被实时枚举剔除 -> 用持久化身份尝试恢复
        kn = _KNOWN.get(mid)
        if kn:
            info = {"mon": kn["mon"], "id": mid, "kind": "external",
                    "inputs": kn["inputs"]}
        else:
            raise ValueError("未知显示器: " + str(mid))
    if info["kind"] != "external":
        raise ValueError("内置屏不支持切换信号源")
    if vcp is None:
        if src is None: raise ValueError("需提供 src 或 vcp")
        if str(src) in info["inputs"]:
            vcp = info["inputs"][str(src)]
        else:
            try: vcp = int(src)
            except Exception: raise ValueError("未知信号源: " + str(src))
    vcp = int(vcp)
    # 用户明确要求: 任何客户端(手机/原前端/面板)点击即直接切换, 不再二次确认/force 门槛。
    # 切到未接线空口(如 HDMI1)即黑屏(关屏), 软件切回由 /input/recover 与 CCD 兜底保证。
    safe = _allowed_inputs(info["id"], info["inputs"])
    safe_vcps = set(safe.values())
    # 切到"安全输入"(已接线, 如 DP1)时, 先确保 Windows 显示路径启用(等效 Win11 扩展桌面)。
    # 否则若该屏被"关屏"断开了路径, 仅 DDC 切输入源不会亮屏。
    if safe and vcp in safe_vcps:
        _ensure_path_on(info["id"])
        _enum_cache["ts"] = 0.0
        _enum_cache["regs"] = None
        time.sleep(1.0)
        info2 = _find_monitor(info["id"])
        if info2:
            info = info2
    # 执行切换；切回"安全输入"(从空口黑屏救回 DP1)时若 DDC 失败，CCD 重建路径兜底。
    # 无论 DDC 成功与否, 切到安全输入后都确保 Windows 显示路径是启用的(切空口可能已禁用路径)。
    try:
        _run_ddc(info["mon"], lambda m: m.set_input_source(vcp), tries=3, mid=info["id"])
    except Exception:
        if safe and vcp in safe_vcps:
            DDC_COOLDOWN.pop(info["id"], None)
            try: _ccd_reenable(info["id"])
            except Exception: pass
            time.sleep(2.0)
            info2 = _find_monitor(info["id"])
            if info2:
                _run_ddc(info2["mon"], lambda m: m.set_input_source(vcp), tries=3, mid=info["id"])
                DDC_COOLDOWN.pop(info["id"], None)
                try: _ccd_reenable(info["id"])
                except Exception: pass
                return vcp
        raise
    DDC_COOLDOWN.pop(info["id"], None)
    return vcp


def monitor_input_get(info):
    if info["kind"] != "external":
        return None
    if _in_cooldown(info["id"]):
        return None
    try:
        s = _run_ddc(info["mon"], lambda m: m.get_input_source(), tries=1, mid=info["id"])
        return int(s) if s is not None else None
    except Exception:
        return None


# ---------------- 主显示器(primary)设置: CCD 路径排序, 首位=主显 ----------------
def _primary_model():
    """返回当前主显示器(路径数组里第一个 active 路径)的身份标识; 失败返回 None。
    外显返回 EDID 型号; 内置屏(无 EDID 型号, outputTechnology=INTERNAL)返回 "internal"。"""
    try:
        import ccd_toggle as C
        paths, n = C._query_paths(C.QDC_ONLY_ACTIVE_PATHS)
        if paths is None:
            return None
        for i in range(n):
            if paths[i].flags & C.DISPLAYCONFIG_PATH_ACTIVE:
                if paths[i].targetInfo.outputTechnology == C.DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INTERNAL:
                    return "internal"
                _src, model = C._path_desc(paths[i])
                if model:
                    return model
    except Exception:
        return None
    return None


def _ccd_internal_screen():
    """用 CCD 全量路径发现内置屏(笔记本面板)。内置屏通常无 EDID friendly name(model 空)、
    且常被禁用(不出现在 DDC 枚举里)，只能靠 outputTechnology==INTERNAL 识别。
    返回 {id:'internal', label, device, enabled, pnp} 或 None。"""
    import ccd_toggle as C
    try:
        paths, n = C._query_paths(C.QDC_ALL_PATHS)
        if paths is None or n == 0:
            return None
        pnp = None
        enabled = False
        seen_src = set()
        for i in range(n):
            p = paths[i]
            if p.targetInfo.outputTechnology != C.DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INTERNAL:
                continue
            dp_pnp = C._path_pnp(p)
            if dp_pnp:
                pnp = dp_pnp
            if p.flags & C.DISPLAYCONFIG_PATH_ACTIVE:
                enabled = True
            src, _m = C._path_desc(p)
            if src and src not in seen_src:
                seen_src.add(src)
        if pnp is None:
            return None
        cfg_dev = (CFG.get("monitor_devices") or {}).get("internal")
        device = cfg_dev or (sorted(seen_src)[0] if seen_src else None)
        return {"id": "internal", "label": _label_for("internal"),
                "device": device, "enabled": enabled, "pnp": pnp}
    except Exception:
        return None


def _apply_primary(paths_list):
    """双阶段提交: 把目标路径排到首位即设为主显。严禁加 SDC_ALLOW_PATH_ORDER_CHANGES
    (那会忽略顺序, 导致主显不变)。mode 索引置 INVALID 让系统自动取模式; statusFlags 清零
    (SetDisplayConfig 会重填)。值拷贝构造, 不污染传入的路径。

    提交策略(实测):
    1) 首选 SDC_TOPOLOGY_SUPPLIED —— 从持久化数据库取 mode(保留原分辨率);
    2) 失败(如首次把某屏设为主显, 数据库无该拓扑条目 -> 0x1f)则回退
       SDC_USE_SUPPLIED_DISPLAY_CONFIG | SDC_ALLOW_CHANGES —— 让系统 best mode logic 补模式。
    """
    import ccd_toggle as C
    n = len(paths_list)
    arr = (C.PATH_INFO * max(n, 1))()
    for j, p in enumerate(paths_list):
        arr[j].sourceInfo = p.sourceInfo
        arr[j].targetInfo = p.targetInfo
        arr[j].flags = p.flags
        arr[j].sourceInfo.modeInfoIdx = C.DISPLAYCONFIG_PATH_MODE_IDX_INVALID
        arr[j].targetInfo.modeInfoIdx = C.DISPLAYCONFIG_PATH_MODE_IDX_INVALID
        arr[j].sourceInfo.statusFlags = 0
        arr[j].targetInfo.statusFlags = 0
    last = 0
    for base in (C.SDC_TOPOLOGY_SUPPLIED,
                 C.SDC_USE_SUPPLIED_DISPLAY_CONFIG | C.SDC_ALLOW_CHANGES):
        ret = C.user32.SetDisplayConfig(n, arr, 0, None, C.SDC_VALIDATE | base)
        if ret != 0:
            last = ret
            continue
        ret = C.user32.SetDisplayConfig(n, arr, 0, None, C.SDC_APPLY | base)
        if ret == 0:
            return 0, "apply"
        last = ret
    return last, "apply"


def monitor_set_primary(model):
    """把指定显示器设为主显示器(任务栏/开始菜单所在屏)。支持三台屏：
    Q27G40ZE / XV320QU LM(外显, 按 EDID 型号匹配) / internal(内置屏, 按 INTERNAL 技术匹配)。

    关键经验(实测):
    - 设外显为主: 用 QDC_ONLY_ACTIVE_PATHS 且**排除内置屏路径**(顺带禁用内置屏回到两屏),
      否则内置屏已启用时 INVALID mode 无法重建 -> SetDisplayConfig 0x1f 失败。
    - 设内置屏为主: 用 QDC_ALL_PATHS 启用一条未占用的 INTERNAL 路径, _filter_paths 精简后
      排到首位 -> 成功(此时三屏: 内置屏+两外显都 active)。
    两者统一: mode 索引置 INVALID + SDC_TOPOLOGY_SUPPLIED(不含 ORDER_CHANGES)。
    """
    import ccd_toggle as C
    is_internal = (model == "internal")

    if is_internal:
        paths, n = C._query_paths(C.QDC_ALL_PATHS)
        if paths is None or n == 0:
            raise RuntimeError("无显示路径")
        paths_list = [paths[i] for i in range(n)]
        internal_paths = [p for p in paths_list
                          if p.targetInfo.outputTechnology == C.DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INTERNAL]
        if not internal_paths:
            raise ValueError("未找到内置屏")
        # 启用一条"未被 active 路径占用 source"的 INTERNAL 路径(内置屏多条 source 可能与外显共用显卡)
        occupied = set()
        for p in paths_list:
            if p.flags & C.DISPLAYCONFIG_PATH_ACTIVE:
                occupied.add((p.sourceInfo.adapterId.LowPart, p.sourceInfo.id))
        chosen = None
        for p in internal_paths:
            if p.flags & C.DISPLAYCONFIG_PATH_ACTIVE:
                chosen = p
                break
        if chosen is None:
            for p in internal_paths:
                if (p.sourceInfo.adapterId.LowPart, p.sourceInfo.id) not in occupied:
                    p.flags |= C.DISPLAYCONFIG_PATH_ACTIVE
                    chosen = p
                    break
        if chosen is None:
            raise ValueError("内置屏所有 source 均被占用, 无法启用(请先断开一台外显)")
        clean = C._filter_paths(paths_list)
        clean.sort(key=lambda p: 0 if p.targetInfo.outputTechnology == C.DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INTERNAL else 1)
    else:
        paths, n = C._query_paths(C.QDC_ONLY_ACTIVE_PATHS)
        if paths is None or n == 0:
            raise RuntimeError("无显示路径")
        paths_list = [paths[i] for i in range(n)]

        def _is_target(p):
            src, m = C._path_desc(p)
            return model.lower() in m.lower() or model.lower() in src.lower()

        if not any(_is_target(p) for p in paths_list):
            raise ValueError("未找到显示器: " + str(model))
        # 只提交 active 外显(排除内置屏), 目标排首位
        clean = [p for p in paths_list
                 if p.targetInfo.outputTechnology != C.DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INTERNAL]
        clean.sort(key=lambda p: 0 if _is_target(p) else 1)

    ret, stage = _apply_primary(clean)
    if ret != 0:
        raise RuntimeError("SetDisplayConfig(%s) 失败 0x%x(可能该布局非法, 已取消)"
                           % (stage, ret & 0xffffffff))
    return True


# ---------------- 电视软切换(断/连显示路径,绕开 DDC 对 HDMI2 的限制) ----------------
TOGGLE_DISPLAY_EXE = os.path.join(HERE, "ToggleDisplay.exe")


def _td_run(args):
    """运行 ToggleDisplay.exe，返回 (returncode, stdout)。"""
    try:
        r = subprocess.run(
            [TOGGLE_DISPLAY_EXE] + args,
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        raise RuntimeError("ToggleDisplay.exe 不存在于 %s（请从 tiny-tools-collection 下载放置）" % HERE)
    except Exception as e:
        raise RuntimeError("ToggleDisplay.exe 执行失败: %s" % e)


def _td_status(target):
    """查询 ToggleDisplay LIST，返回 True=enabled / False=disabled / None=未找到。"""
    code, out = _td_run(["LIST"])
    for line in out.splitlines():
        if target.lower() in line.lower():
            return "[disabled]" not in line
    return None


def _soft_state(mid):
    """判定电视盒屏当前状态: 'box'(盒子) / 'pc'(电脑) / 'unknown'。
    优先 ToggleDisplay(即时, 不受 Windows 路径重建延迟影响); 找不到时按是否被 Windows 枚举兜底。"""
    try:
        st = _td_status(mid)
        if st is not None:
            return "box" if st is False else "pc"
    except Exception:
        pass
    # 兜底: ToggleDisplay 未跟踪到 -> 按当前是否被系统枚举判断
    try:
        info = _find_monitor(mid)
        return "pc" if info is not None else "box"
    except Exception:
        return "unknown"


def monitor_tv_switch(mid, target="pc"):
    """
    电视盒软切换：对接了盒子的显示器(mid)，通过 Windows 侧断/连其显示路径 + DDC 辅助，
    在 HDMI1(电脑) 与 HDMI2(盒子) 间切换，绕开 DDC 固件对 HDMI2 切换值(18)不支持的限制。

    原理：
      target="box" 切到盒子：
        1) 先确保 Windows 路径已启用(在电脑态)；若在盒子态，需先 ENABLE 让 Windows 接管；
        2) DISABLE 该显示路径 -> HDMI1 停止输出 -> 显示器 auto-source 跳到 HDMI2 盒子。
      target="pc"  切回电脑：
        1) ENABLE Windows 显示路径(HDMI1 开始输出)；
        2) 但显示器此时仍停在 HDMI2(盒子有信号, auto-source 不会主动跳回)，
           需再 DDC 写 17(HDMI1) 强制显示器切回 HDMI1 输入 -> 显示电脑。
    返回 {"ok", "monitor", "state", "target"}。
    """
    soft_list = CFG.get("tv_softswitch_monitors") or []
    if mid not in soft_list:
        raise ValueError("显示器 %s 未在 tv_softswitch_monitors 中配置为电视盒屏" % mid)
    if target not in ("box", "pc"):
        raise ValueError("target 只能是 box 或 pc")

    info = _find_monitor(mid)
    model = info["id"] if info else mid          # XV320QU LM

    def _enable_path():
        c = _td_status(model)
        if c is False:
            code, out = _td_run(["ENABLE", model])
            if code != 0:
                raise RuntimeError("启用显示路径失败: %s" % out.strip())
        time.sleep(1.5)   # 等 Windows 建立路径

    def _disable_path():
        c = _td_status(model)
        if c is True:
            code, out = _td_run(["DISABLE", model])
            if code != 0:
                raise RuntimeError("禁用显示路径失败: %s" % out.strip())
        time.sleep(1.5)   # 等 HDMI1 信号消失触发 auto-source

    def _ddc_to_pc():
        # 用 DDC 写 17(HDMI1) 强制显示器切到电脑输入（从盒子切回的有效手段）。
        # 注意：显示器被 DISABLE 过，缓存的 mon 对象已失效，必须重新枚举拿新连接。
        try:
            _enum_cache["ts"] = 0.0
            _enum_cache["regs"] = None
            time.sleep(2.0)          # 等显示器完成 EDID/DDC 重握手
            fresh = _find_monitor(mid)
            if not fresh or fresh.get("mon") is None:
                return
            _run_ddc(fresh["mon"], lambda m: m.set_input_source(17), tries=4, mid=mid)
            DDC_COOLDOWN.pop(mid, None)
        except Exception:
            pass

    if target == "box":
        _enable_path()      # 确保先回电脑态(Windows 有信号)
        _disable_path()     # 断开路径 -> 跳盒子
        state = "box"
    else:  # pc
        _enable_path()      # 让 HDMI1 有信号
        _ddc_to_pc()        # 显示器切回 HDMI1 输入
        state = "pc"

    # 刷新显示器缓存,让 status 反映最新拓扑
    _enum_cache["ts"] = 0.0
    _enum_cache["regs"] = None
    return {"ok": True, "monitor": mid, "state": state, "target": target}


# ---------------- 显示器重命名 ----------------
def monitor_rename(mid, label):
    """持久化：把显示器 mid 的自定义名称写到 config.json。"""
    if not mid: raise ValueError("缺少 monitor 参数")
    label = str(label or "").strip()
    if len(label) > 32:
        raise ValueError("名称太长(最长 32 字符)")
    labels = CFG.setdefault("monitor_labels", {})
    if not label:
        labels.pop(mid, None)
    else:
        labels[mid] = label
    save_config()
    # 立即刷新缓存里该显示器的 label
    obj = _status_cache.get("obj")
    if obj:
        for m in obj.get("monitors", []):
            if m.get("id") == mid:
                m["label"] = label or (mid if mid != "internal" else "内置显示器")
                break
    return label or (mid if mid != "internal" else "内置显示器")


# ---------------- 缩放比例 (DisplayConfigSetDeviceInfo + SOURCE_DPI_SCALE) ----------------
# Windows 每显示器 DPI 缩放的官方 API：DisplayConfigGet/SetDeviceInfo 配合
# DISPLAYCONFIG_DEVICE_INFO_GET/SET_SOURCE_DPI_SCALE (type=-3 / -4)。
# scaleRel 是「相对推荐缩放」的偏移量，单位 25%（一个缩放档），0=推荐，+1=+25%。
# 该 API 立即生效，正是「设置→显示」里改缩放的底层机制。
import ctypes
from ctypes import c_uint16, c_uint32, c_int, byref, sizeof, Structure, c_wchar, POINTER, wintypes

_user32 = ctypes.windll.user32
_shcore = ctypes.windll.shcore

# 设 per-monitor DPI aware，使 GetDpiForMonitor 返回真实 per-monitor DPI
# （否则对非 aware 进程返回虚拟化的 96，导致推荐缩放计算错误）
try:
    _shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        _user32.SetProcessDPIAware()
    except Exception:
        pass


class _LUID(Structure):
    _fields_ = [("LowPart", c_uint32), ("HighPart", c_int)]


class _PATH_SRC(Structure):
    _fields_ = [("adapterId", _LUID), ("id", c_uint32),
                ("modeInfoIdx", c_uint32), ("statusFlags", c_uint32)]


class _RATIONAL(Structure):
    _fields_ = [("Numerator", c_uint32), ("Denominator", c_uint32)]


class _PATH_TGT(Structure):
    _fields_ = [("adapterId", _LUID), ("id", c_uint32), ("modeInfoIdx", c_uint32),
                ("outputTechnology", c_uint32), ("rotation", c_uint32), ("scaling", c_uint32),
                ("refreshRate", _RATIONAL), ("scanLineOrdering", c_uint32),
                ("targetAvailable", c_uint32), ("statusFlags", c_uint32)]


class _PATH_INFO(Structure):
    _fields_ = [("sourceInfo", _PATH_SRC), ("targetInfo", _PATH_TGT), ("flags", c_uint32)]


class _DEVINFO_HDR(Structure):
    _fields_ = [("type", c_int), ("size", c_uint32), ("adapterId", _LUID), ("id", c_uint32)]


class _SRC_NAME(Structure):
    _fields_ = [("header", _DEVINFO_HDR), ("viewGdiDeviceName", c_wchar * 32)]


class _SRC_DPI_GET(Structure):
    _fields_ = [("header", _DEVINFO_HDR), ("minimumScaleRel", c_int),
                ("currentScaleRel", c_int), ("maximumScaleRel", c_int)]


class _SRC_DPI_SET(Structure):
    _fields_ = [("header", _DEVINFO_HDR), ("scaleRel", c_int)]


class _RECT(Structure):
    _fields_ = [("left", c_int), ("top", c_int), ("right", c_int), ("bottom", c_int)]


class _MONINFO(Structure):
    _fields_ = [("cbSize", c_uint32), ("rcMonitor", _RECT), ("rcWork", _RECT),
                ("dwFlags", c_uint32), ("szDevice", c_wchar * 32)]


_QDC_ONLY_ACTIVE_PATHS = 0x00000002
_GET_SOURCE_NAME = 1
_GET_SOURCE_DPI_SCALE = -3
_SET_SOURCE_DPI_SCALE = -4


_source_paths_cache = {"ts": 0.0, "data": None}
_SOURCE_PATHS_TTL = 12.0


def _source_paths():
    """枚举活动显示路径，返回 [(gdi_name, adapterId(LUID), srcId)]。"""
    now = time.time()
    if _source_paths_cache["data"] is not None and now - _source_paths_cache["ts"] < _SOURCE_PATHS_TTL:
        return _source_paths_cache["data"]
    out = []
    np = c_uint32(0); nm = c_uint32(0)
    _user32.GetDisplayConfigBufferSizes(_QDC_ONLY_ACTIVE_PATHS, byref(np), byref(nm))
    if np.value == 0:
        return out
    paths = (_PATH_INFO * np.value)()
    modes = (ctypes.c_byte * nm.value * 64)()
    if _user32.QueryDisplayConfig(_QDC_ONLY_ACTIVE_PATHS, byref(np), paths, byref(nm), modes, None) != 0:
        return out
    for pi in range(np.value):
        p = paths[pi]
        sn = _SRC_NAME()
        sn.header.type = _GET_SOURCE_NAME
        sn.header.size = sizeof(_SRC_NAME)
        sn.header.adapterId = p.sourceInfo.adapterId
        sn.header.id = p.sourceInfo.id
        if _user32.DisplayConfigGetDeviceInfo(byref(sn)) == 0:
            out.append((sn.viewGdiDeviceName.rstrip("\x00"),
                        p.sourceInfo.adapterId, p.sourceInfo.id))
    _source_paths_cache["ts"] = time.time()
    _source_paths_cache["data"] = out
    return out


def _path_for_device(device):
    """根据 GDI device name（如 DISPLAY2）找 (adapterId, srcId)。"""
    device = (device or "").rstrip("\x00")
    for gdi, ad, sid in _source_paths():
        if gdi == device:
            return (ad, sid)
    return None


def _hmonitor_for_device(device):
    """EnumDisplayMonitors 找 GDI device name 对应的 HMONITOR。"""
    device = (device or "").rstrip("\x00")
    found = {}
    _MONITORENUMPROC = ctypes.WINFUNCTYPE(
        c_int, wintypes.HMONITOR, wintypes.HDC, POINTER(_RECT), wintypes.LPARAM)

    def cb(h, hdc, r, l):
        mi = _MONINFO()
        mi.cbSize = sizeof(_MONINFO)
        _user32.GetMonitorInfoW(h, byref(mi))
        if mi.szDevice.rstrip("\x00") == device:
            found["h"] = h
            return 0
        return 1

    _user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(cb), 0)
    return found.get("h")


def _scale_rel_get(ad, sid):
    """读 (minRel, curRel, maxRel)。失败返回 None。"""
    dg = _SRC_DPI_GET()
    dg.header.type = _GET_SOURCE_DPI_SCALE
    dg.header.size = sizeof(_SRC_DPI_GET)
    dg.header.adapterId = ad
    dg.header.id = sid
    if _user32.DisplayConfigGetDeviceInfo(byref(dg)) != 0:
        return None
    return (dg.minimumScaleRel, dg.currentScaleRel, dg.maximumScaleRel)


def _recommended_pct(device, ad, sid):
    """推荐缩放百分比（scaleRel=0 对应的绝对缩放）。

    反推公式：推荐% = 当前有效缩放% - 当前scaleRel×25
      - 当前有效缩放% 由 GetDpiForMonitor(MDT_EFFECTIVE_DPI)/96*100 得到（需进程 per-monitor DPI aware）
      - 当前 scaleRel 由 GET_SOURCE_DPI_SCALE 的 currentScaleRel 得到
    不同屏推荐值可能不同（如 32 寸 1080p 推荐 125%，27 寸 2K 推荐 100%），不能硬编码。"""
    hmon = _hmonitor_for_device(device)
    eff = c_uint32(0)
    effy = c_uint32(0)
    if hmon is not None:
        try:
            _shcore.GetDpiForMonitor(hmon, 0, byref(eff), byref(effy))  # MDT_EFFECTIVE_DPI
        except Exception:
            pass
    cur_rel = 0
    got = _scale_rel_get(ad, sid)
    if got:
        cur_rel = got[1]
    if eff.value > 0:
        current_pct = eff.value / 96.0 * 100.0
        return current_pct - cur_rel * 25.0
    return 100.0


def monitor_scale_set(mid, scale_pct):
    """设置显示器缩放百分比（立即生效）。"""
    scale_pct = int(scale_pct)
    if scale_pct not in CFG.get("scale_options", [100, 125, 150, 175, 200]):
        raise ValueError("缩放比例必须在 scale_options 内")
    info = _find_monitor(mid)
    if not info:
        raise ValueError("未知显示器: " + str(mid))
    device = info.get("device")
    if not device:
        raise RuntimeError("该显示器未配置 GDI device（请在 config.json 的 monitor_devices 配置）")
    loc = _path_for_device(device)
    if not loc:
        raise RuntimeError("找不到该显示器的活动显示路径: " + str(device))
    ad, sid = loc
    got = _scale_rel_get(ad, sid)
    if not got:
        raise RuntimeError("读取当前缩放失败")
    rec_pct = _recommended_pct(device, ad, sid)
    rel = int(round((scale_pct - rec_pct) / 25.0))
    rel = max(got[0], min(got[2], rel))
    ds = _SRC_DPI_SET()
    ds.header.type = _SET_SOURCE_DPI_SCALE
    ds.header.size = sizeof(_SRC_DPI_SET)
    ds.header.adapterId = ad
    ds.header.id = sid
    ds.scaleRel = rel
    rc = _user32.DisplayConfigSetDeviceInfo(byref(ds))
    if rc != 0:
        raise RuntimeError("设置缩放失败（DisplayConfigSetDeviceInfo 返回 %d）" % rc)
    obj = _status_cache.get("obj")
    if obj:
        for m in obj.get("monitors", []):
            if m.get("id") == mid:
                m["scale"] = scale_pct
                break
    return {"scale": scale_pct, "scaleRel": rel, "recommended": round(rec_pct)}


def monitor_scale_get(mid):
    """读当前缩放百分比（从 SOURCE_DPI_SCALE 的 currentScaleRel 换算）。"""
    info = _find_monitor(mid)
    if not info:
        return None
    device = info.get("device")
    if not device:
        return None
    loc = _path_for_device(device)
    if not loc:
        return None
    ad, sid = loc
    got = _scale_rel_get(ad, sid)
    if not got:
        return None
    rec_pct = _recommended_pct(device, ad, sid)
    pct = rec_pct + got[1] * 25.0
    opts = CFG.get("scale_options", [100, 125, 150, 175, 200])
    return min(opts, key=lambda p: abs(p - pct))


# ---------------- 虚拟鼠标 & 键盘 (SendInput / SetCursorPos, 纯 ctypes) ----------------
# 思路:手机遥控电脑。鼠标用"目标屏绝对坐标映射"(前端给 0~1 归一化坐标,后端换算到
#      目标屏虚拟桌面矩形), 兼容多屏与不同分辨率; 键盘文本走 KEYEVENTF_UNICODE(原生支持中文)。
# 需运行在交互式桌面会话(agent 经 onlogon 任务启动, 满足)。
VKEY = {
    # 通用键
    "enter": 0x0D, "tab": 0x09, "space": 0x20, "esc": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "insert": 0x2D, "home": 0x24,
    "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "capslock": 0x14,
    # 修饰键(组合用)
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    # 功能键
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
}
# 虚拟键码 -> 名称, 供 /vinput/key 展示合法键
VKEY_NAME = {v: k for k, v in VKEY.items()}

_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_WHEEL = 0x0800
_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1


class _MOUSEINPUT(Structure):
    _fields_ = [("dx", c_int), ("dy", c_int), ("mouseData", c_uint32),
                ("dwFlags", c_uint32), ("time", c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(Structure):
    _fields_ = [("wVk", c_uint16), ("wScan", c_uint16), ("dwFlags", c_uint32),
                ("time", c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(Structure):
    _fields_ = [("type", c_uint32), ("u", _INPUTUNION)]


class _POINT(Structure):
    _fields_ = [("x", c_int), ("y", c_int)]


def _get_cursor_pos():
    """读取光标虚拟桌面坐标。ctypes 需显式传 POINT* 才能正确解引用。"""
    pt = _POINT()
    if _user32.GetCursorPos(byref(pt)):
        return int(pt.x), int(pt.y)
    return 0, 0


def _send_inputs(inputs):
    """批量投递 input。用真结构体(非 byte 数组)保证 sizeof(_INPUT)==40。
    全失败(sent==0)时抛错, 供上层反馈; 部分成功返回注入条数。"""
    n = len(inputs)
    if n == 0:
        return True
    arr = (_INPUT * n)()
    for i, inp in enumerate(inputs):
        arr[i] = inp
    sent = _user32.SendInput(n, arr, sizeof(_INPUT))
    if sent == 0:
        err = ctypes.get_last_error() or _user32.GetLastError()
        raise RuntimeError("SendInput 注入失败(返回0, 错误码%d)。多为非交互桌面/无前台焦点或结构错误。" % err)
    return sent


def _mouse_event(flags, data=0, dx=0, dy=0):
    inp = _INPUT()
    inp.type = _INPUT_MOUSE
    inp.u.mi.dx = int(dx)
    inp.u.mi.dy = int(dy)
    inp.u.mi.mouseData = int(data) & 0xFFFFFFFF
    inp.u.mi.dwFlags = flags
    inp.u.mi.time = 0
    inp.u.mi.dwExtraInfo = 0
    return inp


def _key_event(vk=None, unicode=None, extended=False, up=False):
    inp = _INPUT()
    inp.type = _INPUT_KEYBOARD
    inp.u.ki.wVk = (vk or 0) & 0xFFFF
    inp.u.ki.wScan = (unicode or 0) & 0xFFFF
    flags = 0
    if extended: flags |= _KEYEVENTF_EXTENDEDKEY
    if up: flags |= _KEYEVENTF_KEYUP
    if unicode is not None: flags |= _KEYEVENTF_UNICODE
    inp.u.ki.dwFlags = flags
    inp.u.ki.time = 0
    inp.u.ki.dwExtraInfo = 0
    return inp


def _key_tap(vk, extended=False):
    return [_key_event(vk=vk, extended=extended), _key_event(vk=vk, extended=extended, up=True)]


def _mod_press(vks):
    return [_key_event(vk=v) for v in vks]


def _mod_release(vks):
    return [_key_event(vk=v, up=True) for v in vks]


# ---- 屏幕几何 (目标屏的虚拟桌面矩形) ----
_screen_rect_cache = {"ts": 0.0, "rects": None}
_SCREEN_RECT_TTL = 12.0


def _screen_rects():
    """枚举显示器 -> {mid: {left,top,right,bottom}}. 用 EnumDisplayMonitors + GetMonitorInfo. """
    now = time.time()
    if _screen_rect_cache["rects"] is not None and now - _screen_rect_cache["ts"] < _SCREEN_RECT_TTL:
        return _screen_rect_cache["rects"]
    rects = {}
    _MONITORENUMPROC = ctypes.WINFUNCTYPE(
        c_int, wintypes.HMONITOR, wintypes.HDC, POINTER(_RECT), wintypes.LPARAM)

    def cb(h, hdc, r, l):
        mi = _MONINFO()
        mi.cbSize = sizeof(_MONINFO)
        _user32.GetMonitorInfoW(h, byref(mi))
        dev = mi.szDevice.rstrip("\x00")  # e.g. \\.\DISPLAY2
        # dev 是 GDI 名(可能带前导 \\.\), 与 config monitor_devices 匹配
        mid = None
        for k, v in (CFG.get("monitor_devices") or {}).items():
            if (v or "").lower().replace("\\\\", "") == dev.lower().replace("\\\\", ""):
                mid = k
                break
        if mid is None:
            # 兜底: 用设备尾号匹配 internal 等
            num = dev.rsplit("\\", 1)[-1].upper()
            for k, v in (CFG.get("monitor_devices") or {}).items():
                if (v or "").upper().endswith(num):
                    mid = k
                    break
        if mid is None:
            mid = "?" + dev
        rects[mid] = {"left": mi.rcMonitor.left, "top": mi.rcMonitor.top,
                      "right": mi.rcMonitor.right, "bottom": mi.rcMonitor.bottom}
        return 1

    _user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(cb), 0)
    _screen_rect_cache["ts"] = time.time()
    _screen_rect_cache["rects"] = rects
    return rects


def _resolve_target_monitor(mid):
    """把 mid 解析为可用于坐标映射的显示器 id(优先按 config 匹配), 返回可用的 id 或抛错。"""
    rects = _screen_rects()
    if mid in rects:
        return mid
    # 按 enum monitor 列表尝试匹配 (internal/型号)
    for info in _enum_monitors():
        if info["id"] == mid and info["id"] in rects:
            return info["id"]
    raise ValueError("无法定位显示器 %s(可能未连接或未配置 monitor_devices)" % mid)


def _clamp_point(mid, x, y):
    """把坐标限制在目标屏矩形内, 防止鼠标飘到别的屏。"""
    r = _screen_rects().get(mid)
    if not r:
        return int(x), int(y)
    x = max(r["left"], min(r["right"] - 1, int(x)))
    y = max(r["top"], min(r["bottom"] - 1, int(y)))
    return x, y


def _set_pos(mid, x, y):
    x, y = _clamp_point(mid, x, y)
    # 优先 SendInput 硬件级绝对移动: 与物理鼠标同一输入路径,
    # 播放器/独占渲染窗口也认(否则 SetCursorPos 移动后光标被视频画面盖住)。
    # 归一化 0~65535, MULTIMONITOR 标志按虚拟桌面全局坐标映射。
    _MOUSEEVENTF_ABSOLUTE = 0x8000
    _MOUSEEVENTF_VIRTUALDESK = 0x4000
    sm_x, sm_y = 76, 77  # SM_XVIRTUALSCREEN / SM_YVIRTUALSCREEN
    vx = _user32.GetSystemMetrics(sm_x)
    vy = _user32.GetSystemMetrics(sm_y)
    vw = _user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    vh = _user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
    if vw > 0 and vh > 0:
        ax = int(round((x - vx) * 65535.0 / (vw - 1)))
        ay = int(round((y - vy) * 65535.0 / (vh - 1)))
        _send_inputs([_mouse_event(_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE
                                   | _MOUSEEVENTF_VIRTUALDESK, dx=ax, dy=ay)])
    else:
        _user32.SetCursorPos(x, y)
    return (x, y)


def _virtual_bounds():
    """整个虚拟桌面的像素边界, 用于相对移动/拖拽的钳制(不限制到单屏, 防跨屏瞬移)。"""
    vx = _user32.GetSystemMetrics(76); vy = _user32.GetSystemMetrics(77)
    vw = _user32.GetSystemMetrics(78); vh = _user32.GetSystemMetrics(79)
    if vw > 0 and vh > 0:
        return (vx, vy, vx + vw, vy + vh)
    rects = list(_screen_rects().values())
    if not rects:
        return (0, 0, 1, 1)
    xs = [r["left"] for r in rects] + [r["right"] for r in rects]
    ys = [r["top"] for r in rects] + [r["bottom"] for r in rects]
    return (min(xs), min(ys), max(xs), max(ys))


def _monitor_at(x, y):
    """返回包含 (x,y) 的显示器 id; 不在任何屏内返回 None。"""
    for mid, r in _screen_rects().items():
        if r["left"] <= x < r["right"] and r["top"] <= y < r["bottom"]:
            return mid
    return None


def _safe_mid(mid):
    """温和解析显示器 id, 解析失败返回 None(不抛异常)。"""
    rects = _screen_rects()
    if mid in rects:
        return mid
    try:
        return _resolve_target_monitor(mid)
    except Exception:
        return None


def _set_pos_global(x, y):
    """把光标移到虚拟桌面全局坐标, 钳制到整个虚拟桌面(不限制单屏,
    防止跨屏/右键菜单场景时相对移动瞬移到目标屏边缘)。"""
    x, y = int(x), int(y)
    vx, vy, vr, vb = _virtual_bounds()
    x = max(vx, min(vr - 1, x)); y = max(vy, min(vb - 1, y))
    _MOUSEEVENTF_ABSOLUTE = 0x8000
    _MOUSEEVENTF_VIRTUALDESK = 0x4000
    ax = int(round((x - vx) * 65535.0 / max(1, (vr - vx - 1))))
    ay = int(round((y - vy) * 65535.0 / max(1, (vb - vy - 1))))
    _send_inputs([_mouse_event(_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE
                               | _MOUSEEVENTF_VIRTUALDESK, dx=ax, dy=ay)])
    return (x, y)


def vinput_mouse_move(mid, nx, ny):
    """绝对定位(轻点取点/居中): 以"光标当前所在屏"为基准映射 nx/ny,
    使点击跟随光标跨屏 —— 修复在任务栏右键打开菜单后触控板失灵的问题。"""
    cx, cy = _get_cursor_pos()
    mid2 = _monitor_at(cx, cy) or _safe_mid(mid)
    r = _screen_rects().get(mid2) or _screen_rects().get(_safe_mid(mid))
    if not r:
        raise ValueError("找不到可用显示器用于定位")
    nx = max(0.0, min(1.0, float(nx)))
    ny = max(0.0, min(1.0, float(ny)))
    px = r["left"] + int(round(nx * (r["right"] - r["left"] - 1)))
    py = r["top"] + int(round(ny * (r["bottom"] - r["top"] - 1)))
    return _set_pos_global(px, py)


def vinput_mouse_delta(mid, dx, dy):
    """相对移动(触控板手感): 走 SendInput 相对路径, 与物理鼠标同一输入流。
    右键菜单/系统托盘/任务栏等模态场景下, 绝对坐标容易被系统忽略导致光标"死掉",
    相对移动仍然能更新光标位置; 同时保留 SendInput 硬件路径, 视频画面下光标可见。"""
    dx = int(dx); dy = int(dy)
    if dx == 0 and dy == 0:
        return True
    # 限制单次位移, 防跳变
    dx = max(-512, min(512, dx))
    dy = max(-512, min(512, dy))
    _send_inputs([_mouse_event(_MOUSEEVENTF_MOVE, dx=dx, dy=dy)])
    return True


def vinput_mouse_button(mid, button="left", action="click", times=1, delta=None):
    """点击/按下/释放/滚轮。button: left/right/middle/wheel/scroll. action: click/down/up.
    click 支持 times=2 (双击)。scroll: 优先用 delta(精确滚轮单位, 可非120倍数, 平滑跟手),
    无 delta 时回退 times(格数) 兼容旧调用。
    点击在"当前真实光标"处执行, 不要求目标屏必须启用/可达
    (修复目标屏被关屏时点击报"无法定位显示器"的错误)。"""
    btn = str(button or "left").lower()
    act = str(action or "click").lower()

    if btn in ("scroll", "wheel"):
        d = int(round(float(delta))) if delta is not None else int(times) * 120
        if d == 0:
            return True
        # 滚轮有效值 = 有符号16位, 限制范围防止溢出/跳变
        d = max(-32767, min(32767, d))
        _send_inputs([_mouse_event(_MOUSEEVENTF_WHEEL, data=d)])
        return True

    if btn == "left":
        down, up = _MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP
    elif btn == "right":
        down, up = _MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP
    elif btn == "middle":
        down, up = _MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP
    else:
        raise ValueError("未知鼠标键: " + btn)

    if act == "down":
        _send_inputs([_mouse_event(down)])
    elif act == "up":
        _send_inputs([_mouse_event(up)])
    else:  # click
        n = max(1, int(times))
        # 右键在任务栏/托盘等场景容易触发系统菜单进入模态循环, 导致 up 被延迟/吞掉,
        # 进而使后续触控板移动被当成"拖拽"处理而失灵。给 down/up 之间加微小间隔,
        # 并在最后补一次 up, 确保按钮状态被正确释放。
        if btn == "right":
            for _ in range(n):
                _send_inputs([_mouse_event(down)])
                time.sleep(0.015)
                _send_inputs([_mouse_event(up)])
                if n > 1:
                    time.sleep(0.030)
            time.sleep(0.010)
            _send_inputs([_mouse_event(up)])
        else:
            for _ in range(n):
                _send_inputs([_mouse_event(down), _mouse_event(up)])
            _send_inputs([_mouse_event(up)])
    return True


def vinput_mouse_drag(mid, x1, y1, x2, y2, button="left"):
    """在目标屏上从 (x1,y1) 拖到 (x2,y2), x/y 均归一化 0~1。"""
    if mid in (None, "all", ""):
        raise ValueError("虚拟鼠标需指定目标屏 monitor")
    mid = _resolve_target_monitor(mid)
    btn = str(button or "left").lower()
    if btn == "right":
        down, up = _MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP
    elif btn == "middle":
        down, up = _MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP
    else:
        down, up = _MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP
    x1 = max(0.0, min(1.0, float(x1))); y1 = max(0.0, min(1.0, float(y1)))
    x2 = max(0.0, min(1.0, float(x2))); y2 = max(0.0, min(1.0, float(y2)))
    r = _screen_rects().get(mid)
    if not r:
        raise ValueError("找不到显示器 %s 的屏幕区域" % mid)
    def _px(v, lo, hi):
        return lo + int(round(v * (hi - lo - 1)))
    p1 = _set_pos(mid, _px(x1, r["left"], r["right"]), _px(y1, r["top"], r["bottom"]))
    _send_inputs([_mouse_event(down)])
    # 平滑走几步, 让系统感知为拖拽而非瞬移
    steps = 12
    sx = _px(x2, r["left"], r["right"]); sy = _px(y2, r["top"], r["bottom"])
    for i in range(1, steps + 1):
        _set_pos(mid, p1[0] + (sx - p1[0]) * i // steps, p1[1] + (sy - p1[1]) * i // steps)
        time.sleep(0.008)
    _set_pos(mid, sx, sy)
    _send_inputs([_mouse_event(up)])
    return True


def vinput_key_press(mods=None, key=None, unicode=None):
    """组合键/单键。mods: [ctrl,shift,...]; key: 虚拟键名(见 VKEY)或单字符。
    若给 unicode(任意字符串), 则用 KEYEVENTF_UNICODE 逐字符输入(支持中文/emoji)。"""
    if unicode is not None:
        return vinput_type_text(str(unicode))
    mod_vks = []
    for m in (mods or []):
        m = str(m).lower()
        if m not in VKEY:
            raise ValueError("未知修饰键: " + m)
        if m not in ("ctrl", "shift", "alt", "win"):
            raise ValueError("仅支持 ctrl/shift/alt/win 作为修饰键, 收到: " + m)
        mod_vks.append(VKEY[m])
    if key is None:
        raise ValueError("需提供 key")
    key = str(key)
    kname = key.lower()
    if kname in VKEY:
        vk = VKEY[kname]
        _send_inputs(_mod_press(mod_vks) + _key_tap(vk) + _mod_release(mod_vks))
        return True
    # 单个可见字符键 (如 'a','1','+'): 用 KEYEVENTF_UNICODE 兼容
    _send_inputs(_mod_press(mod_vks))
    for ch in key:
        code = ord(ch)
        _send_inputs([_key_event(unicode=code), _key_event(unicode=code, up=True)])
    _send_inputs(_mod_release(mod_vks))
    return True


def vinput_type_text(text):
    """逐字符 Unicode 输入整段文本(支持中文/英文/标点/emoji)。速度适中避免丢键。"""
    text = str(text or "")
    if not text:
        return True
    chunk = 20   # 分批投递, 每批间小停, 降低 SendInput 丢键风险
    for i in range(0, len(text), chunk):
        seg = text[i:i + chunk]
        ins = []
        for ch in seg:
            code = ord(ch)
            ins.append(_key_event(unicode=code))
            ins.append(_key_event(unicode=code, up=True))
        _send_inputs(ins)
        time.sleep(0.02)
    return True


def vinput_key_names():
    return sorted(set(VKEY_NAME.values()))


def vinput_mouse_pos(mid):
    """读当前光标(虚拟桌面坐标) + 若在目标屏内给出归一化位置(可选, 供 UI 显示/同步)."""
    cx, cy = _get_cursor_pos()
    out = {"x": cx, "y": cy}
    if mid:
        try:
            mid = _resolve_target_monitor(mid)
            r = _screen_rects().get(mid)
            if r:
                w = r["right"] - r["left"]; h = r["bottom"] - r["top"]
                if w > 0 and h > 0:
                    out["nx"] = round((cx - r["left"]) / w, 4)
                    out["ny"] = round((cy - r["top"]) / h, 4)
                    out["in_target"] = (r["left"] <= cx < r["right"] and r["top"] <= cy < r["bottom"])
        except Exception:
            pass
    return out


def vinput_screens():
    """返回可用于虚拟鼠标坐标映射的屏列表(带矩形), 供前端选目标屏。"""
    rects = _screen_rects()
    out = []
    for mid, r in rects.items():
        out.append({"id": mid, "label": _label_for(mid),
                    "left": r["left"], "top": r["top"],
                    "right": r["right"], "bottom": r["bottom"],
                    "w": r["right"] - r["left"], "h": r["bottom"] - r["top"]})
    out.sort(key=lambda s: (0 if s["id"] == "internal" else 1, s["label"]))
    return out


# ---- 鼠标滚轮一次滚动的行数 (Windows WHEELSCROLLLINES) ----
_SPI_GETWHEELSCROLLLINES = 0x0068
_SPI_SETWHEELSCROLLLINES = 0x0069
_SPIF_SENDCHANGE = 0x02
_WHEEL_LINES_MAX = 20


def wheel_lines_get():
    """读 Windows 鼠标滚轮一次滚动的行数(默认3)。"""
    try:
        val = c_uint32(0)
        _user32.SystemParametersInfoW(_SPI_GETWHEELSCROLLLINES, 0, byref(val), 0)
        return int(val.value)
    except Exception:
        return 3


def wheel_lines_set(n):
    """设 Windows 鼠标滚轮一次滚动的行数(1~20), 返回生效值。"""
    n = max(1, min(_WHEEL_LINES_MAX, int(n)))
    _user32.SystemParametersInfoW(_SPI_SETWHEELSCROLLLINES, n, None, _SPIF_SENDCHANGE)
    return wheel_lines_get()


# ---------------- 状态 ----------------
_status_cache = {"ts": 0.0, "obj": None}
STATUS_CACHE_TTL = 20
STATUS_REFRESH_INTERVAL = 15
DDC_INTER_MONITOR = 0.5


# ================ 电视盒子(Android)遥控 - /box/* ================
# 盒子是独立 Android 设备，不吃 PC 桌面输入(SendInput 到不了它)，
# 虚拟遥控走 ADB。中文 text 需盒子装 ADBKeyBoard IME(专收 ADB_INPUT_TEXT
# 广播)，讯飞等普通输入法接不了 adb 远程注入。
#
# 【延迟优化】实测本类盒子(定制固件)每次 adb shell input keyevent/tap 自身
# 就要 ~1.2s(Java input 命令冷启动开销)，叠加逐条 shell 建立更慢到 ~1.9s。
# 因此这里做两层提速：
#   1) 长驻一个交互式 adb shell 子进程(管道)，命令写 stdin + echo 标记确认，
#      消除"每条命令都新建 adb shell ~0.4~1.4s"的开销；
#   2) 方向键/OK/返回等按键改走 sendevent 直接写内核 input 设备
#      (/dev/input/eventN, 如 tvpic-virtual)，绕开 Java input，实测 ~150ms
#      (提速约 9 倍)。tap/swipe/text 仍是 input 命令(盒子端固有 ~1.2s)，
#      属低频操作可接受。
ADB_EXE = os.environ.get("PCBOX_ADB", "E:/platform-tools/adb.exe")
BOX_SERIAL = os.environ.get("PCBOX_SERIAL", "192.168.31.218:5555")
BOX_W = int(os.environ.get("PCBOX_W", "1920"))
BOX_H = int(os.environ.get("PCBOX_H", "1080"))
# Android keyevent 码(input keyevent 兜底用)
_BOX_KEYCODES = {
    "left": 21, "right": 22, "up": 19, "down": 20, "ok": 23, "enter": 66,
    "back": 4, "home": 3, "menu": 82, "search": 84,
    "volup": 24, "voldown": 25, "mute": 164,
    "playpause": 85, "play": 126, "pause": 127, "stop": 86,
    "next": 87, "prev": 88, "rewind": 89, "forward": 90,
    "power": 26, "backspace": 67, "del": 67, "tab": 61, "space": 62,
}
# Linux 内核 KEY_* 码(sendevent 用, 仅按键加速路径; 无此码的键回退 input)
_BOX_KERNEL_KEYS = {
    "left": 105, "right": 106, "up": 103, "down": 108, "ok": 28, "enter": 28,
    "back": 158, "home": 102, "menu": 139, "search": 217,
    "volup": 115, "voldown": 114, "mute": 113, "power": 116,
    "playpause": 164, "play": 207, "pause": 119, "stop": 128,
    "next": 163, "prev": 165, "rewind": 168, "forward": 159,
    "backspace": 14, "del": 111, "tab": 15, "space": 57,
}
_box_cache = {"ts": 0.0, "obj": None}

# ---- 长驻 adb shell 管道发送器 ----
# 用独立的锁,避免占用全局限流 _lock 太久阻塞 PC 其它请求; box 命令之间仍串行。
# 长驻一个 adb shell 子进程, 用常驻 reader 线程持续把 stdout 拷进共享 buf
# (Windows 管道无法用 select, 只能靠后台线程逐字节读), 命令写 stdin 后用
# 唯一 echo 标记判断"已执行完"。
_box_pipe = {"p": None, "lock": threading.Lock(), "buf": bytearray(),
             "dev": None, "reader": None,
             "keycodes": set()}   # dev: 内核 input 设备(如 event5); keycodes: 该设备支持的KEY数值


def _adb_cmd(args, timeout=10, connect_if_down=True):
    """单条 adb 命令(读取 stdout 用)。不每次都 connect; 仅离线时尝试 connect。
    返回 (code, stdout, stderr)。"""
    if connect_if_down and not _box_adb_online():
        try:
            subprocess.run([ADB_EXE, "connect", BOX_SERIAL],
                           capture_output=True, timeout=5)
        except Exception:
            pass
    try:
        r = subprocess.run([ADB_EXE, "-s", BOX_SERIAL] + args,
                           capture_output=True, timeout=timeout)
        return r.returncode, r.stdout.decode("utf-8", "replace"), \
            r.stderr.decode("utf-8", "replace")
    except Exception as e:
        return -1, "", str(e)


def _box_adb_online():
    """快速判断设备是否在线(不阻塞长)。"""
    try:
        r = subprocess.run([ADB_EXE, "devices"],
                           capture_output=True, timeout=4)
        txt = r.stdout.decode("utf-8", "replace")
        for line in txt.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == BOX_SERIAL and parts[1] == "device":
                return True
        return False
    except Exception:
        return False


def _box_connected():
    code, out, _ = _adb_cmd(["get-state"], timeout=6)
    return code == 0 and "device" in out


def _box_pipe_start():
    """启动/复用长驻 adb shell。调用方需持有 _box_pipe['lock']。
    返回存活进程或 None。"""
    if _box_pipe["p"] is not None:
        try:
            if _box_pipe["p"].poll() is None:
                return _box_pipe["p"]
        except Exception:
            pass
        try:
            _box_pipe["p"].kill()
        except Exception:
            pass
        _box_pipe["p"] = None
        _box_pipe["buf"] = bytearray()
    if not _box_adb_online():
        try:
            subprocess.run([ADB_EXE, "connect", BOX_SERIAL],
                           capture_output=True, timeout=5)
        except Exception:
            pass
    try:
        p = subprocess.Popen(
            [ADB_EXE, "-s", BOX_SERIAL, "shell"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0)
        _box_pipe["p"] = p
        _box_pipe["buf"] = bytearray()

        def _reader():
            try:
                while True:
                    b = p.stdout.read(1)
                    if not b:
                        break
                    _box_pipe["buf"].extend(b)
            except Exception:
                pass
            finally:
                if _box_pipe["reader"] is threading.current_thread():
                    _box_pipe["reader"] = None

        th = threading.Thread(target=_reader, daemon=True)
        _box_pipe["reader"] = th
        th.start()
        time.sleep(0.4)  # 等 shell 就绪
        return p
    except Exception:
        return None


def _box_pipe_send(line, timeout=8):
    """向长驻 shell 写入一行命令, 用唯一 echo 标记确认执行完毕。
    返回 True=已执行完, False=超时/管道断开。线程安全。"""
    with _box_pipe["lock"]:
        p = _box_pipe_start()
        if p is None or p.stdin is None:
            return False
        tag = "WB_%s" % uuid.uuid4().hex[:6]
        payload = ("%s\necho %s\n" % (line, tag)).encode("utf-8", "replace")
        try:
            p.stdin.write(payload)
            p.stdin.flush()
        except Exception:
            return False
        # 非交互 adb shell 会把 echo 命令的输出(即裸 tag)回显; 搜裸 tag 即可,
        # WB_ + 随机串不会误撞。
        tb = tag.encode("ascii")
        t0 = time.time()
        while time.time() - t0 < timeout:
            if tb in _box_pipe["buf"]:
                # 匹配成功即裁剪已消费前缀, 防 buf 无界增长
                idx = _box_pipe["buf"].find(tb) + len(tb)
                if len(_box_pipe["buf"]) > idx + 4096:
                    del _box_pipe["buf"][:idx + 1]
                elif len(_box_pipe["buf"]) > 65536:
                    _box_pipe["buf"] = bytearray()
                return True
            if p.poll() is not None:
                return False
            time.sleep(0.005)
        return False


_BOX_KEYNAME_CODE = {
    # Linux input-event-codes 里部分常见 KEY_* 名 → 数值(用于 getevent 名单解析)
    "key_left": 105, "key_right": 106, "key_up": 103, "key_down": 108,
    "key_enter": 28, "key_ok": 28, "key_select": 28, "key_kpenter": 96,
    "key_back": 158, "key_home": 102, "key_menu": 139, "key_search": 217,
    "key_volumeup": 115, "key_volumedown": 114, "key_mute": 113,
    "key_power": 116, "key_playpause": 164, "key_play": 207, "key_pause": 119,
    "key_stop": 128, "key_stopcd": 128, "key_nextsong": 163,
    "key_previoussong": 165, "key_rewind": 168, "key_fastforward": 159,
    "key_backspace": 14, "key_delete": 111, "key_tab": 15, "key_space": 57,
}


_box_devices = {"ts": 0.0, "list": None}   # list: [{path,name,codes}], 解析所有设备的能力
_DEVICE_CACHE_TTL = 300                      # 设备能力基本不变, 5min 缓存足够


def _box_detect_kernel_dev():
    """探测"默认"内核 input 设备(优先虚拟遥控 tvpic-virtual; 其次任意支持方向键的)。
    兼容旧调用。返回设备节点, 并把所有设备能力表缓存到 _box_devices。"""
    if _box_pipe["dev"]:
        return _box_pipe["dev"]
    dl = _box_all_devices()
    # 选默认设备: 优先 virtual 名含四方向键; 其次任意含四方向键; 再其次任意 KEY 设备
    def has_dirs(codes):
        return ({103, 108, 105, 106} & codes) == {103, 108, 105, 106}
    dev = None
    for d in dl:
        if d["codes"] and has_dirs(d["codes"]):
            if dev is None or "virtual" in d["name"].lower():
                dev = d["path"]
                if "virtual" in d["name"].lower():
                    break
    if not dev:
        for d in dl:
            if d["codes"]:
                dev = d["path"]
                break
    _box_pipe["dev"] = dev
    return dev


def _box_all_devices():
    """解析盒子所有 input 设备的 KEY 能力表。带 5min 缓存。
    返回 [{path,name,codes(set)}]。getevent 的 KEY 名单跨多行换行, 无 KEY_ token 的
    事件块(如 BTN/ABS)自动被忽略; 只在设备块内收集 KEY_ 名字。"""
    now = time.time()
    if _box_devices["list"] is not None and now - _box_devices["ts"] < _DEVICE_CACHE_TTL:
        return _box_devices["list"]
    _, out, _ = _adb_cmd(["shell", "getevent", "-lp"], timeout=10)
    devices, order, cur, cur_codes, cur_name = [], [], None, set(), ""
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("add device"):
            if cur is not None:
                devices.append({"path": cur, "name": cur_name, "codes": cur_codes})
            m = re.search(r"/dev/input/event\d+", s)
            cur = m.group(0) if m else None
            cur_codes, cur_name = set(), ""
            if cur:
                order.append(cur)
        elif cur and "name:" in s:
            cur_name = s.split("name:", 1)[1].strip().strip('"')
        elif cur and "KEY_" in s:
            for t in re.findall(r"KEY_[A-Z0-9_]+", s):
                n = _BOX_KEYNAME_CODE.get(t.lower())
                if n is not None:
                    cur_codes.add(n)
    if cur is not None:
        devices.append({"path": cur, "name": cur_name, "codes": cur_codes})
    if not devices:                     # 解析失败: 保底事件5
        devices = [{"path": "/dev/input/event5", "name": "fallback", "codes": set()}]
    _box_devices["ts"] = now
    _box_devices["list"] = devices
    return devices


def _box_sendevent(code):
    """找一个"声明支持该 KEY 码"的设备做 sendevent; 找不到返回 False。
    关键改进: 遥控器设备(tvpic-virtual)不支持 KEY_BACKSPACE/DELETE 等键盘码,
    但盒子上通常还有全键盘设备(hi keyboard)或 2.4G 接收器声明支持它们。
    按码跨设备查找, 就能让删除/退格/回车/制表等也走 ~150ms 快路径,
    而不是回退到 input keyevent(~1.4s)。返回 True/False。"""
    dev = None
    for d in _box_all_devices():
        if code in d["codes"]:
            # 同一码多个设备都支持: 优先虚拟遥控(保持方向键习惯走 event5), 否则用首个
            if "virtual" in d["name"].lower():
                dev = d["path"]
                break
            if dev is None:
                dev = d["path"]
    if not dev:                        # 没有任何设备声明该码 -> 回退
        return False
    return _box_pipe_send(
        "sendevent %s 1 %d 1; sendevent %s 0 0 0; "
        "sendevent %s 1 %d 0; sendevent %s 0 0 0"
        % (dev, code, dev, dev, code, dev), timeout=6)


def _box_kernel_key(key):
    """快速按键入口: 该键有内核码就尝试跨设备 sendevent;
    失败/无码回退 False, 让上层走 input keyevent。返回 True/False。"""
    code = _BOX_KERNEL_KEYS.get(key)
    if code is None:
        return False
    return _box_sendevent(code)


def box_status(force=False):
    """盒子状态: 是否连上 + 显示分辨率 + 前台 App。带 3s 短缓存。"""
    now = time.time()
    if not force and _box_cache["obj"] is not None and now - _box_cache["ts"] < 3:
        return _box_cache["obj"]
    obj = {"ok": True, "serial": BOX_SERIAL}
    if _box_connected():
        obj["connected"] = True
        obj["width"], obj["height"] = BOX_W, BOX_H
        # 解析真实 wm size(可能被盒子缩放)
        _, sz, _ = _adb_cmd(["shell", "wm", "size"], timeout=6)
        for line in sz.splitlines():
            low = line.lower()
            if "physical size" in low:
                try:
                    w, h = line.split(":", 1)[1].strip().split("x")
                    obj["width"], obj["height"] = int(w), int(h)
                except Exception:
                    pass
                break
        # 前台 App
        _, foc, _ = _adb_cmd(["shell", "dumpsys", "window"], timeout=12)
        cur = None
        for line in foc.splitlines():
            if "mCurrentFocus=" in line:
                cur = line.split("mCurrentFocus=", 1)[1].strip() or None
                break
        obj["front"] = cur
    else:
        obj["connected"] = False
        obj["width"], obj["height"] = BOX_W, BOX_H
        obj["front"] = None
    _box_cache["obj"] = obj
    _box_cache["ts"] = now
    return obj


def box_key(key, times=1):
    if key not in _BOX_KEYCODES:
        raise ValueError("未知盒按键 %r (可用: %s)" %
                         (key, ",".join(sorted(_BOX_KEYCODES))))
    n = max(1, int(times or 1))
    for _ in range(n):
        # 优先走内核 sendevent(快); 失败或无该内核码回退 input keyevent
        if not _box_kernel_key(key):
            code = _BOX_KEYCODES[key]
            _box_pipe_send("input keyevent %d" % code, timeout=8)


def _f01(v, dflt=0.5):
    try:
        v = float(v)
        return max(0.0, min(1.0, v))
    except Exception:
        return dflt


def box_tap(nx=None, ny=None, x=None, y=None):
    if x is not None and y is not None:
        px, py = int(x), int(y)
    elif nx is not None and ny is not None:
        px = int(round(_f01(nx) * BOX_W))
        py = int(round(_f01(ny) * BOX_H))
    else:
        raise ValueError("需 nx/ny(归一化) 或 x/y(绝对像素)")
    _box_pipe_send("input tap %d %d" % (px, py), timeout=8)


def box_swipe(nx1, ny1, nx2, ny2, duration=300):
    px1 = int(round(_f01(nx1) * BOX_W)); py1 = int(round(_f01(ny1) * BOX_H))
    px2 = int(round(_f01(nx2) * BOX_W)); py2 = int(round(_f01(ny2) * BOX_H))
    dur = max(50, int(duration or 300))
    _box_pipe_send("input swipe %d %d %d %d %d"
                   % (px1, py1, px2, py2, dur), timeout=10)


def box_text(text):
    """向盒子当前输入框注入文本。ASCII 走 input text; 含中文走 ADBKeyBoard 广播。
    若无 ADBKeyBoard IME 会失败并给出提示。"""
    if not text:
        return {"note": "empty"}
    if text.isascii():
        # Android shell input text 会把 %s 还原为空格; 故把空格转成 %s
        safe = text.replace("%s", "\\\\%s").replace(" ", "%s")
        ok = _box_pipe_send("input text %s" % safe, timeout=8)
        if ok:
            return {"method": "input_text", "ok": True}
        return {"method": "input_text", "ok": False, "err": "执行失败/超时"}
    # 非 ASCII(中文/emoji)：需 ADBKeyBoard IME。Android 8/9+ 的 am broadcast
    # 不再接受 UTF-8 字符串(ADB_INPUT_TEXT 失效), 用官方 base64 通道
    # ADB_INPUT_B64 可靠。b64 是纯 ASCII, 无 shell 转义问题。
    if not _box_adb_online():
        return {"method": "adb_input_b64", "ok": False,
                "err": "盒子未连接"}
    enc = base64.b64encode(text.encode("utf-8")).decode("ascii")
    ok = _box_pipe_send("am broadcast -a ADB_INPUT_B64 --es msg '%s'" % enc,
                        timeout=8)
    if ok:
        return {"method": "adb_input_b64", "ok": True}
    return {"method": "adb_input_b64", "ok": False,
            "err": "盒子需安装并启用 ADBKeyBoard 输入法才能远程输入中文"}


# ================ /box 路由辅助(在 do_POST 内联处理, 见 Handler) ================


def _status(force=False):
    now = time.time()
    # 请求端(force=False)只要有任何缓存就直接返回，避免慢速 DDC/WMI 枚举阻塞响应；
    # 缓存的新鲜度由后台刷新线程(force=True)每 STATUS_REFRESH_INTERVAL 秒保证。
    if not force and _status_cache["obj"] is not None:
        return _status_cache["obj"]
    monitors = []
    soft_list = CFG.get("tv_softswitch_monitors") or []
    primary = _primary_model()
    found_ids = set()
    for info in _enum_monitors():
        is_soft = info["id"] in soft_list
        is_dead = info.get("_ddc_dead") is True
        found_ids.add(info["id"])
        entry = {
            "id": info["id"],
            "label": info["label"],
            "kind": info["kind"],
            # DDC 半死/失效屏跳过 brightness 读取(否则 I2C 读阻塞数十秒, 拖垮 /status 导致前端误判离线)
            "brightness": (None if is_dead else monitor_brightness_get(info)),
            "has_input": info["supported_input"] and not is_soft,
            "device": info.get("device"),
            "scale_options": CFG.get("scale_options", [100, 125, 150, 175, 200]),
            "scale": monitor_scale_get(info["id"]),
            "tv_softswitch": is_soft,
            "is_primary": (info["id"] == primary),
        }
        if is_soft:
            # 电视盒屏：不再用 DDC 输入源切换(切不了 HDMI2)，用断/连路径软切换
            entry["inputs"] = {}
            entry["input"] = None
            entry["tv_state"] = _soft_state(info["id"])
        elif info["kind"] == "external":
            cur = None if is_dead else monitor_input_get(info)
            entry["input"] = cur
            # 暴露全部物理输入；safe_inputs 标记"已接线可用"输入，供前端标警告/优先
            entry["inputs"] = dict(info["inputs"])
            entry["safe_inputs"] = list(_allowed_inputs(info["id"], info["inputs"]).keys())
            entry["input_unreachable"] = (cur is None)
            if is_dead:
                entry["_offline_ddc"] = True   # DDC 失效(停在空口/半死态), 供前端显示恢复入口
            if not entry["inputs"]:
                entry["has_input"] = False   # 无可切换源 -> 前端隐藏信号源按钮
        monitors.append(entry)
        time.sleep(DDC_INTER_MONITOR)
    # 补充：配置为电视盒屏、但 Windows 路径已断开(切到电视)而枚举不到的屏。
    # 必须恒定返回, 前端才能始终渲染"切回电脑"按钮。
    for mid in soft_list:
        if mid in found_ids:
            continue
        monitors.append({
            "id": mid,
            "label": _label_for(mid),
            "kind": "external",
            "brightness": None,
            "has_input": False,
            "device": _device_for(mid),
            "scale_options": CFG.get("scale_options", [100, 125, 150, 175, 200]),
            "scale": None,
            "tv_softswitch": True,
            "inputs": {},
            "input": None,
            "tv_state": _soft_state(mid),
            "is_primary": (mid == primary),
            "_offline_path": True,   # 供前端区分"已断开"的软切换屏
        })
    # 补充"死信"外显：DDC 失效/停在空输入/路径被禁用(关屏)而实时枚举剔除, 但身份已知
    # (内存 _KNOWN 或磁盘 _KNOWN_PERSIST, agent 重启不丢) -> 补发恢复入口,
    # 前端可点"恢复主输入/亮屏"尝试软件恢复。
    _merged = {}
    for mid, v in _KNOWN_PERSIST.items():
        _merged[mid] = {"inputs": v.get("inputs") or {}, "safe": v.get("safe") or []}
    for mid, kn in _KNOWN.items():
        if mid == "internal":
            continue
        _merged[mid] = {"inputs": kn.get("inputs") or _merged.get(mid, {}).get("inputs") or {},
                        "safe": kn.get("safe") or _merged.get(mid, {}).get("safe") or []}
    for mid, kn in _merged.items():
        if mid in found_ids or mid == "internal" or mid in soft_list:
            continue
        monitors.append({
            "id": mid,
            "label": _label_for(mid),
            "kind": "external",
            "brightness": None,
            "has_input": True,
            "device": _device_for(mid),
            "scale_options": CFG.get("scale_options", [100, 125, 150, 175, 200]),
            "scale": None,
            "tv_softswitch": False,
            "inputs": dict(kn["inputs"]),
            "safe_inputs": list(kn["safe"]),
            "input": None,
            "input_unreachable": True,
            "is_primary": (mid == primary),
            "_offline_ddc": True,   # 供前端显示"DDC 失效, 尝试恢复"
        })
    # 内置屏(笔记本面板): 通常被禁用、无 EDID 型号, DDC 枚举不到 -> 用 CCD INTERNAL 识别补发,
    # 让"设为主显示器"功能覆盖三台屏(Q27 / XV320 / 内置屏)。
    internal = _ccd_internal_screen()
    if internal and "internal" not in found_ids:
        monitors.append({
            "id": "internal",
            "label": internal["label"],
            "kind": "internal",
            "brightness": None,
            "has_input": False,
            "device": internal["device"],
            "scale_options": CFG.get("scale_options", [100, 125, 150, 175, 200]),
            "scale": None,
            "tv_softswitch": False,
            "inputs": {},
            "input": None,
            "is_primary": (primary == "internal"),
            "path_enabled": internal["enabled"],
            "_pnp": internal["pnp"],
        })
    try:
        vscreens = vinput_screens()
    except Exception:
        vscreens = []
    obj = {
        "ok": True,
        "volume": vol_get(),
        "monitors": monitors,
        "apps": list(CFG.get("apps", {}).keys()),
        "vscreens": vscreens,
        "audio_devices": _audio_list_devices(),
        "wheel_lines": wheel_lines_get(),
    }
    _status_cache["ts"] = now
    _status_cache["obj"] = obj
    return obj


def _patch_status_cache(monitor_id, **fields):
    obj = _status_cache.get("obj")
    if not obj: return
    for m in obj.get("monitors", []):
        if m.get("id") == monitor_id:
            m.update(fields); break


# ---------------- 应用 ----------------
def app_run(name):
    apps = CFG.get("apps", {})
    cmd = apps.get(name)
    if not cmd: raise ValueError("未知应用: " + str(name))
    try:
        ps = '(New-Object -ComObject WScript.Shell).AppActivate("%s")' % name.replace('"', '""')
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=10)
    except Exception:
        pass
    subprocess.Popen('start "" "%s"' % cmd, shell=True)
    return True


def apps_list():
    """返回 {应用名: 启动命令/路径} 完整映射（供前端管理界面显示与编辑）。"""
    return dict(CFG.get("apps", {}))


def apps_save(mapping):
    """用前端提交的 {应用名: 命令} 覆盖保存应用列表。做基本清洗：
    丢弃空命令项，规范化路径。返回最终保存的映射。"""
    if not isinstance(mapping, dict):
        raise ValueError("参数必须是 {name: cmd} 对象")
    clean = {}
    for name, cmd in mapping.items():
        name = str(name).strip()
        cmd = str(cmd or "").strip()
        if not name or not cmd:
            continue
        # 防止把整行当命令里带引号/注释字符破坏 start 语义, 统一去包裹引号
        clean[name] = cmd.strip('"')
    CFG["apps"] = clean
    save_config()
    return clean


def apps_scan():
    """扫描开始菜单 + 桌面快捷方式(.lnk)，返回候选应用列表 [{name, path}]，
    供前端『从电脑添加已装应用』。去重(短名优先), 排除明显的卸载/帮助/readme。"""
    up = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    programdata = os.environ.get("ProgramData") or "C:/ProgramData"
    dirs = set()
    # 不依赖 APPDATA/ProgramData 环境变量(服务式启动常缺失), 用固定已知路径拼装。
    dirs.add(os.path.join(up, "AppData", "Roaming", "Microsoft", "Windows",
                          "Start Menu", "Programs"))                    # 用户开始菜单
    dirs.add(os.path.join(programdata, "Microsoft", "Windows",
                          "Start Menu", "Programs"))                     # 所有用户开始菜单
    dirs.add(os.path.join(up, "Desktop"))                                # 用户桌面
    dirs.add("C:/Users/Public/Desktop")                                  # 公共桌面
    # 递归收集 .lnk
    hits = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for fn in files:
                if fn.lower().endswith(".lnk"):
                    hits.append(os.path.join(root, fn))
    # 生成条目
    ex_keywords = ("uninstall", "readme", "帮助", "卸载", "反馈", "启动", "设置",
                   "升级", "更新", "欢迎", "许可", "license")
    entries = []
    for p in hits:
        base = os.path.basename(p)
        name = os.path.splitext(base)[0].strip()
        if not name:
            continue
        low = (name + " " + base).lower()
        if any(k in low for k in ex_keywords):
            continue
        entries.append({"name": name, "path": p})
    # 去重: 同名保留首个; 短名/常见名排序靠前
    entries.sort(key=lambda x: (len(x["name"]), x["name"]))
    dedup = {}
    for e in entries:
        dedup.setdefault(e["name"].lower(), e)
    return sorted(dedup.values(), key=lambda x: x["name"].lower())


# ---------------- HTTP ----------------
def _check_token(headers):
    if not TOKEN: return True
    return headers.get("X-Token", "") == TOKEN


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            # 客户端提前断开(手机轮询超时/页面刷新等)——静默丢弃, 不能让线程异常外溢
            try:
                self.close_connection = True
            except Exception:
                pass

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
        except Exception:
            return {}

    def _ui_html(self):
        html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PC 显示器管理面板</title>
<style>
  :root{--bg:#f5f6f8;--card:#fff;--bd:#e3e6ea;--tx:#1f2329;--mut:#8a9099;--pri:#2f6bff;--ok:#1fa971;--warn:#e8a33d;--danger:#e5484d;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.5 -apple-system,"Microsoft YaHei",sans-serif;padding:18px}
  h1{font-size:18px;margin:0 0 4px}
  .sub{color:var(--mut);font-size:12px;margin-bottom:14px}
  .tok{display:flex;gap:8px;align-items:center;margin-bottom:14px;font-size:12px;color:var(--mut)}
  .tok input{width:160px;padding:4px 8px;border:1px solid var(--bd);border-radius:6px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
  .card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px}
  .card h2{font-size:15px;margin:0 0 2px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .badge{font-size:11px;padding:1px 7px;border-radius:20px;background:var(--pri);color:#fff}
  .badge.off{background:#c9ccd1;color:#fff}
  .row{margin:10px 0}
  .lbl{font-size:12px;color:var(--mut);margin-bottom:4px}
  .btns{display:flex;flex-wrap:wrap;gap:8px}
  button{font:inherit;cursor:pointer;border:1px solid var(--bd);background:#fff;color:var(--tx);padding:6px 12px;border-radius:8px;transition:.15s}
  button:hover{border-color:var(--pri)}
  button.primary{background:var(--pri);color:#fff;border-color:var(--pri)}
  button.recover{background:var(--ok);color:#fff;border-color:var(--ok)}
  button.warn{border-color:var(--warn);color:#b9791f}
  button.active{background:var(--pri);color:#fff;border-color:var(--pri)}
  .cur{font-size:13px}
  .cur b{color:var(--tx)}
  .hint{font-size:12px;color:var(--mut);margin-top:6px}
  .vol{display:flex;gap:8px;align-items:center;margin-top:16px}
  input[type=range]{width:100%}
  .toast{position:fixed;left:50%;top:16px;transform:translateX(-50%);background:#1f2329;color:#fff;padding:8px 16px;border-radius:8px;font-size:13px;opacity:0;transition:.2s;pointer-events:none;z-index:9}
  .toast.show{opacity:1}
</style>
</head>
<body>
<h1>PC 显示器管理面板</h1>
<div class="sub">信号源切换 · 主显示器设置 · 死信恢复 · 亮度/音量 &nbsp;|&nbsp; 由 PC Control Agent 提供</div>
<div class="tok">访问令牌 <input id="token" value="qyx2026"> <span>(与 agent 配置一致)</span></div>
<div id="app" class="grid"></div>
<div class="vol" style="margin-top:16px">
  <button onclick="vol('down')">音量-</button>
  <button onclick="vol('up')">音量+</button>
  <button onclick="vol('mute')">静音</button>
  <span id="volval" class="cur"></span>
</div>
<div class="toast" id="toast"></div>
<script>
const api = p => fetch(p).then(r=>r.json());
async function post(path, body){
  const t = document.getElementById('token').value.trim();
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json','X-Token':t}, body: JSON.stringify(body||{})});
  return r.json();
}
function esc(s){ return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function toast(msg, ok){ ok = ok!==false; const el=document.getElementById('toast'); el.textContent=msg; el.className='toast show'+(ok?'':' err'); setTimeout(()=>el.className='toast',1900); }
async function load(){
  const s = await api('/status');
  if(!s.ok){ toast('获取状态失败', false); return; }
  document.getElementById('volval').textContent = '当前音量: '+(s.volume==null?'?':s.volume);
  const app = document.getElementById('app'); app.innerHTML='';
  (s.monitors||[]).forEach(m=>app.appendChild(card(m)));
}
function card(m){
  const d=document.createElement('div'); d.className='card';
  const isInternal = m.kind==='internal';
  const pri = m.is_primary ? '<span class="badge">主显示器</span>' : '<span class="badge off">副显</span>';
  const disabledBadge = (isInternal && m.path_enabled===false) ? ' <span class="badge off">已禁用</span>' : '';
  const offline = m._offline_ddc ? ' <span class="badge off">DDC失效·可恢复</span>' : (m.input_unreachable?' <span class="badge off">输入读取失败</span>':'');
  d.innerHTML = '<h2>'+esc(m.label)+' '+pri+disabledBadge+offline+'</h2>'+
    '<div class="hint">'+esc(m.id)+(isInternal&&m._pnp?' ('+esc(m._pnp)+')':'')+'</div>'+
    '<div class="row"><div class="lbl">当前信号源</div><div class="cur">'+curInput(m)+'</div></div>'+
    '<div class="row"><div class="lbl">切换信号源</div><div class="btns" id="inp_'+esc(m.id)+'"></div></div>'+
    '<div class="row"><div class="lbl">操作</div><div class="btns">'+
      (isInternal?'':'<button class="recover" onclick="recover(\''+esc(m.id)+'\')">恢复主输入</button>')+
      '<button class="primary" onclick="setPrimary(\''+esc(m.id)+'\')">设为主显示器</button>'+
      '<button onclick="blank(\''+esc(m.id)+'\')">关屏</button>'+
      '<button onclick="unblank(\''+esc(m.id)+'\')">亮屏</button>'+
    '</div></div>'+
    '<div class="hint">关屏/亮屏=可靠黑屏(禁用/启用 Windows 路径); 优于切到空 HDMI1(依赖 DDC, 易卡死)</div>'+
    '<div class="row"><div class="lbl">亮度 <span id="bval_'+esc(m.id)+'">'+(m.brightness==null?'-':m.brightness)+'</span></div>'+
      '<input type="range" min="0" max="100" value="'+(m.brightness==null?50:m.brightness)+'" onchange="setB(\''+esc(m.id)+'\',this.value)"></div>';
  const box=document.getElementById('inp_'+m.id);
  const inputs=m.inputs||{}; const safe=m.safe_inputs||[];
  Object.keys(inputs).forEach(src=>{
    const vcp=inputs[src]; const isSafe = safe.indexOf(src)>=0;
    const b=document.createElement('button');
    b.textContent = src + (isSafe?'':' 未连接');
    if(!isSafe) b.className='warn';
    if(m.input===vcp) b.className+=' active';
    b.onclick=function(){ swInput(m.id, src, !isSafe); };
    box.appendChild(b);
  });
  if(Object.keys(inputs).length===0){ box.innerHTML='<span class="hint">'+(isInternal?'内置屏不支持信号源切换':'该屏不支持信号源切换')+'</span>'; }
  return d;
}
function curInput(m){
  if(m.input==null) return '<span class="hint">无法读取（可能停在空输入）</span>';
  let name=''; const inp=m.inputs||{};
  for(const k in inp){ if(inp[k]===m.input) name=k; }
  return '<b>'+name+'</b> (VCP'+m.input+')';
}
async function swInput(mid, src, unsafe){
  const r = await post('/input', {monitor:mid, src:src, force:true});
  if(r.ok){ toast('已切换 '+mid+' -> '+src+(unsafe?'（黑屏中，点「恢复主输入」可切回）':'')); } else { toast('切换失败: '+r.error, false); }
  setTimeout(load, 900);
}
async function recover(mid){
  const r = await post('/input/recover', {monitor:mid});
  if(r.ok){
    let note = ' 已恢复 '+mid+' -> '+r.recovered_to;
    if(r.method && r.method!=='ddc') note += ' ('+r.method+'重建路径)';
    toast(note);
  } else { toast('恢复失败: '+r.error, false); }
  setTimeout(load, 1300);
}
async function setPrimary(mid){
  const r = await post('/monitor/primary', {monitor:mid});
  if(r.ok){ toast(mid+' 已设为主显示器'); } else { toast('失败: '+r.error, false); }
  setTimeout(load, 1100);
}
async function blank(mid){
  const r = await post('/monitor/blank', {monitor:mid});
  if(r.ok){ toast('已关屏 '+mid+'（亮屏按钮可恢复）'); } else { toast('关屏失败: '+r.error, false); }
  setTimeout(load, 1300);
}
async function unblank(mid){
  const r = await post('/monitor/unblank', {monitor:mid});
  if(r.ok){ toast('已亮屏 '+mid+' '+((r.msg&&r.msg.indexOf('已启用')>=0)?'':'') ); } else { toast('亮屏失败: '+r.error, false); }
  setTimeout(load, 1300);
}
async function setB(mid, v){
  document.getElementById('bval_'+mid).textContent=v;
  await post('/brightness', {monitor:mid, value:parseInt(v)});
  setTimeout(load, 600);
}
async function vol(a){ const r= await post('/volume', {action:a}); document.getElementById('volval').textContent='当前音量: '+(r.volume==null?'?':r.volume); }
load(); setInterval(load, 6000);
</script>
</body>
</html>"""
        return html

    def do_GET(self):
        p = self.path.split("?")[0]
        try:
            if p in ("/ui", "/panel"):
                html = self._ui_html()
                raw = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if p in ("/", "/status"):
                self._send(200, _status()); return
            if p == "/box/status":
                self._send(200, box_status(force=(self.path.endswith("?force")))); return
            if p == "/audio":
                self._send(200, {"ok": True, **audio_list()}); return
            if p == "/apps":
                self._send(200, {"ok": True, "apps": apps_list()}); return
            if p == "/apps/scan":
                self._send(200, {"ok": True, "candidates": apps_scan()}); return
            self._send(404, {"ok": False, "error": "not found"})
        except Exception as e:
            # do_GET 之前无兜底: 任意异常(含 _send 连接中断)会外溢到
            # socketserver → 打印 traceback 到 stdout, ThreadingHTTPServer 不崩
            # 但会在 agent.log 制造大量噪音; 这里统一吞掉。
            try:
                self._send(500, {"ok": False, "error": type(e).__name__ + ": " + str(e)[:120]})
            except Exception:
                pass

    def do_POST(self):
        p = self.path.split("?")[0]
        if p not in ("/volume", "/audio/set", "/app", "/apps/save", "/brightness", "/input", "/input/recover", "/monitor/rename",
                     "/monitor/scale", "/monitor/tvswitch", "/monitor/primary",
                     "/monitor/blank", "/monitor/unblank",
                     "/vinput/mouse", "/vinput/key", "/vinput/text", "/vinput/pos",
                     "/vinput/wheellines",
                     "/box/key", "/box/tap", "/box/swipe", "/box/text"):
            self._send(404, {"ok": False, "error": "not found"}); return
        if not _check_token(self.headers):
            self._send(403, {"ok": False, "error": "token 错误"}); return
        body = self._body()
        # /box/* 走 ADB(慢, input 可达 ~1.2s), 放全局锁外避免阻塞其它 PC 请求;
        # box 命令内部经 _box_pipe 自己的锁串行, 足够。
        if p.startswith("/box/"):
            try:
                if p == "/box/key":
                    box_key(body.get("key"), times=body.get("times", 1))
                    self._send(200, {"ok": True, "key": body.get("key")})
                elif p == "/box/tap":
                    box_tap(nx=body.get("nx"), ny=body.get("ny"),
                            x=body.get("x"), y=body.get("y"))
                    self._send(200, {"ok": True})
                elif p == "/box/swipe":
                    box_swipe(body.get("x1", 0.5), body.get("y1", 0.5),
                              body.get("x2", 0.5), body.get("y2", 0.5),
                              duration=body.get("duration", 300))
                    self._send(200, {"ok": True})
                elif p == "/box/text":
                    self._send(200, {"ok": True,
                                     **box_text(body.get("text", ""))})
                return
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)}); return
        try:
            with _lock:
                if p == "/volume":
                    a = body.get("action", "set")
                    if a == "up": v = vol_step(STEP)
                    elif a == "down": v = vol_step(-STEP)
                    elif a == "mute": v = {"muted": vol_mute()}
                    elif a == "set": v = vol_set(body.get("value", 50))
                    else: raise ValueError("未知 action: " + str(a))
                    self._send(200, {"ok": True, "volume": v})
                elif p == "/audio/set":
                    dev = body.get("id")
                    if not dev: raise ValueError("缺少音频设备 id")
                    res = audio_set(dev)
                    # 切默认后音量对象变化, 清掉 /status 缓存让前端及时反映新设备
                    _status_cache["ts"] = 0.0
                    _status_cache["obj"] = None
                    self._send(200, res)
                elif p == "/app":
                    app_run(body.get("name"))
                    self._send(200, {"ok": True})
                elif p == "/apps/save":
                    saved = apps_save(body.get("apps") or {})
                    # 清掉 /status 缓存, 让前端按钮列表即时刷新
                    _status_cache["ts"] = 0.0
                    _status_cache["obj"] = None
                    self._send(200, {"ok": True, "apps": saved})
                elif p == "/brightness":
                    mid = body.get("monitor"); val = int(body.get("value", 50))
                    monitor_brightness_set(mid, val)
                    _patch_status_cache(mid, brightness=val)
                    self._send(200, {"ok": True, "monitor": mid, "brightness": val})
                elif p == "/input":
                    mid = body.get("monitor")
                    if not mid: raise ValueError("缺少 monitor 参数")
                    vcp = monitor_input_set(mid, src=body.get("src"), vcp=body.get("vcp"),
                                           force=bool(body.get("force")))
                    _patch_status_cache(mid, input=vcp)
                    self._send(200, {"ok": True, "monitor": mid,
                                     "src": body.get("src"), "vcp": vcp})
                elif p == "/input/recover":
                    mid = body.get("monitor")
                    if not mid: raise ValueError("缺少 monitor 参数")
                    vcp, method, target = monitor_input_recover(mid)
                    _patch_status_cache(mid, input=vcp)
                    self._send(200, {"ok": True, "monitor": mid, "recovered_to": target,
                                     "vcp": vcp, "method": method})
                elif p == "/monitor/primary":
                    mid = body.get("monitor")
                    if not mid: raise ValueError("缺少 monitor 参数")
                    monitor_set_primary(mid)
                    _status_cache["ts"] = 0.0
                    _status_cache["obj"] = None
                    self._send(200, {"ok": True, "monitor": mid, "primary": mid})
                elif p == "/monitor/blank":
                    mid = body.get("monitor")
                    if not mid: raise ValueError("缺少 monitor 参数")
                    msg = monitor_blank(mid)
                    _status_cache["ts"] = 0.0
                    _status_cache["obj"] = None
                    self._send(200, {"ok": True, "monitor": mid, "msg": msg})
                elif p == "/monitor/unblank":
                    mid = body.get("monitor")
                    if not mid: raise ValueError("缺少 monitor 参数")
                    msg = monitor_unblank(mid)
                    _status_cache["ts"] = 0.0
                    _status_cache["obj"] = None
                    self._send(200, {"ok": True, "monitor": mid, "msg": msg})
                elif p == "/monitor/rename":
                    mid = body.get("monitor"); label = body.get("label", "")
                    new_label = monitor_rename(mid, label)
                    self._send(200, {"ok": True, "monitor": mid, "label": new_label})
                elif p == "/monitor/scale":
                    mid = body.get("monitor"); scale = int(body.get("scale", 100))
                    info = monitor_scale_set(mid, scale)
                    _patch_status_cache(mid, scale=scale)
                    self._send(200, {"ok": True, "monitor": mid,
                                     "scale": scale, **info})
                elif p == "/vinput/mouse":
                    act = body.get("action", "move")
                    mid = body.get("monitor")
                    if act == "move":
                        if body.get("nx") is not None and body.get("ny") is not None:
                            res = vinput_mouse_move(mid, body.get("nx"), body.get("ny"))
                        else:
                            res = vinput_mouse_delta(mid, body.get("dx", 0), body.get("dy", 0))
                        self._send(200, {"ok": True, "monitor": mid, "pos": vinput_mouse_pos(mid)})
                    elif act == "drag":
                        vinput_mouse_drag(mid, body.get("x1", 0), body.get("y1", 0),
                                          body.get("x2", 0), body.get("y2", 0),
                                          button=body.get("button", "left"))
                        self._send(200, {"ok": True, "monitor": mid})
                    else:  # click/down/up/scroll
                        vinput_mouse_button(mid, button=body.get("button", "left"),
                                            action=body.get("action", "click"),
                                            times=body.get("times", 1),
                                            delta=body.get("delta"))
                        self._send(200, {"ok": True, "monitor": mid})
                elif p == "/vinput/key":
                    vinput_key_press(mods=body.get("mods"), key=body.get("key"))
                    self._send(200, {"ok": True})
                elif p == "/vinput/text":
                    vinput_type_text(body.get("text"))
                    self._send(200, {"ok": True})
                elif p == "/vinput/pos":
                    self._send(200, {"ok": True, "pos": vinput_mouse_pos(body.get("monitor"))})
                elif p == "/vinput/wheellines":
                    self._send(200, {"ok": True,
                                     "wheel_lines": wheel_lines_set(body.get("lines", 3))})
                elif p == "/monitor/tvswitch":
                    mid = body.get("monitor")
                    target = body.get("target", "pc")
                    if not mid: raise ValueError("缺少 monitor 参数")
                    res = monitor_tv_switch(mid, target)
                    _patch_status_cache(mid, tv_state=res.get("state"))
                    self._send(200, res)
        except Exception as e:
            self._send(500, {"ok": False, "error": str(e)})


def main():
    host = CFG.get("host", "0.0.0.0")
    port = int(CFG.get("port", 8765))
    srv = ThreadingHTTPServer((host, port), Handler)
    print("PC Control Agent v2 启动: http://%s:%d  (token=%s)" %
          (host, port, "set" if TOKEN else "none"))

    # 后台预热 + 定时刷新：填满并持续更新显示器枚举/状态缓存，
    # 使任何请求都命中缓存秒回，慢速 DDC/WMI 枚举在后台进行、不阻塞响应。
    def _refresh_loop():
        try:
            _enum_monitors()
            _status(force=True)
        except Exception as e:
            print("[warn] 预热失败:", e, file=sys.stderr)
        while True:
            time.sleep(STATUS_REFRESH_INTERVAL)
            try:
                _status(force=True)
            except Exception as e:
                print("[warn] 状态刷新失败:", e, file=sys.stderr)

    threading.Thread(target=_refresh_loop, daemon=True).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
