-- 006_worker_identity_by_name.sql — worker 身份从 IP 改为计算机名
--
-- 病根: 原来身份=IP((host,port) 唯一), DHCP 换 IP / 克隆 / IP 复用都会乱。
-- 改为: 身份 = name(计算机名, bridge 用 socket.gethostname() 上报, 已 UNIQUE);
--       host/port 降级为"当前地址", 每次 announce 刷新。
-- 因此必须删掉 (host,port) 唯一约束 — 否则换 IP 或两台先后拿到同一 IP 会撞。
ALTER TABLE mt5_hosts DROP CONSTRAINT IF EXISTS mt5_hosts_host_port_key;
