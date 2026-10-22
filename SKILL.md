---
name: cdn-edge-probe
description: 诊断并修复「网站首页能打开，但某个子域名、静态资源或验证码脚本加载失败」这类局部故障。用三态 TLS 探测区分 SNI 域名级、IP 段级、DNS 解析异常三类成因，对多边缘 CDN 站点扫描并选用可达边缘 IP 写入 hosts。触发词：某个资源加载失败、验证码加载不出来、Please enable JS、子域名 403、部分 IP 能连部分不能、CloudFront、CDN 优选 IP、TLS 握手超时。
license: MIT
---

# 局部资源加载失败的定位与修复

## 这个技能解决什么

**不是**「整个站点完全连不上」——那是整站可达性问题，本方法不适用。
**是**「站点主体能访问，但某一个环节断了」——典型症状：

- 首页 200，但 `/signup`、`/login` 或某个子域名返回 403
- 页面显示 `Please enable JS and disable any ad blocker`，但 JS 明明是开着的
- 注册/登录按钮点不动、验证码区域空白、一直转圈
- 浏览器控制台有一两个第三方域名加载失败

这类问题的正确判断顺序**很短**，但方向走错会耽误几个小时。下面是踩坑后的最短路径。

---

## 依赖

三个脚本全部使用 Python 标准库（`socket` / `ssl` / `concurrent.futures` / `argparse`），**无需 pip 安装任何依赖**，Python 3.8+ 即可。

```bash
SKILL_DIR="<本仓库路径>"    # 例如 ~/skills/cdn-edge-probe
PY=python                   # 换成你的解释器路径
```

---

## 最短路径（三步）

### 第 1 步：把失败响应的**原文**读出来

**绝对不要只盯状态码猜原因。** 这是本方法最重要的经验。

```bash
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
curl -s -m 20 -A "$UA" -o page.html -w "HTTP=%{http_code} size=%{size_download}\n" "https://target.example/失败路径"
head -c 800 page.html
```

看什么：

- 响应体是否异常小（几百字节 ~ 1KB）→ 大概率是**拦截页/挑战页**，不是正常业务响应
- 里面出现的**第三方域名**（`captcha-delivery.com`、`datadome`、`cloudflare`、`hcaptcha`、`recaptcha` 等）→ 那才是真正断掉的环节
- 关键字 `Please enable JS`、`blocked`、`captcha`、`challenge`

> 真实案例：一个 770 字节的响应体里，除了状态码几乎没有内容，但明确写着反机器人服务（DataDome）的挑战域名和 `Please enable JS`。
> 因为没读它，先误判成「IP 被拉黑」，绕了两轮弯路。

### 第 2 步：三态 SNI 探测，判定断在哪一层

对每个候选 IP 做三次 TLS 握手：**真 SNI** / **不发 SNI** / **伪 SNI**。

```bash
"$PY" "$SKILL_DIR/scripts/probe_tls.py" <目标域名>
"$PY" "$SKILL_DIR/scripts/probe_tls.py" <目标域名> <候选IP1> <候选IP2>
"$PY" "$SKILL_DIR/scripts/probe_tls.py" --batch hosts.txt --quiet
```

判定矩阵：

| 真 SNI | 无 SNI / 伪 SNI | 判定 | 换 IP 有用吗 |
|---|---|---|---|
| 成功 | — | **可达**，无需处理 | — |
| 失败 | 成功 | **SNI 域名级拦截** | 短期有效，长期需本地反代改写 SNI |
| 失败 | 失败 | **IP 段级不可达** | **有用**，换同 CDN 其他边缘 IP |
| 解析结果指向明显无关网段 | — | **DNS 解析异常** | 无用，连真 IP 都拿不到 |

**DNS 解析异常的识别**：解析结果指向明显不属于该站点的网段。脚本内置了常见异常段：

```
104.244.42.*  Twitter/X       31.13.* / 157.240.*  Facebook/Meta
128.242.*     Verizon          199.16.156.*        Twitter/X 旧段
243.185.187.* 经典黑洞地址      59.24.3.173 / 8.7.198.* / 46.82.174.* / 78.16.49.* / 93.46.8.89
```

`185.199.*`（GitHub Pages）只是提示，不算异常。

> **注意**：CloudFront 等 CDN 要求合法 SNI，「伪 SNI」必然失败，可能造成假阴性。
> **以「真 SNI 是否成功」为主判据**，无 SNI / 伪 SNI 结果只用于区分「域名级」还是「IP 段级」。

### 第 3 步：按判定结果处理

#### 情况 A：IP 段级不可达 → 换同 CDN 的其他边缘 IP

前提：目标是**多边缘 CDN**（CloudFront / Akamai / Fastly / Cloudflare）。同一家 CDN 在全球有大量节点，可达性往往不一致。

```bash
# 扫 CloudFront 段采样，找能通 TLS 的 IP
"$PY" "$SKILL_DIR/scripts/scan_ips.py" --domain target.example --preset cloudfront --count 90

# 顺便验证能不能真取到内容（推荐）
"$PY" "$SKILL_DIR/scripts/scan_ips.py" --domain target.example --preset seed --http /要取的路径

# 自定义候选
"$PY" "$SKILL_DIR/scripts/scan_ips.py" --domain target.example --ips ips.txt
```

拿到可达 IP 后写入 hosts（**必须提权**）：

```bash
"$PY" "$SKILL_DIR/scripts/fix_hosts.py" --set <可用IP>:<目标域名>
```

Windows 下从 PowerShell 提权执行（注意路径要写全）：

