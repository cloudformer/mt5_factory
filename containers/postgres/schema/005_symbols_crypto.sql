-- 005_symbols_crypto.sql — 加密品种 (用于 7x24 调试: 外汇/贵金属周末休市, BTCUSD 不停)
--
-- point 用常见券商精度(2位小数, 如 $65432.10)的默认值; 如果你的券商报价精度不同,
-- 用 bridge /symbol/BTCUSD 查真实 digits/point 后改这行重跑即可 (幂等, 不影响其他数据)。
INSERT INTO symbols (symbol, digits, point) VALUES
    ('BTCUSD', 2, 0.01)
ON CONFLICT (symbol) DO NOTHING;
