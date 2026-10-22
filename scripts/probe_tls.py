#!/usr/bin/env python3
"""三态 TLS 探测：区分「IP 段级不可达 / SNI 域名级拦截 / DNS 解析异常 / 可达」。

用法：
    python probe_tls.py <域名> [IP ...]
    python probe_tls.py target.example
    python probe_tls.py target.example 203.0.113.10 203.0.113.20
    python probe_tls.py --batch hosts.txt      # 每行一个域名
    python probe_tls.py --batch hosts.txt --quiet

判定逻辑（对每个 IP 做三次握手）：
    真 SNI 成功                          -> 可达
    真 SNI 失败，但不发 SNI / 伪 SNI 成功 -> SNI 域名级拦截（换 IP 短期有效，需反代配合）
    三次全失败                            -> IP 段级不可达（换 IP 才有用）
    DNS 解析到明显异常 IP（见 SUSPICIOUS）-> 解析异常，换 IP 无从谈起

注意：CloudFront 等 CDN 要求合法 SNI，此时「伪 SNI」会假阴性，
      以「真 SNI 是否成功」为主判据，不要被伪 SNI 结果误导。
"""
import socket
import ssl
import sys
import concurrent.futures as cf

SUSPICIOUS = {
    # 常见异常解析指向段 / 已知错误映射
    "104.244.42.": "Twitter/X",
    "31.13.": "Facebook/Meta",
    "157.240.": "Facebook/Meta",
    "128.242.": "Verizon/Edgecast",
    "199.16.156.": "Twitter/X 旧段",
    "185.199.": "GitHub Pages（只是提示，非异常）",
    "243.185.187.": "经典黑洞地址",
    "59.24.3.173": "经典错误映射地址",
    "8.7.198.": "经典错误映射地址",
    "46.82.174.": "经典错误映射地址",
    "78.16.49.": "经典错误映射地址",
    "93.46.8.89": "经典错误映射地址",
    "10.10.10.": "私有/黑洞",
}

# 这些段出现时不算异常，仅提示（避免误报）
SUSPICIOUS_OK = ("185.199.",)

RESET_MARKS = ("ConnectionReset", "ResetError", "WinError 10054", "BrokenPipe")
TIMEOUT_MARKS = ("timeout", "Timeout", "timed out", "WinError 10060")


def classify_error(exc):
    name = type(exc).__name__
    msg = str(exc)
    for m in RESET_MARKS:
        if m in name or m in msg:
            return "RST"
    for m in TIMEOUT_MARKS:
        if m in name or m in msg:
            return "TIMEOUT"
    return name


def tls_probe(ip, sni, timeout=6.0):
    """返回 (ok: bool, detail: str)"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        raw = socket.create_connection((ip, 443), timeout=timeout)
    except Exception as e:
        return False, "TCP_" + classify_error(e)
    try:
        sock = ctx.wrap_socket(raw, server_hostname=sni) if sni else ctx.wrap_socket(raw)
        sock.close()
        return True, "OK"
    except Exception as e:
        return False, "TLS_" + classify_error(e)


def full_probe(ip, host, timeout=6.0):
    ok_real, d_real = tls_probe(ip, host, timeout)
    ok_none, d_none = tls_probe(ip, None, timeout)
    ok_fake, d_fake = tls_probe(ip, "example.com", timeout)

    if ok_real:
        verdict = "可达"
    elif ok_none or ok_fake:
        verdict = "SNI域名级拦截"
    else:
        verdict = "IP段级不可达"
    return {
        "ip": ip, "real": d_real, "none": d_none, "fake": d_fake,
        "verdict": verdict, "usable": ok_real,
    }


def resolve(host):
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        seen, out = set(), []
        for info in infos:
            ip = info[4][0]
            if ip not in seen:
                seen.add(ip)
                out.append(ip)
        return out
    except Exception:
        return []


def suspicion(ip):
    for prefix, owner in SUSPICIOUS.items():
        if ip.startswith(prefix):
            if any(ip.startswith(ok) for ok in SUSPICIOUS_OK):
                return None
            return owner
    return None


def report_host(host, extra_ips=None, quiet=False):
    ips = resolve(host)
    if extra_ips:
        for ip in extra_ips:
            if ip not in ips:
                ips.append(ip)
    if not ips:
        return [f"{host:34s} DNS 解析失败（域名不存在或解析异常）"]

    lines = []
    if not quiet:
        lines.append("")
        lines.append("=" * 76)
        lines.append(f"域名: {host}")
        lines.append("=" * 76)

    for ip in ips:
        note = suspicion(ip)
        r = full_probe(ip, host)
        flag = ""
        if note:
            flag = f"  <- 解析异常疑似({note})"
        elif r["usable"]:
            flag = "  <- 可用"
        lines.append(
            f"  {ip:18s} 真SNI:{r['real']:12s} 无SNI:{r['none']:12s} "
            f"伪SNI:{r['fake']:12s} => {r['verdict']}{flag}"
        )

    good = []
    bad_all = True
    for ip in ips:
        if full_probe(ip, host)["usable"]:
            good.append(ip)
            bad_all = False

    if good:
        lines.append(f"  >> 可用 IP: {', '.join(good)}")
        lines.append("  >> 下一步: scan_ips.py 扫更多边缘 IP，或直接 fix_hosts.py --set 写入")
    elif bad_all:
        poisoned = [ip for ip in ips if suspicion(ip)]
        lines.append("")
        if poisoned:
            lines.append(f"  !! 解析结果明显不属于该站点（{', '.join(poisoned)}）→ 判定 DNS 解析异常")
            lines.append("  !! 换 IP 无从谈起，必须先拿到真实 IP（DoH 或其它渠道），再按情况 A/B 处理")
        else:
            lines.append("  !! 全部 IP 不通，两种可能：")
            lines.append("     (a) IP 段级不可达 —— 用 scan_ips.py 扫同 CDN 其他段，有救")
            lines.append("     (b) DNS 解析异常给的是假 IP —— 需先从其它渠道获得真实 IP")
            lines.append("     判别法：扫一通若全灭，基本就是 (b)")
    return lines


def batch(path, quiet=False):
    with open(path, encoding="utf-8") as f:
        hosts = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    lines = []
    for h in hosts:
        lines.extend(report_host(h, quiet=quiet))
    return lines


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    quiet = "--quiet" in sys.argv

    if "--batch" in sys.argv:
        if not args:
            print("用法: probe_tls.py --batch <文件>")
            sys.exit(1)
        out = batch(args[0], quiet)
    else:
        if not args:
            print(__doc__)
            sys.exit(1)
        out = report_host(args[0], args[1:] or None, quiet)

    text = "\n".join(out)
    print(text)

    # 同时写文件，便于终端回显不可靠时读取
    try:
        import io
        import os
        import tempfile
        with io.open(os.path.join(tempfile.gettempdir(), "probe_tls_out.txt"),
                     "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


if __name__ == "__main__":
    main()
