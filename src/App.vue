<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, nextTick } from "vue";
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { openUrl } from "@tauri-apps/plugin-opener";
import type { AppStatus, LogMessage, Settings } from "./types";

const activeTab = ref<"run" | "setup">("run");

// Settings state
const settings = reactive<Settings>({
  tunnel_name: "cf-tunnel-senx",
  mappings: [{ hostname: "cf-tunnel.senx.de5.net", port: 8080 }],
  routed_hosts: [],
});

// App status state
const status = reactive<AppStatus>({
  cert_exists: false,
  tunnel_id: null,
  cred_exists: false,
  routed_count: 0,
  total_hosts: 0,
  is_running: false,
  is_busy: false,
  connections: 0,
  status_text: "正在读取状态...",
  status_color: "#888888",
});

// Logs state
const logs = ref<LogMessage[]>([]);
const logContainerRef = ref<HTMLDivElement | null>(null);
const autoScroll = ref(true);
const MAX_LOGS = 2000;

// Mapping Modal
const showModal = ref(false);
const modalMode = ref<"add" | "edit">("add");
const editIndex = ref(-1);
const formHostname = ref("");
const formPort = ref(8080);
const formError = ref("");

// Host selection dropdown
const showHostDropdown = ref(false);

// Toast notification
const toastMsg = ref("");
const toastType = ref<"info" | "success" | "error">("info");
let toastTimer: number | null = null;

function showToast(msg: string, type: "info" | "success" | "error" = "info") {
  toastMsg.value = msg;
  toastType.value = type;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    toastMsg.value = "";
  }, 3500);
}

// Check if ready to run
const canStartTunnel = computed(() => {
  return (
    status.cert_exists &&
    status.cred_exists &&
    settings.mappings.length > 0 &&
    !status.is_running &&
    !status.is_busy
  );
});

// Load settings from backend
async function fetchSettings() {
  try {
    const res = await invoke<Settings>("load_settings");
    settings.tunnel_name = res.tunnel_name;
    settings.mappings = res.mappings || [];
    settings.routed_hosts = res.routed_hosts || [];
  } catch (e: any) {
    showToast(`读取设置失败: ${e}`, "error");
  }
}

// Refresh status from backend
async function refreshStatus() {
  try {
    const res = await invoke<AppStatus>("get_app_status");
    Object.assign(status, res);
  } catch (e: any) {
    console.error("刷新状态失败:", e);
  }
}

// Save settings to backend
async function saveAllSettings(notify = true) {
  try {
    await invoke("save_settings", { settings: { ...settings } });
    if (notify) {
      showToast("设置已保存", "success");
    }
    await refreshStatus();
  } catch (e: any) {
    showToast(`保存失败: ${e}`, "error");
  }
}

// Start Tunnel
async function handleStartTunnel() {
  if (!canStartTunnel.value) return;
  try {
    await invoke("start_tunnel");
    showToast("Tunnel 启动中...", "info");
    await refreshStatus();
  } catch (e: any) {
    showToast(`启动失败: ${e}`, "error");
  }
}

// Stop Tunnel
async function handleStopTunnel() {
  try {
    await invoke("stop_tunnel");
    showToast("Tunnel 已停止", "info");
    await refreshStatus();
  } catch (e: any) {
    showToast(`停止失败: ${e}`, "error");
  }
}

// Log management
function appendLog(log: LogMessage) {
  logs.value.push(log);
  if (logs.value.length > MAX_LOGS) {
    logs.value.shift();
  }
  if (autoScroll.value) {
    nextTick(() => {
      if (logContainerRef.value) {
        logContainerRef.value.scrollTop = logContainerRef.value.scrollHeight;
      }
    });
  }
}

function clearLogs() {
  logs.value = [];
  showToast("日志已清空", "info");
}

async function copyLogs() {
  const fullText = logs.value.map((l) => l.text).join("");
  try {
    await navigator.clipboard.writeText(fullText);
    showToast("日志已复制到剪贴板", "success");
  } catch {
    showToast("复制失败", "error");
  }
}

// Browser visit
function openSingleUrl(host: string) {
  showHostDropdown.value = false;
  const url = `https://${host}`;
  openUrl(url).catch((err) => {
    showToast(`无法打开浏览器: ${err}`, "error");
  });
}

