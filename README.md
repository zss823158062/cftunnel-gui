# Cloudflare Tunnel Launcher (Tauri Edition)

> 基于 **Tauri 2.0 + Vue 3 + TypeScript + Rust** 构建的高性能、极速零黑窗 Cloudflare Tunnel 桌面客户端。

[![Tauri](https://img.shields.io/badge/Tauri-2.0-blue.svg)](https://tauri.app/)
[![Vue](https://img.shields.io/badge/Vue-3.5-emerald.svg)](https://vuejs.org/)
[![Rust](https://img.shields.io/badge/Rust-1.80%2B-orange.svg)](https://www.rust-lang.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)]()

基于 Cloudflare Tunnel 的免费 HTTPS 内网穿透 GUI 启动器。零环境依赖（无需 Python 运行库）、秒级启动、零黑窗、完全便携，一键把本地端口安全暴露到公网 HTTPS。

---

## 🌟 核心特性

- 🚀 **原生极速架构**：基于 Tauri 2.0 与 Rust 构建底层进程调度管道，启动极快，内存占用小（仅约 30MB）。
- 🪟 **零黑窗口 & 实时管道流**：底层捕获 `cloudflared` 的 stdout/stderr 异步无缝重定向，彩色高亮渲染终端日志，支持环形缓冲与自动滚动。
- 🧭 **智能初始化向导**：
  - ① **Login**：点击后拉起系统默认浏览器进行 Cloudflare 授权，自动检测证书并归档至 `data/`。
  - ② **创建 Tunnel**：根据设定的 Tunnel 名称一键创建，自动提取并绑定 UUID 凭证。
  - ③ **绑定 DNS**：逐个为配置的子域名添加 CNAME 记录 指向当前 Tunnel。
  - ⚡ **一键执行全部**：智能检测当前缺失的步骤并顺序执行。
- 🎯 **多域名/端口映射与精准连接跟踪**：
  - 支持添加、编辑、删除、上下移动多条 `hostname -> localhost:port` 映射配置。
  - 自动维护 `connIndex`（0..3）连接集合，实时反馈连接状态灯与连接数（如 `4/4`）。
- 📦 **完全可移植**：整个应用及 `data/` 目录可置于任意目录或 U 盘运行，配置与凭证全部保存在 `data/` 目录中。
- 🛡️ **优雅进程守护**：关闭窗口或点击停止时，自动向子进程发送终止信号并清理残留进程，避免后台孤儿进程泄露。

---

## 📁 目录结构

```
cf_tunnel/
├── cloudflared.exe         # Cloudflare 官方客户端
├── data/                   # 本地运行数据与凭证（已被 .gitignore 排除）
│   ├── cert.pem            # 登录凭据
│   ├── <UUID>.json         # Tunnel 密钥文件
│   ├── tunnel-id.txt       # Tunnel UUID 缓存
│   ├── gui-settings.json   # 用户表单配置持久化
│   └── config.runtime.yml  # 运行时生成的配置文件
├── src/                    # Vue 3 + TypeScript 前端界面
│   ├── App.vue             # 主桌面窗口组件（运行监控与初始化向导）
│   ├── types.ts            # 前端数据接口定义
│   └── main.ts             # 前端入口
├── src-tauri/              # Rust 后端
│   ├── src/
│   │   ├── commands.rs     # Tauri Invoke 业务指令集
│   │   ├── state.rs        # 进程与配置状态模型
│   │   ├── lib.rs          # 插件与窗口生命周期管理
│   │   └── main.rs         # 入口函数
│   ├── Cargo.toml          # Rust 依赖声明
│   └── tauri.conf.json     # Tauri 2 应用配置
├── run.bat                 # 双击直接启动（已编译则秒开，未编译则自动拉起开发模式）
├── run-debug.bat           # 带控制台日志的开发模式启动
└── package.json
```

---

## 🛠️ 开发与构建

### 1. 安装前端依赖

```bash
pnpm install
```

### 2. 本地开发与调试

```bash
pnpm tauri dev
```
或双击运行 `run-debug.bat`。

### 3. 构建 Release 正式安装包

```bash
pnpm tauri build
```
编译产物将生成在 `src-tauri/target/release/`：
- 单文件便携版可执行文件：`cftunnel-gui.exe`
- 安装包：`bundle/msi/` 与 `bundle/nsis/`

---

## 💡 代理冲突说明

如使用 **Clash / v2rayN** 等带 TUN 模式的代理工具，必须放行 cloudflared，否则可能被 fake-ip 劫持导致状态灯一直为黄色：

**Clash Verge 规则片段**：
```yaml
- PROCESS-NAME,cloudflared.exe,DIRECT
- DOMAIN-SUFFIX,argotunnel.com,DIRECT
- DOMAIN-SUFFIX,cloudflareaccess.com,DIRECT
- DOMAIN-SUFFIX,cftunnel.com,DIRECT
```

---

## 📄 License

[MIT](LICENSE) © 2026 zss823158062
