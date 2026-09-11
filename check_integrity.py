import ctypes, ctypes.wintypes as wt

kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32
user32 = ctypes.windll.user32

TOKEN_QUERY = 0x0008
TokenIntegrityLevel = 25
ERROR_INSUFFICIENT_BUFFER = 122

def get_integrity(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION (跨 UIPI 可用)
    if not h:
        return -1  # 连打开进程都失败(常被 UIPI 拦截 => 目标很可能是高完整性)
    tok = wt.HANDLE()
    if not advapi32.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
        kernel32.CloseHandle(h); return None
    buf = ctypes.create_string_buffer(1024)
    ret = ctypes.c_ulong()
    if not advapi32.GetTokenInformation(tok, TokenIntegrityLevel, buf, 1024, ctypes.byref(ret)):
        kernel32.CloseHandle(tok); kernel32.CloseHandle(h); return None
    # TOKEN_MANDATORY_LABEL: Sid (first 8 bytes = PSID pointer-ish), then label
    # Parse SID and integrity level (last DWORD of SID's subauthority)
    # buf layout: TOKEN_MANDITORY_LABEL { PSID LabelSid; DWORD Attributes }
    import struct
    label_sid_ptr = struct.unpack_from("<Q", buf, 0)[0] if ctypes.sizeof(ctypes.c_void_p)==8 else struct.unpack_from("<I", buf, 0)[0]
    # SID: rev(1) + cnt(1) + auth[6] + sub[count*4]
    sid_bytes = ctypes.string_at(label_sid_ptr, 12)
    rev, cnt = sid_bytes[0], sid_bytes[1]
    # subauthorities start at offset 8, each 4 bytes; integrity level is LAST subauthority
    subs = (ctypes.string_at(label_sid_ptr+8, cnt*4))
    il = struct.unpack_from("<I", subs, (cnt-1)*4)[0]
    kernel32.CloseHandle(tok)
    kernel32.CloseHandle(h)
    return il

def pid_of(name):
    # use toolhelp or just tasklist via ctypes is heavy; use CreateToolhelp32Snapshot
    TH32CS_SNAPPROCESS = 0x00000002
    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_ulong),("cntUsage",ctypes.c_ulong),
                    ("th32ProcessID",ctypes.c_ulong),("th32DefaultHeapID",ctypes.c_void_p),
                    ("th32ModuleID",ctypes.c_ulong),("cntThreads",ctypes.c_ulong),
                    ("th32ParentProcessID",ctypes.c_ulong),("pcPriClassBase",ctypes.c_long),
                    ("dwFlags",ctypes.c_ulong),("szExeFile",ctypes.c_char*260)]
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    pe = PROCESSENTRY32(); pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
    out = []
    if kernel32.Process32First(snap, ctypes.byref(pe)):
        while True:
            out.append((pe.th32ProcessID, pe.szExeFile.decode('mbcs','replace')))
            if not kernel32.Process32Next(snap, ctypes.byref(pe)):
                break
    kernel32.CloseHandle(snap)
    return out

procs = pid_of("")
# 找疑似 i4Tools / 爱思助手 的进程
targets = [p for p in procs if any(k in p[1].lower() for k in
           ["i4tool","aisma","assistant","apple","itunes","4tools","爱思"])]
# 也把 agent 自己列出来做对照
agent = [p for p in procs if "agent" in p[1].lower() or "python" in p[1].lower()]

IL_MAP = {0x0000:"Untrusted",0x1000:"Low",0x2000:"Medium",0x3000:"Medium+",
          0x4000:"High",0xF000:"System"}

print("=== 疑似 i4Tools / 相关进程 ===")
for pid, name in targets:
    il = get_integrity(pid)
    if il is None or il == -1:
        err = kernel32.GetLastError()
        note = "ACCESS_DENIED(5)=> 高完整性/被 UIPI 拦截" if err == 5 else ("err=%d" % err)
        print("  pid=%d %s -> 无法打开进程 (%s)" % (pid, name, note))
    else:
        print("  pid=%d %s -> 完整性 0x%04X (%s)" % (pid, name, il, IL_MAP.get(il, "??")))

print("=== 对照: python/agent 进程(我们注入端) ===")
seen=set()
for pid, name in agent:
    if pid in seen: continue
    seen.add(pid)
    il = get_integrity(pid)
    s = ("0x%04X (%s)" % (il, IL_MAP.get(il,"??"))) if il is not None else "无法读取"
    print("  pid=%d %s -> %s" % (pid, name, s))