function handleOpenUrlClick() {
  if (settings.mappings.length === 0) {
    showToast("暂无可访问的域名映射", "info");
    return;
  }
  if (settings.mappings.length === 1) {
    openSingleUrl(settings.mappings[0].hostname);
  } else {
    showHostDropdown.value = !showHostDropdown.value;
  }
}

// Setup Step 1: Login
async function doLogin() {
  if (status.is_busy || status.is_running) {
    showToast("有任务正在运行中", "error");
    return;
  }
  try {
    await invoke("start_login");
    showToast("已拉起浏览器登录，请在网页中完成授权", "info");
    await refreshStatus();
  } catch (e: any) {
    showToast(`Login 失败: ${e}`, "error");
  }
}

// Setup Step 2: Create Tunnel
async function doCreate() {
  if (!status.cert_exists) {
    showToast("请先执行 ① Login 登录", "error");
    return;
  }
  if (!settings.tunnel_name.trim()) {
    showToast("Tunnel 名称不能为空", "error");
    return;
  }
  try {
    const uuid = await invoke<string>("start_create_tunnel", {
      name: settings.tunnel_name.trim(),
    });
    showToast(`Tunnel 创建成功! UUID=${uuid}`, "success");
    await refreshStatus();
  } catch (e: any) {
    showToast(`创建 Tunnel 失败: ${e}`, "error");
  }
}

// Setup Step 3: Route DNS
async function doRoute() {
  if (!status.cert_exists || !status.cred_exists) {
    showToast("请先执行 ① Login 和 ② 创建 Tunnel", "error");
    return;
  }
  if (settings.mappings.length === 0) {
    showToast("请先添加至少一条端口映射", "error");
    return;
  }

  const unrouted = settings.mappings.filter(
    (m) => !settings.routed_hosts.includes(m.hostname)
  );

  if (unrouted.length === 0) {
    showToast("所有域名均已完成 DNS 绑定", "success");
    return;
  }

  showToast(`开始批量绑定 ${unrouted.length} 个域名...`, "info");
  for (const m of unrouted) {
    try {
      await invoke("start_route_dns", {
        name: settings.tunnel_name.trim(),
        host: m.hostname,
      });
      await fetchSettings();
      await refreshStatus();
    } catch (e: any) {
      showToast(`${m.hostname} 绑定失败: ${e}`, "error");
    }
  }
  showToast("DNS 绑定操作已完成", "success");
}

// Auto Setup Wizard
async function doAutoSetup() {
  if (status.is_busy || status.is_running) {
    showToast("有任务正在运行中", "error");
    return;
  }

  if (!status.cert_exists) {
    showToast("【一键向导】正在执行 ① Login 登录...", "info");
    await doLogin();
    return;
  }

  if (!status.cred_exists) {
    showToast("【一键向导】正在执行 ② 创建 Tunnel...", "info");
    await doCreate();
    return;
  }

  const unrouted = settings.mappings.filter(
    (m) => !settings.routed_hosts.includes(m.hostname)
  );
  if (unrouted.length > 0) {
    showToast("【一键向导】正在执行 ③ 绑定 DNS...", "info");
    await doRoute();
    return;
  }

  showToast("【一键向导】所有步骤已完成，可直接启动 Tunnel！", "success");
}

// Mapping CRUD
function openAddMapping() {
  modalMode.value = "add";
  formHostname.value = "";
  formPort.value = 8080;
  formError.value = "";
  showModal.value = true;
}

function openEditMapping(idx: number) {
  modalMode.value = "edit";
  editIndex.value = idx;
  formHostname.value = settings.mappings[idx].hostname;
  formPort.value = settings.mappings[idx].port;
  formError.value = "";
  showModal.value = true;
}

function saveMappingModal() {
  const host = formHostname.value.trim();
  const port = Number(formPort.value);

  if (!host) {
    formError.value = "Hostname 不能为空";
    return;
  }
  if (host.split(".")[0].includes("_")) {
    formError.value = "主机名段不能包含下划线";
    return;
  }
  if (!port || port < 1 || port > 65535) {
    formError.value = "端口必须在 1 ~ 65535 之间";
    return;
  }

  const duplicate = settings.mappings.some(
    (m, idx) => m.hostname === host && (modalMode.value === "add" || idx !== editIndex.value)
  );
  if (duplicate) {
    formError.value = `Hostname "${host}" 已存在`;
    return;
  }

  if (modalMode.value === "add") {
    settings.mappings.push({ hostname: host, port });
    showToast(`已新增映射 ${host} -> :${port}`, "success");
  } else {
    const oldHost = settings.mappings[editIndex.value].hostname;
    if (oldHost !== host) {
      settings.routed_hosts = settings.routed_hosts.filter((h) => h !== oldHost);
    }
    settings.mappings[editIndex.value] = { hostname: host, port };
    showToast(`已更新映射 ${host} -> :${port}`, "success");
  }

  showModal.value = false;
  saveAllSettings(false);
}

