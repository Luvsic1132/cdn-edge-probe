#!/usr/bin/env python3
"""CDN 边缘 IP 优选扫描：给定域名，扫候选 IP 找能完成 TLS 握手的那些。

用法：
    # 用内置 CloudFront 段采样扫描（最常用）
    python scan_ips.py --domain target.example --preset cloudfront --count 60

    # 用自带 IP 列表
    python scan_ips.py --domain example.com --ips ips.txt

    # 顺带验证 HTTP 内容（可选，加 --http 会真发 GET 并报告状态码/大小）
    python scan_ips.py --domain target.example --preset cloudfront --http /c.js

原理：同一家 CDN 在全球有很多边缘节点，国内往往只封了其中一部分 IP 段。
      换到未被封的边缘 IP，TLS 能通、内容能取，问题即解。
      （圈内俗称「优选 IP」。仅对「多边缘 CDN」且「仅部分段落不可达」的站点有效。）

重要：必须使用目标域名的真实 SNI —— CloudFront 等 CDN 对无 SNI / 伪 SNI 一律拒绝。
"""
import argparse
import random
import socket
import ssl
import sys
import concurrent.futures as cf

# 公开的 CloudFront 边缘网段（/16 或更粗粒度），用于采样
CLOUDFRONT_PREFIXES = [
    "13.32", "13.35", "13.113", "13.124", "13.224", "13.225", "13.249",
    "18.64", "18.65", "18.160", "18.238", "18.239",
    "52.84", "52.85", "52.46",
    "54.182", "54.192", "54.230", "54.239",
    "64.252", "99.84",
    "108.138", "108.156", "108.157",
    "143.204", "205.251",
    "3.160", "3.161", "3.162", "3.163", "3.164", "3.165",
]

# 优先尝试的已知良好 IP（可作为种子）
SEED_GOOD = [
    "13.32.1.1", "13.35.1.1", "13.225.1.1", "52.85.1.1",
    "54.182.1.1", "54.192.1.1", "54.230.1.1", "108.138.1.1",
    "108.156.1.1", "143.204.1.1", "18.160.1.1",
]


def gen_prefix_candidates(prefixes, per_prefix=3):
    ips = []
    for p in prefixes:
        for _ in range(per_prefix):
            ips.append("%s.%d.%d" % (p, random.randint(1, 254), random.randint(1, 254)))
    return ips


def tls_check(ip, sni, timeout=5.0):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        raw = socket.create_connection((ip, 443), timeout=timeout)
    except Exception:
        return False, "TCP失败"
    try:
        s = ctx.wrap_socket(raw, server_hostname=sni)
        s.close()
        return True, "TLS成功"
    except Exception as e:
        return False, type(e).__name__


def http_check(ip, sni, path, timeout=10.0):
    """用 TLS 手写一个最小 HTTP/1.1 GET，返回 (status, bytes) 或 None"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        raw = socket.create_connection((ip, 443), timeout=timeout)
        s = ctx.wrap_socket(raw, server_hostname=sni)
        req = (
            "GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: Mozilla/5.0\r\n"
            "Accept: */*\r\nConnection: close\r\n\r\n" % (path, sni)
        )
        s.sendall(req.encode())
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
            if sum(len(c) for c in chunks) > 4 * 1024 * 1024:
                break
        s.close()
        data = b"".join(chunks)
        head = data.split(b"\r\n\r\n", 1)
        status = 0
        if head and head[0].startswith(b"HTTP/"):
            try:
                status = int(head[0].split(b" ")[1])
            except Exception:
                status = -1
        body_len = len(head[1]) if len(head) > 1 else 0
        return status, body_len
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True, help="目标域名（同时用作 SNI）")
    ap.add_argument("--preset", default="cloudfront", help="候选 IP 来源：cloudfront / seed")
    ap.add_argument("--count", type=int, default=60, help="每个段采样的 IP 数")
    ap.add_argument("--ips", help="自定义候选 IP 文件，每行一个")
    ap.add_argument("--http", help="额外验证的 URL 路径，如 /c.js")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    candidate = []
    if args.ips:
        with open(args.ips, encoding="utf-8") as f:
            candidate = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    elif args.preset == "seed":
        candidate = list(SEED_GOOD)
    else:
        per = max(1, args.count // len(CLOUDFRONT_PREFIXES))
        candidate = list(SEED_GOOD) + gen_prefix_candidates(CLOUDFRONT_PREFIXES, per)
        seen, uniq = set(), []
        for ip in candidate:
            if ip not in seen:
                seen.add(ip)
                uniq.append(ip)
        candidate = uniq

    if not args.quiet:
        print(f"目标域名(SNI): {args.domain}")
        print(f"候选 IP 数量: {len(candidate)}")
        print("扫描中...\n")

    def work(ip):
        ok, detail = tls_check(ip, args.domain)
        extra = ""
        if ok and args.http:
            r = http_check(ip, args.domain, args.http)
            if r:
                extra = "  HTTP=%s size=%s" % r
            else:
                extra = "  HTTP失败"
        return ip, ok, detail, extra

    good = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for ip, ok, detail, extra in ex.map(work, candidate):
            if ok:
                good.append(ip)
                if not args.quiet:
                    print(f"  [可 用] {ip:18s} {detail}{extra}")

    print()
    print("=" * 70)
    print(f"可用 IP: {len(good)} / {len(candidate)}")
    if good:
        print("清单（可直接填 hosts）:")
        for ip in good:
            print(f"    {ip:18s} {args.domain}")
        print()
        print("示例 hosts 行:")
        print(f"    {good[0]} {args.domain}")
    else:
        print("没有可用 IP —— 说明该站属于「IP 段级不可达」或「DNS 解析异常」，换 IP 无效，需按所在网络策略处理。")

    text_out = "\n".join(good)
    try:
        import tempfile
        import os
        path = os.path.join(tempfile.gettempdir(), "scan_ips_good.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text_out)
        print(f"\n可用 IP 已写入: {path}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
