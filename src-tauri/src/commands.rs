use crate::state::{
    classify_log, get_base_dir, get_cloudflared_path, get_data_dir, parse_conn_index, AppState,
    AppStatus, LogMessage, Mapping, Settings, DEFAULT_HOSTNAME, DEFAULT_PORT, DEFAULT_TUNNEL_NAME,
};
use std::collections::HashSet;
use std::fs;
use std::path::PathBuf;
use std::process::Stdio;
use std::time::SystemTime;
use tauri::{AppHandle, Emitter, State};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

fn kill_pid(pid: u32) {
    #[cfg(windows)]
    {
        let _ = std::process::Command::new("taskkill")
            .args(["/F", "/T", "/PID", &pid.to_string()])
            .creation_flags(CREATE_NO_WINDOW)
            .output();
    }
    #[cfg(not(windows))]
    {
        let _ = std::process::Command::new("kill")
            .args(["-9", &pid.to_string()])
            .output();
    }
}

#[cfg(windows)]
use std::os::windows::process::CommandExt;

fn make_cf_command(args: &[&str]) -> Command {
    let cf_path = get_cloudflared_path();
    let mut cmd = Command::new(&cf_path);
    cmd.current_dir(get_base_dir());
    cmd.args(args);
    cmd.stdout(Stdio::piped());
    cmd.stderr(Stdio::piped());
    #[cfg(windows)]
    {
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd
}

#[tauri::command]
pub fn get_app_status(state: State<'_, AppState>) -> Result<AppStatus, String> {
    let data_dir = get_data_dir();
    let cert_file = data_dir.join("cert.pem");
    let cert_exists = cert_file.exists();

    let tid_file = data_dir.join("tunnel-id.txt");
    let mut tunnel_id = None;
    let mut cred_exists = false;
    if tid_file.exists() {
        if let Ok(content) = fs::read_to_string(&tid_file) {
            let id = content.trim().to_string();
            if !id.is_empty() {
                cred_exists = data_dir.join(format!("{}.json", id)).exists();
                tunnel_id = Some(id);
            }
        }
    }

    let settings = load_settings().unwrap_or_default();
    let routed_set: HashSet<_> = settings.routed_hosts.into_iter().collect();
    let total_hosts = settings.mappings.len();
    let routed_count = settings
        .mappings
        .iter()
        .filter(|m| routed_set.contains(&m.hostname))
        .count();

    let lock = state.inner.lock().map_err(|e| e.to_string())?;

    Ok(AppStatus {
        cert_exists,
        tunnel_id,
        cred_exists,
        routed_count,
        total_hosts,
        is_running: lock.is_running_tunnel,
        is_busy: lock.is_busy,
        connections: lock.connections,
        status_text: lock.status_text.clone(),
        status_color: lock.status_color.clone(),
    })
}

#[tauri::command]
pub fn load_settings() -> Result<Settings, String> {
    let path = get_data_dir().join("gui-settings.json");
    if !path.exists() {
        return Ok(Settings::default());
    }

    let content = fs::read_to_string(&path).map_err(|e| e.to_string())?;
    let val: serde_json::Value = serde_json::from_str(&content).unwrap_or(serde_json::json!({}));

    let tunnel_name = val
        .get("tunnel_name")
        .and_then(|v| v.as_str())
        .unwrap_or(DEFAULT_TUNNEL_NAME)
        .to_string();

    let mut mappings = Vec::new();
    if let Some(list) = val.get("mappings").and_then(|v| v.as_array()) {
        for item in list {
            if let (Some(h), Some(p)) = (
                item.get("hostname").and_then(|v| v.as_str()),
                item.get("port").and_then(|v| v.as_u64()),
            ) {
                let h_clean = h.trim();
                if !h_clean.is_empty() && (1..=65535).contains(&p) {
                    mappings.push(Mapping {
                        hostname: h_clean.to_string(),
                        port: p as u16,
                    });
                }
            }
        }
    }

    if mappings.is_empty() {
        if let Some(legacy_host) = val.get("hostname").and_then(|v| v.as_str()) {
            let legacy_port = val
                .get("port")
                .and_then(|v| v.as_u64())
                .unwrap_or(DEFAULT_PORT as u64) as u16;
            mappings.push(Mapping {
                hostname: legacy_host.trim().to_string(),
                port: legacy_port,
            });
        } else {
            mappings.push(Mapping {
                hostname: DEFAULT_HOSTNAME.to_string(),
                port: DEFAULT_PORT,
            });
        }
    }

    let mut routed_hosts = Vec::new();
    if let Some(list) = val.get("routed_hosts").and_then(|v| v.as_array()) {
        for item in list {
            if let Some(s) = item.as_str() {
                routed_hosts.push(s.to_string());
            }
        }
    }

    Ok(Settings {
        tunnel_name,
        mappings,
        routed_hosts,
    })
}

#[tauri::command]
pub fn save_settings(settings: Settings) -> Result<(), String> {
    let path = get_data_dir().join("gui-settings.json");
    let json = serde_json::to_string_pretty(&settings).map_err(|e| e.to_string())?;
    fs::write(path, json).map_err(|e| e.to_string())?;
    Ok(())
}

fn emit_log(app: &AppHandle, text: String, level: &str) {
    let _ = app.emit(
        "tunnel-log",
        LogMessage {
            text,
            level: level.to_string(),
        },
    );
}

fn get_cert_candidates() -> Vec<PathBuf> {
    let mut list = Vec::new();
    if let Some(home) = dirs::home_dir() {
        list.push(home.join(".cloudflared").join("cert.pem"));
    }
    let base = get_base_dir();
    list.push(base.join("cert.pem"));
    list.push(base.join(".cloudflared").join("cert.pem"));
    list
}

mod dirs {
    use std::path::PathBuf;
    pub fn home_dir() -> Option<PathBuf> {
        #[cfg(windows)]
        {
            if let Ok(prof) = std::env::var("USERPROFILE") {
                return Some(PathBuf::from(prof));
            }
        }
        std::env::var("HOME").ok().map(PathBuf::from)
    }
}

#[tauri::command]
pub async fn start_login(app: AppHandle, state: State<'_, AppState>) -> Result<(), String> {
    {
        let mut lock = state.inner.lock().map_err(|e| e.to_string())?;
        if lock.is_busy || lock.is_running_tunnel {
            return Err("已有任务或 Tunnel 正在运行".to_string());
        }
        lock.is_busy = true;
    }

    emit_log(
        &app,
        "[Login] 启动浏览器授权，请在浏览器中完成登录...\n".to_string(),
        "meta",
    );

    let candidates = get_cert_candidates();
    let mut baseline: Vec<(PathBuf, Option<SystemTime>)> = Vec::new();
    for p in &candidates {
        let mtime = fs::metadata(p).and_then(|m| m.modified()).ok();
        baseline.push((p.clone(), mtime));
    }

    let mut cmd = make_cf_command(&["tunnel", "login"]);
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            let mut lock = state.inner.lock().unwrap();
            lock.is_busy = false;
            let msg = format!("[Login] 启动失败: {}\n", e);
            emit_log(&app, msg.clone(), "error");
            return Err(msg);
        }
    };

    let pid = child.id().unwrap_or(0);
    {
        let mut lock = state.inner.lock().unwrap();
        lock.active_pid = Some(pid);
    }

    let app_clone = app.clone();
    let state_clone = state.inner.clone();

    tokio::spawn(async move {
        let target_cert = get_data_dir().join("cert.pem");
        let mut found = false;

        for _ in 0..120 {
            tokio::time::sleep(tokio::time::Duration::from_millis(1000)).await;

            for (p, prev_mtime) in &baseline {
                if p.exists() {
                    let cur_mtime = fs::metadata(p).and_then(|m| m.modified()).ok();
                    let is_newer = match (cur_mtime, prev_mtime) {
                        (Some(cur), Some(prev)) => cur > *prev,
                        (Some(_), None) => true,
                        _ => false,
                    };
                    if is_newer {
                        emit_log(
                            &app_clone,
                            format!("[Login] 检测到新证书: {:?}，正在迁移...\n", p),
                            "ok",
                        );
                        if let Err(e) = fs::copy(p, &target_cert) {
                            emit_log(&app_clone, format!("[Login] 复制失败: {}\n", e), "error");
                        } else {
                            emit_log(
                                &app_clone,
                                format!("[Login] 证书已成功安装到 {:?}\n", target_cert),
                                "ok",
                            );
                            found = true;
                            break;
                        }
                    }
                }
            }

            if found {
                break;
            }

            if let Ok(Some(status)) = child.try_wait() {
                emit_log(
                    &app_clone,
                    format!("[Login] 登录命令已结束: {:?}\n", status),
                    "meta",
                );
                break;
            }
        }

        if pid > 0 {
            kill_pid(pid);
        }

        if !found && target_cert.exists() {
            emit_log(
                &app_clone,
                format!("[Login] 已检测到已有证书: {:?}\n", target_cert),
                "ok",
            );
        } else if !target_cert.exists() {
            emit_log(&app_clone, "[Login] 未检测到证书写入\n".to_string(), "warn");
        }

        let mut lock = state_clone.lock().unwrap();
        lock.is_busy = false;
        lock.active_pid = None;
        let _ = app_clone.emit("status-refresh", ());
    });

    Ok(())
}