function deleteMapping(idx: number) {
  const host = settings.mappings[idx].hostname;
  if (!confirm(`确定删除映射 ${host} 吗？`)) return;
  settings.mappings.splice(idx, 1);
  settings.routed_hosts = settings.routed_hosts.filter((h) => h !== host);
  saveAllSettings(false);
  showToast(`已删除 ${host}`, "info");
}

function moveMapping(idx: number, delta: number) {
  const target = idx + delta;
  if (target < 0 || target >= settings.mappings.length) return;
  const temp = settings.mappings[idx];
  settings.mappings[idx] = settings.mappings[target];
  settings.mappings[target] = temp;
  saveAllSettings(false);
}

// Lifecycle & Listeners
let unlistenLog: UnlistenFn | null = null;
let unlistenStatus: UnlistenFn | null = null;
let statusInterval: number | null = null;

onMounted(async () => {
  await fetchSettings();
  await refreshStatus();

  unlistenLog = await listen<LogMessage>("tunnel-log", (event) => {
    appendLog(event.payload);
  });

  unlistenStatus = await listen("status-refresh", () => {
    refreshStatus();
  });

  // Regular status ping
  statusInterval = window.setInterval(() => {
    refreshStatus();
  }, 3000);
});

onUnmounted(() => {
  if (unlistenLog) unlistenLog();
  if (unlistenStatus) unlistenStatus();
  if (statusInterval) clearInterval(statusInterval);
});
</script>

