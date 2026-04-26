"""
Cloudflare Tunnel GUI - All-in-One
集成 login / create / route dns / run 四步，零黑窗口，实时日志。
"""
import os
import re
import sys
import json
import queue
import shutil
import signal
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

BASE_DIR = Path(__file__).resolve().parent
CLOUDFLARED = BASE_DIR / "cloudflared.exe"
DATA_DIR = BASE_DIR / "data"
CERT = DATA_DIR / "cert.pem"
TUNNEL_ID_FILE = DATA_DIR / "tunnel-id.txt"
CONFIG_RUNTIME = DATA_DIR / "config.runtime.yml"
SETTINGS_FILE = DATA_DIR / "gui-settings.json"

DEFAULT_TUNNEL_NAME = "cf-tunnel-senx"
DEFAULT_HOSTNAME = "cf-tunnel.senx.de5.net"
DEFAULT_PORT = 8080

MAX_LOG_LINES = 2000
POLL_MS = 80
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
NEW_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_settings(d: dict):
    DATA_DIR.mkdir(exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


class TunnelApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.reader_thread: threading.Thread | None = None
        self.connections = 0
        self._connected_idx: set[int] = set()
        self.running_tunnel = False
        self.settings = load_settings()

        root.title("Cloudflare Tunnel Launcher")
        root.geometry("980x640")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_ui()
        self._refresh_status()
        self.root.after(POLL_MS, self._drain_queue)

    # ---------------------------- UI ----------------------------

    def _build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.X, padx=8, pady=(8, 0))

        self.tab_run = ttk.Frame(nb, padding=8)
        self.tab_setup = ttk.Frame(nb, padding=8)
        nb.add(self.tab_run, text=" 运行 ")
        nb.add(self.tab_setup, text=" 初始化向导 ")

        self._build_run_tab()
        self._build_setup_tab()

        self.log = scrolledtext.ScrolledText(
            self.root, wrap=tk.NONE, font=("Consolas", 9),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4", height=18,
        )
        self.log.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        for tag, color in (
            ("info", "#9cdcfe"), ("warn", "#dcdcaa"), ("error", "#f48771"),
            ("ok", "#b5cea8"), ("meta", "#808080"),
        ):
            self.log.tag_config(tag, foreground=color)

    def _build_run_tab(self):
        f = self.tab_run

        row1 = ttk.Frame(f)
        row1.pack(fill=tk.X)

        self.status_canvas = tk.Canvas(row1, width=18, height=18, highlightthickness=0)
        self.status_dot = self.status_canvas.create_oval(3, 3, 15, 15, fill="#888")
        self.status_canvas.pack(side=tk.LEFT, padx=(0, 6))
        self.status_label = ttk.Label(row1, text="未启动", width=22)
        self.status_label.pack(side=tk.LEFT)

        self.conn_label = ttk.Label(row1, text="连接数: 0/4", width=14)
        self.conn_label.pack(side=tk.LEFT, padx=10)

        self.start_btn = ttk.Button(row1, text="启动 Tunnel", command=self.start_tunnel)
        self.start_btn.pack(side=tk.LEFT, padx=4)
        self.stop_btn = ttk.Button(row1, text="停止", command=self.stop_tunnel, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=4)
        ttk.Button(row1, text="清空日志", command=self.clear_log).pack(side=tk.LEFT, padx=4)
        ttk.Button(row1, text="打开访问地址", command=self.open_url).pack(side=tk.LEFT, padx=4)

        row2 = ttk.LabelFrame(f, text="当前配置", padding=6)
        row2.pack(fill=tk.X, pady=(8, 0))

        self.info_tunnel = ttk.Label(row2, text="Tunnel: -")
        self.info_tunnel.pack(anchor=tk.W)
        self.info_host = ttk.Label(row2, text="Hostname: -")
        self.info_host.pack(anchor=tk.W)
        self.info_port = ttk.Label(row2, text="本地端口: -")
        self.info_port.pack(anchor=tk.W)
        self.info_cert = ttk.Label(row2, text="cert.pem: -")
        self.info_cert.pack(anchor=tk.W)

    def _build_setup_tab(self):
        f = self.tab_setup

        form = ttk.LabelFrame(f, text="参数", padding=8)
        form.pack(fill=tk.X)

        ttk.Label(form, text="Tunnel 名称:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.var_name = tk.StringVar(value=self.settings.get("tunnel_name", DEFAULT_TUNNEL_NAME))
        ttk.Entry(form, textvariable=self.var_name, width=40).grid(row=0, column=1, sticky=tk.W, padx=6)

        ttk.Label(form, text="子域名 (hostname):").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.var_host = tk.StringVar(value=self.settings.get("hostname", DEFAULT_HOSTNAME))
        ttk.Entry(form, textvariable=self.var_host, width=40).grid(row=1, column=1, sticky=tk.W, padx=6)

        ttk.Label(form, text="本地端口:").grid(row=2, column=0, sticky=tk.W, pady=2)
        self.var_port = tk.StringVar(value=str(self.settings.get("port", DEFAULT_PORT)))
        ttk.Entry(form, textvariable=self.var_port, width=10).grid(row=2, column=1, sticky=tk.W, padx=6)

        ttk.Button(form, text="保存参数", command=self.save_params).grid(row=3, column=1, sticky=tk.W, padx=6, pady=(6, 0))

        steps = ttk.LabelFrame(f, text="初始化步骤（按顺序执行，已完成会显示 ✓）", padding=8)
        steps.pack(fill=tk.X, pady=(8, 0))

        self.step1_label = ttk.Label(steps, text="① Login", width=30)
        self.step1_label.grid(row=0, column=0, sticky=tk.W, pady=3)
        self.step1_btn = ttk.Button(steps, text="执行", command=self.do_login, width=12)
        self.step1_btn.grid(row=0, column=1, padx=4)
        ttk.Label(steps, text="会弹出浏览器授权，完成后自动把 cert.pem 迁到本程序目录", foreground="#666").grid(row=0, column=2, sticky=tk.W, padx=8)

        self.step2_label = ttk.Label(steps, text="② 创建 Tunnel", width=30)
        self.step2_label.grid(row=1, column=0, sticky=tk.W, pady=3)
        self.step2_btn = ttk.Button(steps, text="执行", command=self.do_create, width=12)
        self.step2_btn.grid(row=1, column=1, padx=4)
        ttk.Label(steps, text="同名 tunnel 已存在会自动复用", foreground="#666").grid(row=1, column=2, sticky=tk.W, padx=8)

        self.step3_label = ttk.Label(steps, text="③ 绑定 DNS", width=30)
        self.step3_label.grid(row=2, column=0, sticky=tk.W, pady=3)
        self.step3_btn = ttk.Button(steps, text="执行", command=self.do_route, width=12)
        self.step3_btn.grid(row=2, column=1, padx=4)
        ttk.Label(steps, text="把子域名 CNAME 指向 tunnel", foreground="#666").grid(row=2, column=2, sticky=tk.W, padx=8)

        ttk.Button(f, text="一键执行全部（缺什么做什么）", command=self.do_auto_setup).pack(pady=10)

    # ---------------------------- Status ----------------------------

    def _refresh_status(self):
        name = self.var_name.get().strip()
        host = self.var_host.get().strip()
        port = self.var_port.get().strip()

        self.info_tunnel.config(text=f"Tunnel: {name}")
        self.info_host.config(text=f"Hostname: {host}")
        self.info_port.config(text=f"本地端口: {port}")

        cert_ok = CERT.exists()
        self.info_cert.config(text=f"cert.pem: {'✓ 已存在' if cert_ok else '✗ 缺失'}")
        self.step1_label.config(text=f"① Login  {'✓' if cert_ok else ''}")

        tid_ok = TUNNEL_ID_FILE.exists() and TUNNEL_ID_FILE.read_text(encoding="utf-8").strip() != ""
        cred_ok = False
        if tid_ok:
            uuid = TUNNEL_ID_FILE.read_text(encoding="utf-8").strip()
            cred_ok = (DATA_DIR / f"{uuid}.json").exists()
        self.step2_label.config(text=f"② 创建 Tunnel  {'✓' if cred_ok else ''}")

        routed = self.settings.get("routed_hosts", [])
        routed_ok = host in routed
        self.step3_label.config(text=f"③ 绑定 DNS  {'✓' if routed_ok else ''}")

        can_run = cert_ok and cred_ok and not self.running_tunnel
        self.start_btn.config(state=tk.NORMAL if can_run else tk.DISABLED)

    # ---------------------------- Setup actions ----------------------------

    def _guard_busy(self) -> bool:
        if self.proc and self.proc.poll() is None:
            messagebox.showwarning("忙", "有子进程正在运行，请先停止")
            return True
        return False

    def save_params(self):
        self.settings["tunnel_name"] = self.var_name.get().strip()
        self.settings["hostname"] = self.var_host.get().strip()
        try:
            self.settings["port"] = int(self.var_port.get().strip())
        except ValueError:
            messagebox.showerror("端口错误", "端口必须是数字")
            return
        save_settings(self.settings)
        self._append("[设置] 参数已保存\n", "meta")
        self._refresh_status()

    def do_login(self):
        if self._guard_busy():
            return
        if not CLOUDFLARED.exists():
            messagebox.showerror("错误", f"找不到 {CLOUDFLARED}")
            return
        DATA_DIR.mkdir(exist_ok=True)
        self._append("[Login] 启动浏览器授权，请在浏览器里选择域名授权...\n", "meta")

        def on_exit(rc):
            candidates = [
                Path(os.path.expanduser("~")) / ".cloudflared" / "cert.pem",
                BASE_DIR / "cert.pem",
            ]
            if os.name == "nt":
                userprofile = os.environ.get("USERPROFILE")
                if userprofile:
                    candidates.append(Path(userprofile) / ".cloudflared" / "cert.pem")

            moved = False
            for src in candidates:
                if src.resolve() == CERT.resolve():
                    continue
                if src.exists():
                    try:
                        shutil.move(str(src), str(CERT))
                        try:
                            src.parent.rmdir()
                        except OSError:
                            pass
                        self._append(f"[Login] 已从 {src} 迁移到 {CERT}\n", "ok")
                        moved = True
                        break
                    except Exception as e:
                        self._append(f"[Login] 迁移 {src} 失败: {e}\n", "error")

            if not moved and CERT.exists():
                self._append(f"[Login] cert.pem 已就绪: {CERT}\n", "ok")
            elif not CERT.exists():
                self._append("[Login] 未找到 cert.pem，可能授权被取消或 cloudflared 把文件写到了未知位置\n", "error")
            self._refresh_status()

        self._spawn(
            [str(CLOUDFLARED), "tunnel", "login"],
            label="login", on_exit=on_exit,
        )

    def do_create(self):
        if self._guard_busy():
            return
        if not CERT.exists():
            messagebox.showerror("顺序错误", "请先执行 ① Login")
            return
        name = self.var_name.get().strip()
        if not name:
            messagebox.showerror("参数错误", "Tunnel 名称不能为空")
            return

        def on_exit(rc):
            jsons = sorted(DATA_DIR.glob("*.json"))
            if jsons:
                uuid = jsons[-1].stem
                TUNNEL_ID_FILE.write_text(uuid, encoding="utf-8")
                self._append(f"[Create] 凭证就绪，UUID={uuid}\n", "ok")
            else:
                self._append("[Create] 未发现凭证 JSON，请检查上方报错\n", "error")
            self._refresh_status()

        self._spawn(
            [str(CLOUDFLARED), "tunnel", "--origincert", str(CERT), "create", name],
            label=f"create {name}", on_exit=on_exit,
        )

    def do_route(self):
        if self._guard_busy():
            return
        if not CERT.exists() or not TUNNEL_ID_FILE.exists():
            messagebox.showerror("顺序错误", "请先执行 ① Login 和 ② 创建 Tunnel")
            return
        name = self.var_name.get().strip()
        host = self.var_host.get().strip()
        if not host or "_" in host.split(".")[0]:
            messagebox.showerror("域名错误", "域名不能为空，且主机名段不能含下划线")
            return

        def on_exit(rc):
            if rc == 0:
                routed = self.settings.setdefault("routed_hosts", [])
                if host not in routed:
                    routed.append(host)
                save_settings(self.settings)
                self._append(f"[Route] {host} CNAME 已绑定\n", "ok")
            else:
                self._append(f"[Route] 绑定失败，返回码 {rc}（域名是否已有冲突记录？）\n", "error")
            self._refresh_status()

        self._spawn(
            [str(CLOUDFLARED), "tunnel", "--origincert", str(CERT), "route", "dns", name, host],
            label=f"route {host}", on_exit=on_exit,
        )

    def do_auto_setup(self):
        if self._guard_busy():
            return
        if not CERT.exists():
            self._append("[向导] 从 ① Login 开始\n", "meta")
            self.do_login()
            return
        tid_ok = TUNNEL_ID_FILE.exists() and TUNNEL_ID_FILE.read_text(encoding="utf-8").strip() != ""
        cred_ok = False
        if tid_ok:
            uuid = TUNNEL_ID_FILE.read_text(encoding="utf-8").strip()
            cred_ok = (DATA_DIR / f"{uuid}.json").exists()
        if not cred_ok:
            self._append("[向导] 从 ② 创建 Tunnel 开始\n", "meta")
            self.do_create()
            return
        host = self.var_host.get().strip()
        if host not in self.settings.get("routed_hosts", []):
            self._append("[向导] 执行 ③ 绑定 DNS\n", "meta")
            self.do_route()
            return
        self._append("[向导] 所有步骤已完成，可直接到“运行”页启动 Tunnel\n", "ok")

    # ---------------------------- Tunnel run ----------------------------

    def _render_config(self) -> bool:
        try:
            uuid = TUNNEL_ID_FILE.read_text(encoding="utf-8").strip()
            cred = DATA_DIR / f"{uuid}.json"
            if not cred.exists():
                self._append(f"[错误] 凭证缺失: {cred}\n", "error")
                return False
            host = self.var_host.get().strip()
            port = self.var_port.get().strip()
            config_text = (
                f"tunnel: {self.var_name.get().strip()}\n"
                f"credentials-file: {cred}\n"
                f"origincert: {CERT}\n\n"
                f"ingress:\n"
                f"  - hostname: {host}\n"
                f"    service: http://localhost:{port}\n"
                f"  - service: http_status:404\n"
            )
            CONFIG_RUNTIME.write_text(config_text, encoding="utf-8")
            self._append(f"[配置] {host} -> localhost:{port} (UUID={uuid})\n", "meta")
            return True
        except Exception as e:
            self._append(f"[错误] 生成配置失败: {e}\n", "error")
            return False

    def start_tunnel(self):
        if self.proc and self.proc.poll() is None:
            return
        if not self._render_config():
            return
        self.connections = 0
        self._connected_idx = set()
        self.conn_label.config(text="连接数: 0/4")
        self._set_status("正在连接...", "#dcdcaa")
        self.running_tunnel = True

        cmd = [
            str(CLOUDFLARED), "tunnel",
            "--origincert", str(CERT),
            "--config", str(CONFIG_RUNTIME),
            "run", self.var_name.get().strip(),
        ]
        self._spawn(cmd, label="tunnel run", on_exit=self._on_tunnel_exit, line_hook=self._tunnel_line_hook)
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

    def _tunnel_line_hook(self, line: str):
        # 精确跟踪每条连接的 connIndex=N 状态（0..3）
        m_idx = re.search(r"connIndex=(\d+)", line)
        idx = int(m_idx.group(1)) if m_idx else None

        low = line.lower()
        # 连接建立：cloudflared 多种版本措辞
        #   "Registered tunnel connection connIndex=0 ..."
        #   "Connection <uuid> registered connIndex=0 ..."
        if idx is not None and "registered" in low and "unregister" not in low:
            self._connected_idx.add(idx)
        # 连接断开：只有带 connIndex 的明确断线才算
        #   "Unregistered tunnel connection connIndex=0"
        #   "Lost connection connIndex=0"
        elif idx is not None and re.search(r"unregistered|lost connection", low):
            self._connected_idx.discard(idx)

        self.connections = len(self._connected_idx)
        self.conn_label.config(text=f"连接数: {self.connections}/4")
        if self.connections >= 1:
            self._set_status("已连接 (HTTPS 就绪)", "#4ec94e")
        elif self.running_tunnel:
            self._set_status("断开重连中", "#dcdcaa")

    def _on_tunnel_exit(self, rc):
        self._append(f"[Tunnel 退出] rc={rc}\n", "meta")
        self._set_status("已停止", "#888")
        self.stop_btn.config(state=tk.DISABLED)
        self.running_tunnel = False
        self.connections = 0
        self._connected_idx = set()
        self.conn_label.config(text="连接数: 0/4")
        self._refresh_status()

    def stop_tunnel(self):
        if not self.proc or self.proc.poll() is not None:
            return
        self._append("[操作] 停止 tunnel\n", "meta")
        try:
            if sys.platform == "win32":
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.proc.terminate()
        except Exception:
            self.proc.terminate()
        self.root.after(4000, lambda: self.proc and self.proc.poll() is None and self.proc.kill())

    # ---------------------------- Subprocess plumbing ----------------------------

    def _spawn(self, cmd, label, on_exit=None, line_hook=None):
        self._append(f"[运行] {label}: {' '.join(cmd)}\n", "meta")
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=str(BASE_DIR), bufsize=1, text=True,
                encoding="utf-8", errors="replace",
                creationflags=NO_WINDOW | NEW_GROUP,
            )
        except Exception as e:
            self._append(f"[错误] 启动失败: {e}\n", "error")
            if on_exit:
                on_exit(-1)
            return
        self._on_exit_cb = on_exit
        self._line_hook = line_hook
        self.reader_thread = threading.Thread(target=self._reader, daemon=True)
        self.reader_thread.start()

    def _reader(self):
        assert self.proc and self.proc.stdout
        try:
            for line in self.proc.stdout:
                self.log_queue.put(line)
        except Exception as e:
            self.log_queue.put(f"[读取异常] {e}\n")
        finally:
            rc = self.proc.wait() if self.proc else -1
            self.log_queue.put(f"__PROC_EXIT__{rc}")

    def _drain_queue(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                if line.startswith("__PROC_EXIT__"):
                    rc = int(line.replace("__PROC_EXIT__", "").strip() or -1)
                    cb = getattr(self, "_on_exit_cb", None)
                    self._on_exit_cb = None
                    self._line_hook = None
                    if cb:
                        cb(rc)
                    continue
                hook = getattr(self, "_line_hook", None)
                if hook:
                    try:
                        hook(line)
                    except Exception:
                        pass
                self._append(line, self._classify(line))
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._drain_queue)

    def _classify(self, line: str) -> str:
        low = line.lower()
        if "err" in low or "failed" in low or "fatal" in low:
            return "error"
        if "warn" in low or "retry" in low:
            return "warn"
        if "registered" in low or "added cname" in low or "route propagating" in low:
            return "ok"
        return "info"

    # ---------------------------- Misc ----------------------------

    def _set_status(self, text: str, color: str):
        self.status_label.config(text=text)
        self.status_canvas.itemconfig(self.status_dot, fill=color)

    def _append(self, text: str, tag: str = "info"):
        self.log.insert(tk.END, text, tag)
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES:
            self.log.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")
        self.log.see(tk.END)

    def clear_log(self):
        self.log.delete("1.0", tk.END)

    def open_url(self):
        import webbrowser
        host = self.var_host.get().strip()
        webbrowser.open(f"https://{host}")

    def on_close(self):
        if self.proc and self.proc.poll() is None:
            self.stop_tunnel()
            self.root.after(500, self.on_close)
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.2)
    except Exception:
        pass
    TunnelApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