#[tauri::command]
pub async fn start_create_tunnel(
    name: String,
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<String, String> {
    {
        let mut lock = state.inner.lock().map_err(|e| e.to_string())?;
        if lock.is_busy || lock.is_running_tunnel {
            return Err("任务正在运行中".to_string());
        }
        lock.is_busy = true;
    }

    let cert_path = get_data_dir().join("cert.pem");
    if !cert_path.exists() {
        let mut lock = state.inner.lock().unwrap();
        lock.is_busy = false;
        return Err("cert.pem 缺失，请先执行登录".to_string());
    }

    emit_log(
        &app,
        format!("[Create] 开始创建 Tunnel: {}\n", name),
        "meta",
    );

    let cert_str = cert_path.to_string_lossy().to_string();
    let mut cmd = make_cf_command(&["tunnel", "--origincert", &cert_str, "create", &name]);
    let output = cmd.output().await.map_err(|e| {
        let mut lock = state.inner.lock().unwrap();
        lock.is_busy = false;
        e.to_string()
    })?;

    let stdout_str = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr_str = String::from_utf8_lossy(&output.stderr).to_string();

    if !stdout_str.is_empty() {
        emit_log(&app, stdout_str, "info");
    }
    if !stderr_str.is_empty() {
        emit_log(&app, stderr_str.clone(), "info");
    }

    let data_dir = get_data_dir();
    let mut latest_uuid: Option<String> = None;
    if let Ok(entries) = fs::read_dir(&data_dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|s| s.to_str()) == Some("json") {
                if let Some(stem) = path.file_stem().and_then(|s| s.to_str()) {
                    if stem != "gui-settings" && stem.len() >= 32 {
                        latest_uuid = Some(stem.to_string());
                    }
                }
            }
        }
    }

    let mut lock = state.inner.lock().unwrap();
    lock.is_busy = false;

    if let Some(uuid) = latest_uuid {
        let tid_file = data_dir.join("tunnel-id.txt");
        let _ = fs::write(&tid_file, &uuid);
        emit_log(
            &app,
            format!("[Create] 凭证就绪，UUID={}\n", uuid),
            "ok",
        );
        let _ = app.emit("status-refresh", ());
        Ok(uuid)
    } else {
        emit_log(&app, "[Create] 未发现凭证 JSON\n".to_string(), "error");
        Err("创建未生成凭据文件".to_string())
    }
}