<template>
  <div class="app-layout">
    <!-- Header -->
    <header class="app-header">
      <div class="brand">
        <div class="brand-logo">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z" stroke="#f48120" fill="#f4812022" />
          </svg>
        </div>
        <div class="brand-text">
          <h1 class="brand-title">Cloudflare Tunnel</h1>
          <span class="brand-sub">Native Desktop Launcher</span>
        </div>
      </div>

      <!-- Live Status Pill -->
      <div class="status-pill">
        <span class="status-dot" :style="{ backgroundColor: status.status_color }"></span>
        <span class="status-name">{{ status.status_text }}</span>
        <div class="conn-badge" :class="{ active: status.connections > 0 }">
          连接数: {{ status.connections }}/4
        </div>
      </div>

      <!-- Navigation Tabs -->
      <nav class="nav-tabs">
        <button
          class="tab-btn"
          :class="{ active: activeTab === 'run' }"
          @click="activeTab = 'run'"
        >
          运行与监控
        </button>
        <button
          class="tab-btn"
          :class="{ active: activeTab === 'setup' }"
          @click="activeTab = 'setup'"
        >
          初始化向导
        </button>
      </nav>
    </header>

    <!-- Main Content Area -->
    <main class="app-main">
      <!-- TAB 1: RUN & MONITOR -->
      <section v-if="activeTab === 'run'" class="tab-pane">
        <!-- Control Actions Bar -->
        <div class="control-panel">
          <div class="btn-group">
            <button
              v-if="!status.is_running"
              class="action-btn btn-primary"
              :disabled="!canStartTunnel"
              @click="handleStartTunnel"
            >
              <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
                <polygon points="5 3 19 12 5 21 5 3" />
              </svg>
              启动 Tunnel
            </button>
            <button
              v-else
              class="action-btn btn-danger"
              @click="handleStopTunnel"
            >
              <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
                <rect x="6" y="6" width="12" height="12" rx="2" />
              </svg>
              停止 Tunnel
            </button>

            <!-- Visit URL Dropdown -->
            <div class="dropdown-wrapper">
              <button class="action-btn btn-secondary" @click="handleOpenUrlClick">
                <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
                  <polyline points="15 3 21 3 21 9" />
                  <line x1="10" y1="14" x2="21" y2="3" />
                </svg>
                打开访问地址
                <span v-if="settings.mappings.length > 1" class="chevron">▾</span>
              </button>

              <div v-if="showHostDropdown && settings.mappings.length > 1" class="dropdown-menu">
                <div
                  v-for="m in settings.mappings"
                  :key="m.hostname"
                  class="dropdown-item"
                  @click="openSingleUrl(m.hostname)"
                >
                  <span class="item-host">https://{{ m.hostname }}</span>
                  <span class="item-port">:{{ m.port }}</span>
                </div>
              </div>
            </div>

            <button class="action-btn btn-secondary" @click="clearLogs">
              清空日志
            </button>
          </div>

          <div class="btn-group-right">
            <label class="toggle-scroll">
              <input type="checkbox" v-model="autoScroll" />
              <span>自动滚动</span>
            </label>
            <button class="action-btn btn-text" @click="copyLogs" title="复制所有日志">
              复制日志
            </button>
          </div>
        </div>

        <!-- Runtime Info Overview Card -->
        <div class="info-card">
          <div class="info-item">
            <span class="info-label">Tunnel 名称</span>
            <span class="info-value font-mono">{{ settings.tunnel_name || '-' }}</span>
          </div>
          <div class="info-item">
            <span class="info-label">凭据 UUID</span>
            <span class="info-value font-mono" :title="status.tunnel_id || ''">
              {{ status.tunnel_id ? status.tunnel_id.substring(0, 16) + '...' : '未就绪' }}
            </span>
          </div>
          <div class="info-item">
            <span class="info-label">证书 (cert.pem)</span>
            <span class="info-tag" :class="{ ok: status.cert_exists }">
              {{ status.cert_exists ? '已就绪' : '缺失' }}
            </span>
          </div>
          <div class="info-item info-mappings-col">
            <span class="info-label">映射列表 ({{ settings.mappings.length }})</span>
            <div class="mapping-badges">
              <span
                v-for="m in settings.mappings"
                :key="m.hostname"
                class="mapping-badge"
                :title="`本地 localhost:${m.port}`"
              >
                {{ m.hostname }} ➔ :{{ m.port }}
              </span>
            </div>
          </div>
        </div>

        <!-- Terminal Log Box -->
        <div class="terminal-wrapper">
          <div class="terminal-header">
            <div class="term-dots">
              <span class="term-dot dot-red"></span>
              <span class="term-dot dot-yellow"></span>
              <span class="term-dot dot-green"></span>
            </div>
            <span class="term-title">实时终端输出 ({{ logs.length }} 行)</span>
            <span class="term-hint">实时监控 stdout / stderr</span>
          </div>
          <div ref="logContainerRef" class="terminal-body font-mono">
            <div
              v-for="(log, idx) in logs"
              :key="idx"
              class="log-line"
              :class="`level-${log.level}`"
            >{{ log.text }}</div>
            <div v-if="logs.length === 0" class="log-empty">
              [等待命令执行或 Tunnel 启动输出...]
            </div>
          </div>
        </div>
      </section>

      <!-- TAB 2: SETUP WIZARD -->
      <section v-else-if="activeTab === 'setup'" class="tab-pane setup-pane">
        <!-- Form: Tunnel Name -->
        <div class="card">
          <div class="card-header">
            <h3>基础参数</h3>
          </div>
          <div class="card-body form-row">
            <div class="field-group">
              <label>Tunnel 名称</label>
              <input
                type="text"
                v-model="settings.tunnel_name"
                placeholder="例如: cf-tunnel-senx"
                class="input-control"
              />
            </div>
            <button class="action-btn btn-primary" @click="saveAllSettings(true)">
              保存 Tunnel 名称
            </button>
          </div>
        </div>

        <!-- Mappings Table -->
        <div class="card">
          <div class="card-header space-between">
            <div>
              <h3>端口映射配置</h3>
              <p class="card-desc">将外网公网 Hostname 映射到本机服务端口 (localhost:port)</p>
            </div>
            <button class="action-btn btn-primary btn-sm" @click="openAddMapping">
              + 新增映射
            </button>
          </div>
          <div class="card-body no-padding">
            <table class="table">
              <thead>
                <tr>
                  <th style="width: 60px">#</th>
                  <th>Hostname (外网域名)</th>
                  <th style="width: 140px">本地端口</th>
                  <th style="width: 110px">DNS 状态</th>
                  <th style="width: 220px; text-align: right">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(m, idx) in settings.mappings" :key="m.hostname">
                  <td>{{ idx + 1 }}</td>
                  <td class="font-mono text-bold">{{ m.hostname }}</td>
                  <td class="font-mono">
                    <span class="port-tag">:{{ m.port }}</span>
                  </td>
                  <td>
                    <span
                      class="dns-tag"
                      :class="{ routed: settings.routed_hosts.includes(m.hostname) }"
                    >
                      {{ settings.routed_hosts.includes(m.hostname) ? '已绑定' : '未绑定' }}
                    </span>
                  </td>
                  <td style="text-align: right">
                    <div class="row-actions">
                      <button class="btn-icon" @click="moveMapping(idx, -1)" title="上移" :disabled="idx === 0">
                        ↑
                      </button>
                      <button class="btn-icon" @click="moveMapping(idx, 1)" title="下移" :disabled="idx === settings.mappings.length - 1">
                        ↓
                      </button>
                      <button class="btn-link" @click="openEditMapping(idx)">编辑</button>
                      <button class="btn-link text-danger" @click="deleteMapping(idx)">删除</button>
                    </div>
                  </td>
                </tr>
                <tr v-if="settings.mappings.length === 0">
                  <td colspan="5" class="table-empty">暂无端口映射，请点击右上角新增</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- Setup Steps -->
        <div class="card">
          <div class="card-header space-between">
            <div>
              <h3>初始化步骤</h3>
              <p class="card-desc">首次使用请依次执行以下 3 步（已完成步骤会显示绿色对勾）</p>
            </div>
            <button class="action-btn btn-auto" :disabled="status.is_busy" @click="doAutoSetup">
              ⚡ 一键执行全部（缺什么做什么）
            </button>
          </div>
          <div class="card-body steps-container">
            <!-- Step 1 -->
            <div class="step-card" :class="{ done: status.cert_exists }">
              <div class="step-badge">
                <span v-if="status.cert_exists" class="badge-icon">✓</span>
                <span v-else>1</span>
              </div>
              <div class="step-content">
                <div class="step-title">
                  <span>① Login 账号授权</span>
                  <span class="step-status" :class="{ ok: status.cert_exists }">
                    {{ status.cert_exists ? '已授权 (cert.pem)' : '待执行' }}
                  </span>
                </div>
                <div class="step-desc">
                  会自动拉起默认浏览器进行 Cloudflare 授权，完成后自动将凭证归档到本程序 data 目录。
                </div>
              </div>
              <div class="step-action">
                <button
                  class="action-btn btn-secondary btn-sm"
                  :disabled="status.is_busy"
                  @click="doLogin"
                >
                  {{ status.cert_exists ? '重新登录' : '执行 Login' }}
                </button>
              </div>
            </div>

            <!-- Step 2 -->
            <div class="step-card" :class="{ done: status.cred_exists }">
              <div class="step-badge">
                <span v-if="status.cred_exists" class="badge-icon">✓</span>
                <span v-else>2</span>
              </div>
              <div class="step-content">
                <div class="step-title">
                  <span>② 创建 Tunnel</span>
                  <span class="step-status" :class="{ ok: status.cred_exists }">
                    {{ status.cred_exists ? '已创建' : '待执行' }}
                  </span>
                </div>
                <div class="step-desc">
                  使用设定的 Tunnel 名称在 Cloudflare 创建隧道并拉取凭据 JSON；同名隧道已存在会自动识别复用。
                </div>
              </div>
              <div class="step-action">
                <button
                  class="action-btn btn-secondary btn-sm"
                  :disabled="!status.cert_exists || status.is_busy"
                  @click="doCreate"
                >
                  执行创建
                </button>
              </div>
            </div>

            <!-- Step 3 -->
            <div class="step-card" :class="{ done: status.routed_count === status.total_hosts && status.total_hosts > 0 }">
              <div class="step-badge">
                <span v-if="status.routed_count === status.total_hosts && status.total_hosts > 0" class="badge-icon">✓</span>
                <span v-else>3</span>
              </div>
              <div class="step-content">
                <div class="step-title">
                  <span>③ 绑定 DNS 路由</span>
                  <span
                    class="step-status"
                    :class="{ ok: status.routed_count === status.total_hosts && status.total_hosts > 0 }"
                  >
                    已绑定 ({{ status.routed_count }}/{{ status.total_hosts }})
                  </span>
                </div>
                <div class="step-desc">
                  把配置的全部子域名以 CNAME 方式指向该 Tunnel，使外网流量能够路由到本机。
                </div>
              </div>
              <div class="step-action">
                <button
                  class="action-btn btn-secondary btn-sm"
                  :disabled="!status.cred_exists || status.is_busy"
                  @click="doRoute"
                >
                  执行绑定
                </button>
              </div>
            </div>
          </div>
        </div>
      </section>
    </main>

    <!-- Mapping Add/Edit Modal -->
    <div v-if="showModal" class="modal-overlay" @click.self="showModal = false">
      <div class="modal-box">
        <div class="modal-header">
          <h3>{{ modalMode === 'add' ? '新增端口映射' : '编辑端口映射' }}</h3>
          <button class="modal-close" @click="showModal = false">✕</button>
        </div>
        <div class="modal-body">
          <div class="form-group">
            <label>Hostname (子域名)</label>
            <input
              type="text"
              v-model="formHostname"
              placeholder="例如: cf-tunnel.yourdomain.com"
              class="input-control"
            />
            <span class="form-hint">DNS 规范限制：主机名段不能包含下划线 (_)</span>
          </div>
          <div class="form-group">
            <label>本地端口 (Localhost Port)</label>
            <input
              type="number"
              v-model="formPort"
              placeholder="8080"
              class="input-control"
            />
            <span class="form-hint">本地提供 HTTP 服务的端口号 (1 ~ 65535)</span>
          </div>
          <div v-if="formError" class="form-error">
            {{ formError }}
          </div>
        </div>
        <div class="modal-footer">
          <button class="action-btn btn-secondary" @click="showModal = false">取消</button>
          <button class="action-btn btn-primary" @click="saveMappingModal">确定</button>
        </div>
      </div>
    </div>

    <!-- Toast Notification -->
    <transition name="fade">
      <div v-if="toastMsg" class="toast" :class="`toast-${toastType}`">
        {{ toastMsg }}
      </div>
    </transition>
  </div>
