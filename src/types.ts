export interface Mapping {
  hostname: string;
  port: number;
}

export interface Settings {
  tunnel_name: string;
  mappings: Mapping[];
  routed_hosts: string[];
}

export interface AppStatus {
  cert_exists: boolean;
  tunnel_id: string | null;
  cred_exists: boolean;
  routed_count: number;
  total_hosts: number;
  is_running: boolean;
  is_busy: boolean;
  connections: number;
  status_text: string;
  status_color: string;
}

export interface LogMessage {
  text: string;
  level: "info" | "warn" | "error" | "ok" | "meta";
}
