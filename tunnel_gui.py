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
DEFAULT_MAPPINGS = [{"hostname": DEFAULT_HOSTNAME, "port": DEFAULT_PORT}]

MAX_LOG_LINES = 2000
POLL_MS = 80
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
NEW_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0


def load_settings() -> dict:
    data: dict = {}
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    # 向后兼容：把旧的单条 hostname/port 迁移成 mappings 列表
    if "mappings" not in data:
        legacy_host = data.pop("hostname", None)
        legacy_port = data.pop("port", None)
        if legacy_host:
            try:
                p = int(legacy_port) if legacy_port is not None else DEFAULT_PORT
            except (TypeError, ValueError):
                p = DEFAULT_PORT
            data["mappings"] = [{"hostname": legacy_host, "port": p}]
        else:
            data["mappings"] = [dict(m) for m in DEFAULT_MAPPINGS]
    # 防御：清洗 mappings 字段
    cleaned = []
    for m in data.get("mappings", []):
        if not isinstance(m, dict):
            continue
        h = str(m.get("hostname", "")).strip()
        try:
            p = int(m.get("port"))
        except (TypeError, ValueError):
            continue
        if not h or not (1 <= p <= 65535):
            continue
        cleaned.append({"hostname": h, "port": p})
    if not cleaned:
        cleaned = [dict(m) for m in DEFAULT_MAPPINGS]
    data["mappings"] = cleaned
    return data


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
        self._route_queue: list[str] = []

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
        self.info_mappings = ttk.Label(row2, text="映射: -", justify=tk.LEFT)
        self.info_mappings.pack(anchor=tk.W)
        self.info_cert = ttk.Label(row2, text="cert.pem: -")
        self.info_cert.pack(anchor=tk.W)

    def _build_setup_tab(self):
        f = self.tab_setup

        form = ttk.LabelFrame(f, text="参数", padding=8)
        form.pack(fill=tk.X)

        ttk.Label(form, text="Tunnel 名称:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.var_name = tk.StringVar(value=self.settings.get("tunnel_name", DEFAULT_TUNNEL_NAME))
        ttk.Entry(form, textvariable=self.var_name, width=40).grid(row=0, column=1, sticky=tk.W, padx=6)

        ttk.Button(form, text="保存 Tunnel 名称", command=self.save_params).grid(row=0, column=2, sticky=tk.W, padx=6)

        # 映射表
        map_box = ttk.LabelFrame(f, text="端口映射（hostname -> localhost:port）", padding=8)
        map_box.pack(fill=tk.BOTH, expand=False, pady=(8, 0))

        cols = ("hostname", "port")
        self.tree = ttk.Treeview(map_box, columns=cols, show="headings", height=6, selectmode="browse")
        self.tree.heading("hostname", text="Hostname")
        self.tree.heading("port", text="本地端口")
        self.tree.column("hostname", width=420, anchor=tk.W)
        self.tree.column("port", width=100, anchor=tk.CENTER)
        self.tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.tree.bind("<Double-1>", lambda e: self.edit_mapping())

        btns = ttk.Frame(map_box)
        btns.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0))
        ttk.Button(btns, text="新增", width=10, command=self.add_mapping).pack(pady=2)
        ttk.Button(btns, text="编辑", width=10, command=self.edit_mapping).pack(pady=2)
        ttk.Button(btns, text="删除", width=10, command=self.delete_mapping).pack(pady=2)
        ttk.Button(btns, text="上移", width=10, command=lambda: self.move_mapping(-1)).pack(pady=2)
        ttk.Button(btns, text="下移", width=10, command=lambda: self.move_mapping(1)).pack(pady=2)

        self._reload_tree()

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
        mappings = self.settings.get("mappings", [])

        self.info_tunnel.config(text=f"Tunnel: {name}")
        if mappings:
            lines = ["映射:"] + [f"  {m['hostname']} -> :{m['port']}" for m in mappings]
            self.info_mappings.config(text="\n".join(lines))
        else:
            self.info_mappings.config(text="映射: -（请到「初始化向导」添加）")

        cert_ok = CERT.exists()
        self.info_cert.config(text=f"cert.pem: {'✓ 已存在' if cert_ok else '✗ 缺失'}")
        self.step1_label.config(text=f"① Login  {'✓' if cert_ok else ''}")

        tid_ok = TUNNEL_ID_FILE.exists() and TUNNEL_ID_FILE.read_text(encoding="utf-8").strip() != ""
        cred_ok = False
        if tid_ok:
            uuid = TUNNEL_ID_FILE.read_text(encoding="utf-8").strip()
            cred_ok = (DATA_DIR / f"{uuid}.json").exists()
        self.step2_label.config(text=f"② 创建 Tunnel  {'✓' if cred_ok else ''}")

        routed = set(self.settings.get("routed_hosts", []))
        hosts = [m["hostname"] for m in mappings]
        if hosts:
            done = sum(1 for h in hosts if h in routed)
            mark = "✓" if done == len(hosts) else f"({done}/{len(hosts)})"
        else:
            mark = ""
        self.step3_label.config(text=f"③ 绑定 DNS  {mark}")

        can_run = cert_ok and cred_ok and bool(mappings) and not self.running_tunnel
        self.start_btn.config(state=tk.NORMAL if can_run else tk.DISABLED)

    # ---------------------------- Setup actions ----------------------------

    def _guard_busy(self) -> bool:
        if self.proc and self.proc.poll() is None:
            messagebox.showwarning("忙", "有子进程正在运行，请先停止")
            return True
        return False

    def save_params(self):
        name = self.var_name.get().strip()
        if not name:
            messagebox.showerror("参数错误", "Tunnel 名称不能为空")
            return
        self.settings["tunnel_name"] = name
        save_settings(self.settings)
        self._append("[设置] Tunnel 名称已保存\n", "meta")
        self._refresh_status()

    # -------- mappings CRUD --------

    def _reload_tree(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for i, m in enumerate(self.settings.get("mappings", [])):
            self.tree.insert("", tk.END, iid=str(i), values=(m["hostname"], m["port"]))

    def _selected_index(self) -> int:
        sel = self.tree.selection()
        if not sel:
            return -1
        try:
            return int(sel[0])
        except ValueError:
            return -1

    def _mapping_dialog(self, title: str, init_host: str = "", init_port: str = ""):
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        result = {"ok": False, "hostname": "", "port": 0}

        ttk.Label(dlg, text="Hostname:").grid(row=0, column=0, sticky=tk.W, padx=8, pady=6)
        v_host = tk.StringVar(value=init_host)
        ttk.Entry(dlg, textvariable=v_host, width=42).grid(row=0, column=1, padx=8, pady=6)

        ttk.Label(dlg, text="本地端口:").grid(row=1, column=0, sticky=tk.W, padx=8, pady=6)
        v_port = tk.StringVar(value=init_port)
        ttk.Entry(dlg, textvariable=v_port, width=12).grid(row=1, column=1, sticky=tk.W, padx=8, pady=6)

        msg = ttk.Label(dlg, text="", foreground="#c44")
        msg.grid(row=2, column=0, columnspan=2, sticky=tk.W, padx=8)

        def on_ok():
            h = v_host.get().strip()
            p_raw = v_port.get().strip()
            if not h:
                msg.config(text="hostname 不能为空")
                return
            if "_" in h.split(".")[0]:
                msg.config(text="主机名段不能含下划线")
                return
            try:
                p = int(p_raw)
            except ValueError:
                msg.config(text="端口必须是数字")
                return
            if not (1 <= p <= 65535):
                msg.config(text="端口必须在 1-65535")
                return
            result.update({"ok": True, "hostname": h, "port": p})
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.grid(row=3, column=0, columnspan=2, pady=8)
        ttk.Button(btns, text="确定", width=10, command=on_ok).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="取消", width=10, command=dlg.destroy).pack(side=tk.LEFT, padx=4)

        dlg.update_idletasks()
        # 居中
        w, h = dlg.winfo_width(), dlg.winfo_height()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 2
        dlg.geometry(f"+{x}+{y}")

        self.root.wait_window(dlg)
        return result if result["ok"] else None

    def add_mapping(self):
        r = self._mapping_dialog("新增映射", init_port=str(DEFAULT_PORT))
        if not r:
            return
        mappings = self.settings.setdefault("mappings", [])
        if any(m["hostname"] == r["hostname"] for m in mappings):
            messagebox.showerror("重复", f"hostname 已存在: {r['hostname']}")
            return
        mappings.append({"hostname": r["hostname"], "port": r["port"]})
        save_settings(self.settings)
        self._reload_tree()
        self._refresh_status()
        self._append(f"[映射] 已新增 {r['hostname']} -> :{r['port']}\n", "meta")

    def edit_mapping(self):
        idx = self._selected_index()
        if idx < 0:
            messagebox.showinfo("提示", "请先选中一行")
            return
        cur = self.settings["mappings"][idx]
        r = self._mapping_dialog("编辑映射", init_host=cur["hostname"], init_port=str(cur["port"]))
        if not r:
            return
        # 校验 hostname 唯一性（除自己）
        for i, m in enumerate(self.settings["mappings"]):
            if i != idx and m["hostname"] == r["hostname"]:
                messagebox.showerror("重复", f"hostname 已存在: {r['hostname']}")
                return
        # 如果 hostname 变了，旧的从 routed_hosts 里去掉
        old_host = cur["hostname"]
        if old_host != r["hostname"]:
            routed = self.settings.setdefault("routed_hosts", [])
            if old_host in routed:
                routed.remove(old_host)
        self.settings["mappings"][idx] = {"hostname": r["hostname"], "port": r["port"]}
        save_settings(self.settings)
        self._reload_tree()
        self.tree.selection_set(str(idx))
        self._refresh_status()
        self._append(f"[映射] 已更新 {r['hostname']} -> :{r['port']}\n", "meta")

    def delete_mapping(self):
        idx = self._selected_index()
        if idx < 0:
            messagebox.showinfo("提示", "请先选中一行")
            return
        mappings = self.settings["mappings"]
        if len(mappings) <= 1:
            if not messagebox.askyesno("确认", "至少需要保留一条映射，确定继续删除会清空映射？"):
                return
        host = mappings[idx]["hostname"]
        if not messagebox.askyesno("确认", f"删除 {host} ?"):
            return
        mappings.pop(idx)
        routed = self.settings.setdefault("routed_hosts", [])
        if host in routed:
            routed.remove(host)
        save_settings(self.settings)
        self._reload_tree()
        self._refresh_status()
        self._append(f"[映射] 已删除 {host}\n", "meta")

    def move_mapping(self, delta: int):
        idx = self._selected_index()
        if idx < 0:
            return
        mappings = self.settings["mappings"]
        new_idx = idx + delta
        if not (0 <= new_idx < len(mappings)):
            return
        mappings[idx], mappings[new_idx] = mappings[new_idx], mappings[idx]
        save_settings(self.settings)
        self._reload_tree()
        self.tree.selection_set(str(new_idx))

    def _login_cert_candidates(self) -> list[Path]:
        candidates = [
            Path(os.path.expanduser("~")) / ".cloudflared" / "cert.pem",
            BASE_DIR / "cert.pem",
            BASE_DIR / ".cloudflared" / "cert.pem",
        ]
        if os.name == "nt":
            userprofile = os.environ.get("USERPROFILE")
            if userprofile:
                candidates.append(Path(userprofile) / ".cloudflared" / "cert.pem")
        return candidates

    def _finalize_login(self, baseline: dict[Path, float]):
        # 只把"本次 login 期间新出现 / 新更新"的 cert.pem 迁移过来，避免误用旧文件
        moved = False
        for src in self._login_cert_candidates():
            try:
                if src.resolve() == CERT.resolve():
                    continue
            except OSError:
                continue
            if not src.exists():
                continue
            prev_mtime = baseline.get(src, 0.0)
            try:
                cur_mtime = src.stat().st_mtime
            except OSError:
                continue
            if cur_mtime <= prev_mtime:
                continue
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

    def do_login(self):
        if self._guard_busy():
            return
        if not CLOUDFLARED.exists():
            messagebox.showerror("错误", f"找不到 {CLOUDFLARED}")
            return
        DATA_DIR.mkdir(exist_ok=True)
        self._append("[Login] 启动浏览器授权，请在浏览器里选择域名授权...\n", "meta")

        # 记录每个候选路径"登录前"的 mtime，授权完成后只认新写入的 cert
        baseline: dict[Path, float] = {}
        for p in self._login_cert_candidates():
            try:
                baseline[p] = p.stat().st_mtime if p.exists() else 0.0
            except OSError:
                baseline[p] = 0.0

        self._login_done = False  # 防止轮询与 on_exit 双重触发

        def on_exit(rc):
            # cloudflared 自己退出了——兜底路径
            if self._login_done:
                return
            self._login_done = True
            self._finalize_login(baseline)

        def poll_cert():
            # 每 1s 检查一次候选路径，发现新 cert 即认为授权完成
            if self._login_done:
                return
            if self.proc is None or self.proc.poll() is not None:
                # 子进程已退出，交给 on_exit 处理
                return
            for p in self._login_cert_candidates():
                try:
                    if not p.exists():
                        continue
                    cur = p.stat().st_mtime
                except OSError:
                    continue
                if cur > baseline.get(p, 0.0):
                    self._login_done = True
                    self._append("[Login] 检测到 cert.pem 已写入，结束等待\n", "ok")
                    # 主动终止 cloudflared login（它在拿到 cert 后有时不会自己退出）
                    try:
                        if sys.platform == "win32":
                            self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                        else:
                            self.proc.terminate()
                    except Exception:
                        try:
                            self.proc.terminate()
                        except Exception:
                            pass
                    self.root.after(2000, lambda: self.proc and self.proc.poll() is None and self.proc.kill())
                    self._finalize_login(baseline)
                    return
            self.root.after(1000, poll_cert)

        self._spawn(
            [str(CLOUDFLARED), "tunnel", "login"],
            label="login", on_exit=on_exit,
        )
        self.root.after(1000, poll_cert)

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
        mappings = self.settings.get("mappings", [])
        if not mappings:
            messagebox.showerror("没有映射", "请先到「端口映射」添加至少一条")
            return
        for m in mappings:
            h = m["hostname"]
            if not h or "_" in h.split(".")[0]:
                messagebox.showerror("域名错误", f"非法 hostname: {h}（主机名段不能含下划线）")
                return
        routed = set(self.settings.get("routed_hosts", []))
        pending = [m["hostname"] for m in mappings if m["hostname"] not in routed]
        if not pending:
            self._append("[Route] 所有 hostname 已绑定 DNS\n", "ok")
            return
        self._route_queue = pending
        self._append(f"[Route] 开始批量绑定 {len(pending)} 个 hostname\n", "meta")
        self._route_next()

    def _route_next(self):
        if not self._route_queue:
            self._append("[Route] 批量绑定完成\n", "ok")
            self._refresh_status()
            return
        host = self._route_queue.pop(0)
        name = self.var_name.get().strip()

        def on_exit(rc):
            if rc == 0:
                routed = self.settings.setdefault("routed_hosts", [])
                if host not in routed:
                    routed.append(host)
                save_settings(self.settings)
                self._append(f"[Route] {host} CNAME 已绑定\n", "ok")
            else:
                self._append(f"[Route] {host} 绑定失败，返回码 {rc}（域名是否已有冲突记录？）\n", "error")
            self._refresh_status()
            # 串行下一个
            self.root.after(200, self._route_next)

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
        routed = set(self.settings.get("routed_hosts", []))
        hosts = [m["hostname"] for m in self.settings.get("mappings", [])]
        if not hosts:
            messagebox.showerror("没有映射", "请先到「端口映射」添加至少一条")
            return
        if any(h not in routed for h in hosts):
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
            mappings = self.settings.get("mappings", [])
            if not mappings:
                self._append("[错误] 没有任何端口映射，请先到「初始化向导」添加\n", "error")
                return False
            ingress_lines = ["ingress:"]
            for m in mappings:
                ingress_lines.append(f"  - hostname: {m['hostname']}")
                ingress_lines.append(f"    service: http://localhost:{m['port']}")
            ingress_lines.append("  - service: http_status:404")
            config_text = (
                f"tunnel: {self.var_name.get().strip()}\n"
                f"credentials-file: {cred}\n"
                f"origincert: {CERT}\n\n"
                + "\n".join(ingress_lines) + "\n"
            )
            CONFIG_RUNTIME.write_text(config_text, encoding="utf-8")
            for m in mappings:
                self._append(f"[配置] {m['hostname']} -> localhost:{m['port']}\n", "meta")
            self._append(f"[配置] UUID={uuid}\n", "meta")
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
        hosts = [m["hostname"] for m in self.settings.get("mappings", [])]
        if not hosts:
            messagebox.showinfo("提示", "尚无映射可访问")
            return
        if len(hosts) == 1:
            webbrowser.open(f"https://{hosts[0]}")
            return
        menu = tk.Menu(self.root, tearoff=0)
        for h in hosts:
            menu.add_command(label=h, command=lambda u=h: webbrowser.open(f"https://{u}"))
        try:
            x = self.root.winfo_pointerx()
            y = self.root.winfo_pointery()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

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