</template>

<style scoped>
.app-layout {
  display: flex;
  flex-direction: column;
  height: 100vh;
  width: 100vw;
  background-color: #12151a;
  color: #e2e8f0;
  user-select: none;
  overflow: hidden;
}

/* Header */
.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 20px;
  background: #181d24;
  border-bottom: 1px solid #232b36;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
}

.brand-logo {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: 8px;
  background: #232a35;
}

.brand-title {
  font-size: 15px;
  font-weight: 600;
  margin: 0;
  color: #f8fafc;
}

.brand-sub {
  font-size: 11px;
  color: #94a3b8;
}

/* Status Pill */
.status-pill {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 14px;
  background: #0f1217;
  border-radius: 20px;
  border: 1px solid #273140;
}

.status-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  box-shadow: 0 0 6px currentColor;
  transition: background-color 0.3s;
}

.status-name {
  font-size: 13px;
  font-weight: 500;
}

.conn-badge {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 12px;
  background: #1e2633;
  color: #94a3b8;
}

.conn-badge.active {
  background: #064e3b;
  color: #34d399;
}

/* Nav Tabs */
.nav-tabs {
  display: flex;
  background: #0f1217;
  padding: 3px;
  border-radius: 8px;
  border: 1px solid #273140;
}

.tab-btn {
  background: transparent;
  border: none;
  color: #94a3b8;
  font-size: 13px;
  font-weight: 500;
  padding: 6px 16px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s;
}

