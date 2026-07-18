-- 028_drop_dead_config_keys.sql — 清僵尸配置键(2026-07-18)
-- config.symbols / config.data_start 是 005 之前的老配置(下载哪些品种 + 全局起始日),
-- 005 起 symbols 表成为品种唯一数据源(每品种 download 开关 + data_start 列)接管了它们。
-- 现在无任何代码读这俩 key(CONFIG_KEYS 也不含), 属僵尸种子(001 种入)。删掉, 免维护。
-- (001 按规矩不改; 新库先种入再被本文件删, 结果一致)
DELETE FROM config WHERE key IN ('symbols', 'data_start');
