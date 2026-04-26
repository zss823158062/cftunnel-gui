# cftunnel-gui

> A zero-config Windows GUI launcher for Cloudflare Tunnel — one-click HTTPS for localhost.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)]()

基于 Cloudflare Tunnel 的免费 HTTPS 内网穿透 GUI 启动器。零配置、零黑窗、零 C 盘污染，一键把本地端口暴露到公网 HTTPS。

## 特性

- 🪟 **零黑窗口**：所有 cloudflared 输出集中在 GUI 日志区
- ⚡ **日志不卡顿**：子线程异步读取，环形缓冲 2000 行
- 🧭 **内置初始化向导**：login / create / route dns 三步在 GUI 里完成，无需命令行
- 📦 **完全可移植**：整个目录可丢到任意盘符/U 盘，所有数据跟随程序
- 🎯 **精确状态指示**：按 `connIndex` 跟踪每条隧道，状态灯不漂移
- 🔐 **敏感凭证隔离**：全部落在 `data/`，`.gitignore` 已排除

## 截图

（可在此处添加 GUI 截图）

## 环境要求

- Windows 10/11
- Python 3.10+（自带 tkinter，零第三方依赖）
- Cloudflare 账号 + 一个已托管在 Cloudflare 的域名

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/zss823158062/cftunnel-gui.git
cd cftunnel-gui
```

### 2. 下载 cloudflared

本仓库不包含 `cloudflared.exe`（62MB，通过 .gitignore 排除）。自行下载到项目根目录：

```
https://github.com/cloudflare/cloudflared/releases/latest
```

选择 `cloudflared-windows-amd64.exe`，重命名为 `cloudflared.exe`。

### 3. 启动 GUI

双击 `run.bat`。

### 4. 首次初始化

在「**初始化向导**」标签页：

1. 填写 **Tunnel 名称**、**子域名**、**本地端口** → 点「保存参数」
2. 点「**一键执行全部**」：
   - ① Login（弹浏览器授权）
   - ② 创建 Tunnel
   - ③ 绑定 DNS

### 5. 日常启动

切到「**运行**」标签页 → 点「**启动 Tunnel**」。绿灯亮起时访问你的子域名即可。

## 目录结构

```
cftunnel-gui/
├── cloudflared.exe         # 自行下载（不入库）
├── tunnel_gui.py           # GUI 主程序
├── run.bat                 # 无黑窗口启动
├── run-debug.bat           # 带控制台启动（排错用）
├── LICENSE
├── README.md
└── data/                   # 运行数据（不入库）
    ├── cert.pem            # 登录凭证
    ├── <UUID>.json         # Tunnel 凭证
    ├── tunnel-id.txt       # UUID 缓存
    ├── gui-settings.json   # 表单参数持久化
    └── config.runtime.yml  # 运行时生成的配置
```

## 代理冲突（重要）

如使用 **Clash / v2rayN** 等带 TUN 模式的代理工具，必须放行 cloudflared，否则会被 fake-ip 劫持：

**Clash Verge 规则片段**：

```yaml
- PROCESS-NAME,cloudflared.exe,DIRECT
- DOMAIN-SUFFIX,argotunnel.com,DIRECT
- DOMAIN-SUFFIX,cloudflareaccess.com,DIRECT
- DOMAIN-SUFFIX,cftunnel.com,DIRECT
```

加完后需**重启 Clash 内核**或重新激活 profile。验证：

```bash
nslookup region1.v2.argotunnel.com
```

返回 `198.41.x.x` 开头即生效。若仍为 `198.18.x.x` 说明规则未加载。

## 故障排查

| 现象 | 原因 / 解决 |
|------|------|
| GUI 打不开 | 用 `run-debug.bat` 查看 Python 异常 |
| 状态灯一直黄 | 代理 fake-ip 劫持，参见上方 |
| 访问返回 502 | 本地端口服务未启动 / 被防火墙拦截 |
| DNS 绑定报 1003 | 子域名已有冲突记录，到 Cloudflare 后台删除 |
| 域名含下划线报错 | DNS 规范不允许，改用连字符 |

## 技术栈

- **GUI**：tkinter（Python 标准库）
- **后端**：Cloudflare 官方 `cloudflared`
- **协议**：QUIC（优先）/ HTTP/2

## 设计要点

- `CREATE_NO_WINDOW` 启动子进程，无控制台窗口
- 子线程读 stdout → Queue → GUI 80ms 轮询渲染
- `CTRL_BREAK_EVENT` 优雅停止 cloudflared，4 秒超时兜底强杀
- 按 `connIndex` 维护连接集合，避免状态计数漂移
- `BASE_DIR = Path(__file__).parent`，路径完全相对

## License

[MIT](LICENSE) © 2026 zss823158062

## 相关链接

- [Cloudflare Tunnel 官方文档](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
- [cloudflared Releases](https://github.com/cloudflare/cloudflared/releases)
