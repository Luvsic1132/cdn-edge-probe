#!/usr/bin/env python3
"""hosts 定点修复 / 回滚（需管理员权限运行）。

用法（普通权限会失败，必须提权）：
    # 写入
    python fix_hosts.py --set 203.0.113.10:target.example
    python fix_hosts.py --set 203.0.113.10:target.example --set 203.0.113.10:cdn.target.example

    # 查看当前自定义块
    python fix_hosts.py --show

    # 回滚（只删本脚本写的块，不动别人的）
    python fix_hosts.py --revert

提权执行（从 PowerShell 工具）：
    Start-Process -FilePath <python.exe> `
      -ArgumentList "<本脚本路径>","--set","203.0.113.10:target.example" `
      -Verb RunAs -Wait -WindowStyle Hidden

设计要点：
- 用 START/END 注释包裹，便于精确回滚，不误伤 Steam++ 等其他工具写的块
- 幂等：重复执行会先清掉旧块再写新块
- 每次修改前自动备份到 %TEMP%，文件名带时间戳
- 结果写日志到 %TEMP%\\fix_hosts.log（PowerShell 回显不可靠时读这个）
"""
import argparse
import datetime
import io
import os
import shutil
import sys
import tempfile

HOSTS = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                     "System32", "drivers", "etc", "hosts")
LOG = os.path.join(tempfile.gettempdir(), "fix_hosts.log")
MARK_START = "# --- cdn-edge-ip fix start ---"
MARK_END = "# --- cdn-edge-ip fix end ---"

# 兼容性：如果你的机器上曾有其它工具或早期版本写入的标记块需要一并清理，
# 把标记对填到这里，--revert 时会一并移除。默认留空，避免误删别人的段落。
LEGACY_MARKS = [
    # ("# --- custom block start ---", "# --- custom block end ---"),
]


def log(lines):
    text = "\n".join(lines)
    print(text)
    try:
        with io.open(LOG, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


def read_hosts():
    with io.open(HOSTS, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def strip_block(raw):
    """移除本脚本写过的块（含 START/END 两行），并兼容历史标记"""
    markers = [(MARK_START, MARK_END)] + LEGACY_MARKS
    changed = True
    while changed:
        changed = False
        for start, end in markers:
            if start not in raw:
                continue
            before, rest = raw.split(start, 1)
            if end in rest:
                after = rest.split(end, 1)[1]
            else:
                # 没有 END（写入中断），保守：丢掉紧随其后的纯 IP 起始行
                after = "\n".join(ln for ln in rest.splitlines() if not ln[:1].isdigit())
            raw = before.rstrip() + "\r\n" + after.lstrip("\r\n")
            changed = True
    return raw


def backup(raw):
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(tempfile.gettempdir(), "hosts.bak_" + stamp)
    try:
        shutil.copy2(HOSTS, path)
        return path
    except Exception:
        try:
            with io.open(path, "w", encoding="utf-8", errors="replace", newline="") as f:
                f.write(raw)
            return path
        except Exception:
            return None


def write(raw):
    with io.open(HOSTS, "w", encoding="ascii", errors="replace", newline="") as f:
        f.write(raw)


def current_block(raw):
    """返回当前生效的自定义映射行（含历史标记块）"""
    found = []
    for start, end in [(MARK_START, MARK_END)] + LEGACY_MARKS:
        if start not in raw:
            continue
        seg = raw.split(start, 1)[1]
        seg = seg.split(end, 1)[0] if end in seg else seg
        for ln in seg.splitlines():
            ln = ln.strip()
            if ln and ln[:1].isdigit():
                found.append(ln)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", action="append", default=[], metavar="IP:域名",
                    help="要写入的映射，可重复")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args()

    out = []
    try:
        raw = read_hosts()
    except PermissionError:
        log(["错误: 无权限读取 hosts，请以管理员身份运行"])
        return

    if args.show and not args.set and not args.revert:
        blk = current_block(raw)
        out.append("hosts: %s" % HOSTS)
        out.append("总行数: %d" % len(raw.splitlines()))
        out.append("自定义块: %s" % ("有" if blk else "无"))
        for ln in blk:
            out.append("  " + ln)
        log(out)
        return

    bak = backup(raw)
    out.append("备份: %s" % (bak or "失败（本次仍继续）"))

    try:
        if args.revert:
            new = strip_block(raw)
            write(new)
            out.append("已回滚自定义块")
        elif args.set:
            pairs = []
            for item in args.set:
                if ":" not in item:
                    out.append("跳过格式错误的项: %s" % item)
                    continue
                ip, host = item.split(":", 1)
                pairs.append((ip.strip(), host.strip()))
            if not pairs:
                log(out + ["没有有效映射，未修改"])
                return
            new = strip_block(raw).rstrip() + "\r\n\r\n" + MARK_START + "\r\n"
            for ip, host in pairs:
                new += "%s %s\r\n" % (ip, host)
            new += MARK_END + "\r\n"
            write(new)
            out.append("已写入 %d 条映射" % len(pairs))
        else:
            log(["未指定操作，用 --set / --show / --revert"])
            return
    except PermissionError:
        log(out + ["错误: 无权限写入 hosts —— 必须以管理员身份运行本脚本"])
        return

    chk = read_hosts()
    out.append("校验: 含自定义块=%s" % (MARK_START in chk))
    out.append("校验: 总行数=%d" % len(chk.splitlines()))
    blk = current_block(chk)
    for ln in blk:
        out.append("  " + ln)
    log(out)


if __name__ == "__main__":
    main()