```powershell
Start-Process -FilePath "python.exe" `
  -ArgumentList "$env:USERPROFILE\skills\cdn-edge-probe\scripts\fix_hosts.py","--set","203.0.113.10:target.example" `
  -Verb RunAs -Wait -WindowStyle Hidden
```

部分终端（包括某些 Agent 的 PowerShell 回显）拿不到标准输出，跑完直接读日志文件确认结果：

```
%TEMP%\fix_hosts.log
```

回滚：`fix_hosts.py --revert`（只删本脚本的标记块，不误伤其他工具写入的段落）。

> `fix_hosts.py` 的设计要点：START/END 注释包裹可精确回滚；幂等，重复执行先清旧块再写新块；每次修改前自动备份到 `%TEMP%\hosts.bak_<时间戳>`。

#### 情况 B：SNI 域名级拦截

换 IP 只能短期有效。可持续的做法需要**本地反向代理并改写 SNI**
（hosts 指到 `127.0.0.1` + 本地 443 监听 + 自签证书 + 用别的 SNI 连真实服务器）。

成本与维护负担都不低。实践上更稳妥的选择是：确认该域名的访问策略是否由所在网络统一管控，按网络管理方的规定处理。

#### 情况 C：DNS 解析异常

连真 IP 都拿不到，**优选 IP 无从谈起**。可选：

1. 用 DoH/DoT 取真实 IP（注意 DoH 端点本身也可能解析异常，需先验证）
2. 从其他渠道获得真实 IP 后，再按情况 A / B 处理
3. 按所在网络的访问策略处理

#### 情况 D：可达

不用处理。如果仍打不开，问题在别处（缓存、插件、账号状态、前端脚本报错）。

---

## 常见干扰源：已安装的代理工具

如果你的机器上装过**本地反向代理类工具**（VPN 客户端、游戏加速器、hosts 管理工具等），它们会同时从两个方向污染诊断结果：

1. **诊断失真**——它们改写了 hosts，你 `curl` 拿到的 IP 已经不是 DNS 给的那个
2. **证书报错**——它们通常安装自签根证书到 `Cert:\LocalMachine\Root`，浏览器不查私有 CA 的吊销状态所以没事，**但命令行工具会挂**

排查前的第一步：先确认这类工具是否在运行、hosts 里是否有非系统写入的段落。

```bash
"$PY" "$SKILL_DIR/scripts/fix_hosts.py" --show
```

### 附带的证书问题及解法

这类工具装的自签根证书会引发两处典型报错。

**curl**：

```
curl: (35) schannel: ... CRYPT_E_NO_REVOCATION_CHECK (0x80092012)
```

解法：加 `--ssl-no-revoke`。

**git (HTTPS)**：`git config --global http.schannelCheckRevoke false` 在部分版本不生效；
切 `http.sslBackend=openssl` 又会变成 `unable to get local issuer certificate (20)`——因为 OpenSSL 后端不认 Windows 证书库。

可行解法：复制系统 CA 包 → 追加该工具的证书 → 让 git 指过去。

```bash
CERT="<工具目录>/xxx.Certificate.cer"
OUT="$HOME/.ssh/ca-bundle-local.pem"
cp /mingw64/etc/ssl/certs/ca-bundle.crt "$OUT"
openssl x509 -inform DER -in "$CERT" -out /tmp/local.pem
cat /tmp/local.pem >> "$OUT"
git config --global http.sslBackend openssl
git config --global http.sslCAInfo "$OUT"
```

**必须保留原有公共根证书**（例如 150 条 → 151 条），否则其他站点会一起挂。

**SSH 走 443 端口**（不依赖任何代理，最稳）：

```
Host github.com
  HostName ssh.github.com
  Port 443
  User git
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
```

`ssh -T git@github.com` 返回 `Permission denied (publickey)` = **通道已通**，只是公钥还没加到服务端。

---

## 排错清单（按易错程度排序）

1. **没读响应原文就下结论** —— 最常见、最费时。先 `-o` 存文件再 `head`。
2. **只看 DNS 返回的几个 IP 就断定整段不可达** —— CDN 有几十上百个边缘 IP，必须扫。
3. **用伪 SNI 的失败结果当判据** —— CDN 要求合法 SNI，会假阴性。
4. **改了 hosts 却看不到效果** —— `nslookup` / `ping` 不走 hosts 缓存，用 `curl` 看实际解析到的 IP。
5. **hosts 写不进去** —— 必须提权；非管理员会报「访问被拒绝」。
6. **反复刷同一个端点** —— 有速率限制，刷多了会封更久。
7. **以为换出口网络一定有用** —— 只对 IP 段级 / DNS 解析异常有意义；域名级问题换网络无效。
8. **以为换虚拟机有用** —— VMware NAT 走宿主机同一张网卡，出口完全一样。除非给虚拟机独立出口（USB 网卡直通 / 桥接另一网卡）。
9. **忘了检查本地代理工具** —— 它改写过 hosts 或装了自签证书时，所有诊断结论都不可信。

---

## 一句话总结

> **先读原文找到真正断掉的域名 → 三态探测判断断在哪一层 → CDN 站点换边缘 IP，其余按网络策略处理。**

---

## 免责声明

本项目仅用于**诊断和修复自身网络环境下单个域名的可达性问题**，目标是让正常的业务请求（如网页注册流程中的验证码脚本）能够加载。

- 修改 hosts 需要管理员权限，请先 `--show` 确认当前状态，改动前脚本会自动备份
- 请遵守所在网络的管理规定和当地法律法规
- 作者不对使用本工具产生的任何后果负责

MIT License.
