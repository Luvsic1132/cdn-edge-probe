# cdn-edge-probe

诊断「网站主体能打开，但某个子域名 / 静态资源 / 验证码脚本加载不出来」这一类局部故障。

不是整站连不上——那是另一回事。这里处理的是**部分可达**：主体 HTTP 200，偏偏某一个环节断了。

```
首页 200，/signup 却 403
页面显示 Please enable JS，但 JS 明明开着
验证码区域一直空白 / 转圈
控制台里有一两个第三方域名加载失败
某些 IP 能连、某些不能，同一域名
```

## 核心思路

大部分人在这一步走错方向，然后花几小时。正确的顺序只有四步：

**0. 先看 hosts 里有没有指向 `127.0.0.1` 的残留。** 加速器类工具退出后常留下这类映射，而它们的本地服务早就不跑了——域名全部变成黑洞。症状是「页面框架能开、图片头像全碎」，看起来像整个域名不可达，实际上是自己机器把域名指进了死胡同。30 秒能排除，比后面三步加起来都省时间。

**1. 先把失败响应的原文读出来。** 不要只盯状态码。响应体只有几百字节、里面写着反机器人服务的域名，那就不是「IP 被封」而是「挑战脚本加载失败了」——两个完全不同的原因，修法也不同。

**2. 三态 TLS 探测，判断断在哪一层。** 对每个候选 IP 做三次握手：带真 SNI、不带 SNI、带伪 SNI。

| 真 SNI | 无 SNI / 伪 SNI | 判定 | 换 IP 有用吗 |
|---|---|---|---|
| 成功 | — | 可达 | — |
| 失败 | 成功 | SNI 域名级拦截 | 短期有效，长期需反代改写 SNI |
| 失败 | 失败 | IP 段级不可达 | **有用**，换同 CDN 其他边缘 IP |
| 解析指向无关网段 | — | DNS 解析异常 | 无用，连真 IP 都拿不到 |

**3. 按判定处理。** 只有「IP 段级不可达 + 多边缘 CDN」这一种情况，换边缘 IP 才有意义。其余情况换 IP 是白费功夫。

## 快速开始

纯 Python 标准库，3.8+，无需安装依赖。

```bash
git clone https://github.com/Luvsic1132/cdn-edge-probe.git
cd cdn-edge-probe

# 第 1 步：探测，看断在哪一层
python scripts/probe_tls.py target.example

# 第 2 步：如果判定为 IP 段级不可达，扫同 CDN 其他边缘 IP
python scripts/scan_ips.py --domain target.example --preset cloudfront --count 90

# 顺便验证能不能真取到内容
python scripts/scan_ips.py --domain target.example --preset seed --http /c.js

# 第 3 步：写入 hosts（需要管理员权限）
python scripts/fix_hosts.py --set 203.0.113.10:target.example

# 查看 / 回滚
python scripts/fix_hosts.py --show
python scripts/fix_hosts.py --revert
```

Windows 下提权执行：

```powershell
Start-Process -FilePath "python.exe" `
  -ArgumentList ".\scripts\fix_hosts.py","--set","203.0.113.10:target.example" `
  -Verb RunAs -Wait -WindowStyle Hidden
# 部分终端拿不到 stdout，跑完读 %TEMP%\fix_hosts.log
```

## 三个脚本

| 脚本 | 作用 |
|---|---|
| `probe_tls.py` | 三态 SNI 探测，输出每个 IP 的判定结果；支持 `--batch` 批量 |
| `scan_ips.py` | 按 CDN 网段采样大量边缘 IP，筛出能完成 TLS 握手的那些；可选 `--http` 验证真实内容 |
| `fix_hosts.py` | hosts 定点写入 / 查看 / 回滚。用 START/END 标记包裹，只删自己写的块；改前自动备份到 `%TEMP%` |

## 局限

诚实地说清楚这个方法的边界：

- **只对多边缘 CDN 有效。** CloudFront、Akamai、Fastly、Cloudflare 这类在全球有大量节点的服务，可达性不一致才有「优选」的空间。
- **整段不可达和 DNS 解析异常，换 IP 完全无用。** 这两种情况得先拿到真实 IP，或者按所在网络的访问策略处理。
- **不是通用代理方案。** 它不改变出口，只改变「连接到哪个边缘节点」。需要改出口的场景，本工具帮不上忙。
- **优选 IP 会失效。** CDN 节点状态随时变化，今天能用的 IP 几周后可能不行，需要重新扫。

## 作为 Agent Skill 使用

`SKILL.md` 同时是一份可直接加载的 skill 定义（带 YAML frontmatter）。放进你的 skills 目录即可被 Claude Code / 同类 Agent 自动索引：

```bash
git clone https://github.com/Luvsic1132/cdn-edge-probe.git ~/.claude/skills/cdn-edge-probe
```

## 免责声明

本项目仅用于诊断和修复**自身网络环境下单个域名的可达性问题**，目标是让正常业务请求（如注册流程中的验证码脚本）能正常加载。

- 修改 hosts 需要管理员权限，请先 `--show` 确认当前状态；脚本改动前会自动备份
- 请遵守所在网络的管理规定和当地法律法规
- 作者不对使用本工具产生的任何后果负责

## License

[MIT](LICENSE)