.tab-btn.active {
  background: #232b36;
  color: #f8fafc;
}

/* Main */
.app-main {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
}

.tab-pane {
  display: flex;
  flex-direction: column;
  height: 100%;
  gap: 12px;
}

.setup-pane {
  overflow-y: auto;
  padding-bottom: 20px;
}

/* Control Panel */
.control-panel {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 14px;
  background: #181d24;
  border-radius: 8px;
  border: 1px solid #232b36;
}

.btn-group,
.btn-group-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.toggle-scroll {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #94a3b8;
  cursor: pointer;
}

/* Action Buttons */
.action-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 7px 15px;
  font-size: 13px;
  font-weight: 500;
  border-radius: 6px;
  border: 1px solid transparent;
  cursor: pointer;
  transition: all 0.15s ease-in-out;
}

.action-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.btn-primary {
  background: #2563eb;
  color: white;
}
.btn-primary:hover:not(:disabled) {
  background: #1d4ed8;
}

.btn-danger {
  background: #dc2626;
  color: white;
}
.btn-danger:hover:not(:disabled) {
  background: #b91c1c;
}

.btn-secondary {
  background: #232b36;
  color: #cbd5e1;
  border-color: #333f50;
}
.btn-secondary:hover:not(:disabled) {
  background: #2d3748;
  color: white;
}

