-- 053_regime_versions.sql — Regime 口径版本化(v0.2 设计, 2026-07-31 与 Frank 定)
-- 一套参数 = 一个 version(params UNIQUE 判重执法, 撞上即"这是 vN"); 版本号 = 行 id。
-- regime_timeline 加 version_id 维度: 每版本一套完整时间线, 并存互不覆盖。
-- 页面无删除口; 生成错了 Frank 直接删库: DELETE FROM regime_versions WHERE id=N
-- (ON DELETE CASCADE 连带清掉该版本时间线, 一条语句干净)。
CREATE TABLE IF NOT EXISTS regime_versions (
    id         SERIAL PRIMARY KEY,          -- 版本号 v{id}: 自动编号, 不改不删(删=手工库操作)
    params     JSONB NOT NULL UNIQUE,       -- {"long_ma","short_ma","atr_n","vol_win","vol_q"}
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 种子 v1 = 当前口径(config regime_params, 048 种子/评定期定版值); 只在空表时种一次
INSERT INTO regime_versions (params)
SELECT COALESCE(
         (SELECT value FROM config WHERE key = 'regime_params'),
         '{"long_ma":"sma200","short_ma":"sma20","atr_n":14,"vol_win":252,"vol_q":0.5}'::jsonb)
 WHERE NOT EXISTS (SELECT 1 FROM regime_versions);

-- 当前默认版本指针(config 一处): 存版本 id(数字)
INSERT INTO config (key, value)
SELECT 'regime_version', to_jsonb((SELECT min(id) FROM regime_versions))
ON CONFLICT (key) DO NOTHING;

-- timeline 加版本维: 存量行归属 v1(默认值回填), 主键升级 (version_id, symbol, date)
ALTER TABLE regime_timeline
    ADD COLUMN IF NOT EXISTS version_id INTEGER NOT NULL DEFAULT 1
    REFERENCES regime_versions(id) ON DELETE CASCADE;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.key_column_usage
         WHERE table_name = 'regime_timeline'
           AND constraint_name = 'regime_timeline_pkey'
           AND column_name = 'version_id') THEN
        ALTER TABLE regime_timeline DROP CONSTRAINT regime_timeline_pkey;
        ALTER TABLE regime_timeline ADD CONSTRAINT regime_timeline_pkey
            PRIMARY KEY (version_id, symbol, date);
    END IF;
END $$;