#[tauri::command]
pub async fn start_route_dns(
    name: String,
    host: String,
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<(), String> {
    {
        let mut lock = state.inner.lock().map_err(|e| e.to_string())?;
        if lock.is_busy || lock.is_running_tunnel {
            return Err("任务正在运行中".to_string());
        }
        lock.is_busy = true;
    }

    let cert_path = get_data_dir().join("cert.pem");
    let cert_str = cert_path.to_string_lossy().to_string();

    emit_log(
        &app,
        format!("[Route] 绑定 DNS: {} -> Tunnel {}\n", host, name),
        "meta",
    );

    let mut cmd = make_cf_command(&[
        "tunnel",
        "--origincert",
        &cert_str,
        "route",
        "dns",
        &name,
        &host,
    ]);

    let output = cmd.output().await.map_err(|e| {
        let mut lock = state.inner.lock().unwrap();
        lock.is_busy = false;
        e.to_string()
    })?;

    let stdout_str = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr_str = String::from_utf8_lossy(&output.stderr).to_string();

    if !stdout_str.is_empty() {
        emit_log(&app, stdout_str, "info");
    }
    if !stderr_str.is_empty() {
        emit_log(&app, stderr_str.clone(), "info");
    }

    let success = output.status.success() || stderr_str.contains("already exists") || stderr_str.contains("is already configured");

    let mut lock = state.inner.lock().unwrap();
    lock.is_busy = false;

    if success {
        let mut s = load_settings().unwrap_or_default();
        if !s.routed_hosts.contains(&host) {
            s.routed_hosts.push(host.clone());
            let _ = save_settings(s);
        }
        emit_log(&app, format!("[Route] {} CNAME 绑定成功\n", host), "ok");
        let _ = app.emit("status-refresh", ());
        Ok(())
    } else {
        emit_log(
            &app,
            format!("[Route] {} 绑定失败，可能已有 DNS 记录冲突\n", host),
            "error",
        );
        Err(format!("Route failed with status {:?}", output.status))
    }
}

fn generate_runtime_config(tunnel_name: &str, uuid: &str, mappings: &[Mapping]) -> Result<PathBuf, String> {
    let data_dir = get_data_dir();
    let cred_file = data_dir.join(format!("{}.json", uuid));
    let cert_file = data_dir.join("cert.pem");

    if !cred_file.exists() {
        return Err(format!("凭证缺失: {:?}", cred_file));
    }
    if mappings.is_empty() {
        return Err("没有任何端口映射".to_string());
    }

    let mut ingress_yaml = String::new();
    ingress_yaml.push_str("ingress:\n");
    for m in mappings {
        ingress_yaml.push_str(&format!("  - hostname: {}\n", m.hostname));
        ingress_yaml.push_str(&format!("    service: http://localhost:{}\n", m.port));
    }
    ingress_yaml.push_str("  - service: http_status:404\n");

    let config_content = format!(
        "tunnel: {}\ncredentials-file: {}\norigincert: {}\n\n{}",
        tunnel_name,
        cred_file.to_string_lossy(),
        cert_file.to_string_lossy(),
        ingress_yaml
    );

    let config_file = data_dir.join("config.runtime.yml");
    fs::write(&config_file, config_content).map_err(|e| e.to_string())?;
    Ok(config_file)
}

#[tauri::command]
pub async fn start_tunnel(app: AppHandle, state: State<'_, AppState>) -> Result<(), String> {
    let settings = load_settings()?;
    let tid_file = get_data_dir().join("tunnel-id.txt");
    if !tid_file.exists() {
        return Err("请先创建 Tunnel 并生成凭证".to_string());
    }
    let uuid = fs::read_to_string(tid_file)
        .map_err(|e| e.to_string())?
        .trim()
        .to_string();

    let config_path = generate_runtime_config(&settings.tunnel_name, &uuid, &settings.mappings)?;
    let cert_path = get_data_dir().join("cert.pem");

    {
        let mut lock = state.inner.lock().map_err(|e| e.to_string())?;
        if lock.is_running_tunnel || lock.is_busy {
            return Err("已有 Tunnel 或任务正在运行".to_string());
        }
        lock.is_running_tunnel = true;
        lock.connections = 0;
        lock.connected_indices.clear();
        lock.status_text = "正在连接...".to_string();
        lock.status_color = "#dcdcaa".to_string();
    }

    emit_log(
        &app,
        format!(
            "[Tunnel] 正在启动 Tunnel: {} (UUID={})\n",
            settings.tunnel_name, uuid
        ),
        "meta",
    );
    for m in &settings.mappings {
        emit_log(
            &app,
            format!("[配置] {} -> localhost:{}\n", m.hostname, m.port),
            "meta",
        );
    }

    let cert_str = cert_path.to_string_lossy().to_string();
    let config_str = config_path.to_string_lossy().to_string();

    let mut cmd = make_cf_command(&[
        "tunnel",
        "--origincert",
        &cert_str,
        "--config",
        &config_str,
        "run",
        &settings.tunnel_name,
    ]);

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            let mut lock = state.inner.lock().unwrap();
            lock.is_running_tunnel = false;
            lock.status_text = "启动失败".to_string();
            lock.status_color = "#f48771".to_string();
            let _ = app.emit("status-refresh", ());
            let msg = format!("[Tunnel] 启动进程失败: {}\n", e);
            emit_log(&app, msg.clone(), "error");
            return Err(msg);
        }
    };

    let pid = child.id().unwrap_or(0);
    {
        let mut lock = state.inner.lock().unwrap();
        lock.active_pid = Some(pid);
    }

    let stdout = child.stdout.take().unwrap();
    let stderr = child.stderr.take().unwrap();

    let app_for_stdout = app.clone();
    let state_for_stdout = state.inner.clone();

    // 监听 stdout
    tokio::spawn(async move {
        let mut reader = BufReader::new(stdout).lines();
        while let Ok(Some(line)) = reader.next_line().await {
            process_tunnel_line(&line, &app_for_stdout, &state_for_stdout);
        }
    });

    let app_for_stderr = app.clone();
    let state_for_stderr = state.inner.clone();

    // 监听 stderr
    tokio::spawn(async move {
        let mut reader = BufReader::new(stderr).lines();
        while let Ok(Some(line)) = reader.next_line().await {
            process_tunnel_line(&line, &app_for_stderr, &state_for_stderr);
        }
    });

    let app_for_exit = app.clone();
    let state_for_exit = state.inner.clone();

    tokio::spawn(async move {
        let status = child.wait().await;
        let mut lock = state_for_exit.lock().unwrap();
        lock.is_running_tunnel = false;
        lock.active_pid = None;
        lock.connections = 0;
        lock.connected_indices.clear();
        lock.status_text = "已停止".to_string();
        lock.status_color = "#888888".to_string();

        emit_log(
            &app_for_exit,
            format!("[Tunnel] 进程退出: {:?}\n", status),
            "meta",
        );
        let _ = app_for_exit.emit("status-refresh", ());
    });

    let _ = app.emit("status-refresh", ());
    Ok(())
}

