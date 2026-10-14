use regex::Regex;
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use tokio::process::Child;

pub const DEFAULT_TUNNEL_NAME: &str = "cf-tunnel-senx";
pub const DEFAULT_HOSTNAME: &str = "cf-tunnel.senx.de5.net";
pub const DEFAULT_PORT: u16 = 8080;

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Mapping {
    pub hostname: String,
    pub port: u16,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Settings {
    pub tunnel_name: String,
    pub mappings: Vec<Mapping>,
    pub routed_hosts: Vec<String>,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            tunnel_name: DEFAULT_TUNNEL_NAME.to_string(),
            mappings: vec![Mapping {
                hostname: DEFAULT_HOSTNAME.to_string(),
                port: DEFAULT_PORT,
            }],
            routed_hosts: vec![],
        }
    }
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct AppStatus {
    pub cert_exists: bool,
    pub tunnel_id: Option<String>,
    pub cred_exists: bool,
    pub routed_count: usize,
    pub total_hosts: usize,
    pub is_running: bool,
    pub is_busy: bool,
    pub connections: usize,
    pub status_text: String,
    pub status_color: String,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct LogMessage {
    pub text: String,
    pub level: String, // "info", "warn", "error", "ok", "meta"
}

pub struct InnerState {
    pub active_child: Option<Child>,
    pub active_pid: Option<u32>,
    pub is_running_tunnel: bool,
    pub is_busy: bool,
    pub connections: usize,
    pub connected_indices: HashSet<u32>,
    pub status_text: String,
    pub status_color: String,
}

#[derive(Clone)]
pub struct AppState {
    pub inner: Arc<Mutex<InnerState>>,
}

impl AppState {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(InnerState {
                active_child: None,
                active_pid: None,
                is_running_tunnel: false,
                is_busy: false,
                connections: 0,
                connected_indices: HashSet::new(),
                status_text: "未启动".to_string(),
                status_color: "#888888".to_string(),
            })),
        }
    }
}

pub fn get_base_dir() -> PathBuf {
    if let Ok(manifest_dir) = std::env::var("CARGO_MANIFEST_DIR") {
        let p = PathBuf::from(manifest_dir);
        if let Some(parent) = p.parent() {
            return parent.to_path_buf();
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            if parent.join("cloudflared.exe").exists() || parent.join("data").exists() {
                return parent.to_path_buf();
            }
        }
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

pub fn get_data_dir() -> PathBuf {
    let d = get_base_dir().join("data");
    let _ = std::fs::create_dir_all(&d);
    d
}

pub fn get_cloudflared_path() -> PathBuf {
    let base = get_base_dir();
    let direct = base.join("cloudflared.exe");
    if direct.exists() {
        return direct;
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            let p = parent.join("cloudflared.exe");
            if p.exists() {
                return p;
            }
        }
    }
    PathBuf::from("cloudflared.exe")
}

pub fn classify_log(line: &str) -> String {
    let low = line.to_lowercase();
    if low.contains("err") || low.contains("failed") || low.contains("fatal") {
        "error".to_string()
    } else if low.contains("warn") || low.contains("retry") {
        "warn".to_string()
    } else if low.contains("registered") || low.contains("added cname") || low.contains("route propagating") {
        "ok".to_string()
    } else {
        "info".to_string()
    }
}

pub fn parse_conn_index(line: &str) -> Option<u32> {
    let re = Regex::new(r"connIndex=(\d+)").ok()?;
    let cap = re.captures(line)?;
    cap.get(1)?.as_str().parse::<u32>().ok()
}