.btn-auto {
  background: linear-gradient(135deg, #f59e0b, #d97706);
  color: #ffffff;
  font-weight: 600;
}
.btn-auto:hover:not(:disabled) {
  opacity: 0.92;
}

.btn-text {
  background: transparent;
  color: #94a3b8;
  padding: 6px 10px;
}
.btn-text:hover {
  color: #f1f5f9;
}

.btn-sm {
  padding: 4px 10px;
  font-size: 12px;
}

/* Dropdown */
.dropdown-wrapper {
  position: relative;
}

.chevron {
  font-size: 10px;
}

.dropdown-menu {
  position: absolute;
  top: 100%;
  left: 0;
  margin-top: 6px;
  background: #181d24;
  border: 1px solid #333f50;
  border-radius: 6px;
  box-shadow: 0 10px 25px rgba(0, 0, 0, 0.5);
  z-index: 50;
  min-width: 240px;
  overflow: hidden;
}

.dropdown-item {
  display: flex;
  justify-content: space-between;
  padding: 8px 12px;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.15s;
}

.dropdown-item:hover {
  background: #2563eb;
  color: white;
}

.item-port {
  color: #94a3b8;
}

/* Info Card */
.info-card {
  display: grid;
  grid-template-columns: 180px 180px 130px 1fr;
  gap: 12px;
  padding: 12px 16px;
  background: #181d24;
  border-radius: 8px;
  border: 1px solid #232b36;
}

.info-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.info-label {
  font-size: 11px;
  color: #64748b;
  text-transform: uppercase;
}

.info-value {
  font-size: 13px;
  color: #f8fafc;
}

.info-tag {
  display: inline-block;
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
  background: #451a1a;
  color: #f87171;
  width: fit-content;
}
.info-tag.ok {
  background: #064e3b;
  color: #34d399;
}

.mapping-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.mapping-badge {
  font-family: monospace;
  font-size: 11px;
  background: #0f1217;
  padding: 2px 8px;
  border-radius: 4px;
  border: 1px solid #273140;
  color: #93c5fd;
}

/* Terminal Log View */
.terminal-wrapper {
  flex: 1;
  min-height: 250px;
  display: flex;
  flex-direction: column;
  background: #0b0d11;
  border-radius: 8px;
  border: 1px solid #232b36;
  overflow: hidden;
}

.terminal-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  background: #14171d;
  border-bottom: 1px solid #1f2530;
}

.term-dots {
  display: flex;
  gap: 6px;
}