fn process_tunnel_line(
    line: &str,
    app: &AppHandle,
    state: &std::sync::Arc<std::sync::Mutex<crate::state::InnerState>>,
) {
    let level = classify_log(line);
    emit_log(app, format!("{}\n", line), &level);

    let low = line.to_lowercase();
    let idx = parse_conn_index(line);

    let mut update_status = false;
    {
        let mut lock = state.lock().unwrap();
        if let Some(i) = idx {
            if low.contains("registered") && !low.contains("unregister") {
                lock.connected_indices.insert(i);
                update_status = true;
            } else if low.contains("unregistered") || low.contains("lost connection") {
                lock.connected_indices.remove(&i);
                update_status = true;
            }
        }

        if update_status {
            lock.connections = lock.connected_indices.len();
            if lock.connections >= 1 {
                lock.status_text = "已连接 (HTTPS 就绪)".to_string();
                lock.status_color = "#4ec94e".to_string();
            } else if lock.is_running_tunnel {
                lock.status_text = "断开重连中".to_string();
                lock.status_color = "#dcdcaa".to_string();
            }
        }
    }

    if update_status {
        let _ = app.emit("status-refresh", ());
    }
}

#[tauri::command]
pub async fn stop_tunnel(app: AppHandle, state: State<'_, AppState>) -> Result<(), String> {
    emit_log(&app, "[操作] 正在停止 Tunnel...\n".to_string(), "meta");
    let pid = {
        let lock = state.inner.lock().map_err(|e| e.to_string())?;
        lock.active_pid
    };

    if let Some(p) = pid {
        kill_pid(p);
    }

    {
        let mut lock = state.inner.lock().unwrap();
        lock.is_running_tunnel = false;
        lock.is_busy = false;
        lock.active_pid = None;
        lock.connections = 0;
        lock.connected_indices.clear();
        lock.status_text = "已停止".to_string();
        lock.status_color = "#888888".to_string();
    }

    let _ = app.emit("status-refresh", ());
    Ok(())
}