.term-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
}
.dot-red { background: #ef4444; }
.dot-yellow { background: #f59e0b; }
.dot-green { background: #10b981; }

.term-title {
  font-size: 12px;
  color: #94a3b8;
  font-weight: 500;
}

.term-hint {
  margin-left: auto;
  font-size: 11px;
  color: #475569;
}

.terminal-body {
  flex: 1;
  padding: 10px 14px;
  overflow-y: auto;
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-all;
}

.log-line {
  margin-bottom: 1px;
}

.level-info { color: #cbd5e1; }
.level-ok { color: #4ade80; }
.level-warn { color: #facc15; }
.level-error { color: #f87171; }
.level-meta { color: #64748b; }

.log-empty {
  color: #475569;
  font-style: italic;
}

/* Card in Setup */
.card {
  background: #181d24;
  border: 1px solid #232b36;
  border-radius: 8px;
  overflow: hidden;
}

.card-header {
  padding: 12px 16px;
  background: #1c222b;
  border-bottom: 1px solid #232b36;
}

.card-header h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: #f1f5f9;
}

.card-desc {
  margin: 4px 0 0 0;
  font-size: 12px;
  color: #94a3b8;
}

.space-between {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.card-body {
  padding: 16px;
}

.no-padding {
  padding: 0;
}

.form-row {
  display: flex;
  align-items: flex-end;
  gap: 16px;
}

.field-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.field-group label {
  font-size: 12px;
  color: #94a3b8;
}

.input-control {
  background: #0f1217;
  border: 1px solid #2e3846;
  border-radius: 6px;
  padding: 7px 12px;
  font-size: 13px;
  color: #f8fafc;
  outline: none;
  min-width: 280px;
}
.input-control:focus {
  border-color: #2563eb;
}

/* Table */
.table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.table th {
  text-align: left;
  padding: 9px 16px;
  background: #14171d;
  color: #64748b;
  font-weight: 500;
  border-bottom: 1px solid #232b36;
}

.table td {
  padding: 10px 16px;
  border-bottom: 1px solid #1c222b;
  color: #cbd5e1;
}

.table tr:hover td {
  background: #1a2029;
}

.port-tag {
  background: #0f1217;
  padding: 2px 6px;
  border-radius: 4px;
  color: #38bdf8;
}

.dns-tag {
  display: inline-block;
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
  background: #2a2215;
  color: #fbbf24;
}
.dns-tag.routed {
  background: #064e3b;
  color: #34d399;
}

.row-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.btn-icon {
  background: #232b36;
  border: none;
  color: #94a3b8;
  width: 24px;
  height: 24px;
  border-radius: 4px;
  cursor: pointer;
}
.btn-icon:disabled {
  opacity: 0.3;
  cursor: not-allowed;
}

.btn-link {
  background: none;
  border: none;
  color: #38bdf8;
  cursor: pointer;
  font-size: 12px;
  padding: 0 4px;
}
.btn-link.text-danger {
  color: #f87171;
}

.table-empty {
  text-align: center;
  padding: 24px !important;
  color: #64748b;
}

/* Steps */
.steps-container {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.step-card {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 14px 18px;
  background: #14171d;
  border: 1px solid #232b36;
  border-radius: 8px;
}

.step-card.done {
  border-color: #065f46;
}

.step-badge {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: #232b36;
  color: #94a3b8;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 600;
  font-size: 14px;
}

.step-card.done .step-badge {
  background: #064e3b;
  color: #34d399;
}

.step-content {
  flex: 1;
}

.step-title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 14px;
  font-weight: 600;
  color: #f8fafc;
}

.step-status {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  background: #242c38;
  color: #94a3b8;
  font-weight: normal;
}
.step-status.ok {
  background: #064e3b;
  color: #34d399;
}

.step-desc {
  margin-top: 4px;
  font-size: 12px;
  color: #94a3b8;
}

/* Modal */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.modal-box {
  background: #181d24;
  border: 1px solid #333f50;
  border-radius: 10px;
  width: 440px;
  max-width: 90vw;
  box-shadow: 0 20px 40px rgba(0, 0, 0, 0.6);
  overflow: hidden;
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 14px 18px;
  background: #1c222b;
  border-bottom: 1px solid #232b36;
}

.modal-header h3 {
  margin: 0;
  font-size: 14px;
  color: #f8fafc;
}

.modal-close {
  background: none;
  border: none;
  color: #94a3b8;
  cursor: pointer;
  font-size: 16px;
}

.modal-body {
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-group label {
  font-size: 12px;
  color: #94a3b8;
}

.form-hint {
  font-size: 11px;
  color: #64748b;
}

.form-error {
  font-size: 12px;
  color: #f87171;
  background: #451a1a;
  padding: 6px 10px;
  border-radius: 4px;
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 12px 18px;
  background: #14171d;
  border-top: 1px solid #232b36;
}

/* Toast */
.toast {
  position: fixed;
  bottom: 24px;
  right: 24px;
  padding: 10px 18px;
  border-radius: 8px;
  font-size: 13px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
  z-index: 200;
}
.toast-info {
  background: #1e293b;
  color: #e2e8f0;
  border: 1px solid #334155;
}
.toast-success {
  background: #064e3b;
  color: #a7f3d0;
  border: 1px solid #059669;
}
.toast-error {
  background: #7f1d1d;
  color: #fecaca;
  border: 1px solid #b91c1c;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.25s, transform 0.25s;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
  transform: translateY(8px);
}

.font-mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
}
.text-bold {
  font-weight: 600;
}
</style>