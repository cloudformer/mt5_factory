# License(用户授权)· 设计与实施步骤(2026-08-14/15)

> 给 Frank 快速回忆进度用;AI 接手时当上下文。设计问答与决策都在这一份里。

## 一句话

**你只管签"一张纸"(每用户一张:到期 / worker 台数 / 策略数),其余全部天生继承** ——
worker 和策略本来就归属 user(`owner_id`/`worker_keys.user_id`),纸只约束数量与期限。

## 定死的设计(2026-08-14 与 Frank 逐条定)

- **纸的格式**:明文 + 签名,没有多余字段
  ```json
  {"license": {"user_id": 3, "name": "acme", "expires": "2026-09-09T23:59:59+00:00",
               "workers": 2, "max_strategies": 100},
   "sig": "base64(Ed25519)"}
  ```
- **公钥在 git 里**(`license/canonical.py`),私钥只在 Frank 机器(`~/.mt5_factory/`);
  **纸里绝不放公钥**(放了=自签自验,形同没签)
- **worker 没有自己的纸**:钥匙(worker_keys)只证明身份,权利全从 user 的纸继承;
  换机器 = 吊旧钥匙腾位 → 发新钥匙,纸不动
- **计数口径**(所有执法共用,users 页已展示):
  策略数 = 非 ARCHIVED(归档不占额度);台数 = enabled 的 worker_keys 数
- **无纸 = 不限**(owner 与存量用户零影响,license 是逐用户 opt-in);
  **有纸但签名无效 = 禁用**(绝不回落"不限" —— 篡改只能把自己搞停)
- **过期 = 不发策略、不删数据、贴新纸即恢复**(持仓有券商侧 SL/TP 保护)
- **执法全在 Frank 的 server 上**(SaaS 模型):客户改自己 Windows 的代码毫无意义,
  领不到策略就是最终执法 → worker 本地验签不需要,整套纯 Linux
- 这是商业授权不是 DRM;签名挡的是"改库/改配置提额"这类篡改

## 两个关键问答(实现的根据)

**Q1 明文和签名如何确保一致?** 签名盖的是"字节"不是"意思"。规范化序列化
(`sort_keys` + 无空格)只写一份 `canonical()`,签发脚本与 api 共 import ——
两边在字节层面天然一致;页面展示的数字全部从验过签的那份解析,结构上不可能分叉。

**Q2 库里的纸如何防篡改?** 库只是抽屉不是权威:每次读出都用 git 公钥重验。
改任何一个字 → 验签失败 → invalid(禁用)。server 只认 Frank 亲手签的条款。

## 步骤与状态

| 步 | 内容 | 状态 |
|---|---|---|
| 1 | **计数尺子 + 用量展示**:users 页 8 列(今日/当月回测 · 今日/当月AI · 活动/归档策略 · worker数 · 有效期);`usage_counters` 加 month 翻篇(schema/071) | ✅ 2026-08-14 |
| 2 | **签发工具**:`scripts/make_license.py`(keygen/sign/verify)+ `license/canonical.py`(规范化与验签唯一实现,公钥常量在此进 git) | ✅ 已建,**keygen 待 Frank 跑** |
| 3 | **纸落库 + 展示**:`users.license` 列(schema/072)+ `PUT /users/{id}/license`(先验签再落库,贴错人/无效拒收)+ 管理页粘贴框 + 有效期列五态 + 额度分母并进用量列;Workers 页「license」行改名「钥匙」(撞名) | ✅ 2026-08-14,零执法 |
| 4a | 执法:发 worker key 时 `count(enabled) < workers` | ⬜ |
| 4b | 执法:建策略时 `count(非ARCHIVED) < max_strategies`(唯一收货管道 `create_instances` 一处管全部) | ⬜ |
| 4c | 执法:过期 → `/strategies/status` 返回空(runner 不加载 = 不开新仓);4a/4b 同判过期 | ⬜ |
| 5 | 用户自助发钥匙(现在 owner-only;额度由 4a 管) | ⬜ 不急 |

## 状态五态(license.parse)

`none` 没纸=不限 | `valid` | `expired` | `invalid` 验签失败/贴错人=禁用 |
`unavailable` 公钥还是占位符(keygen 未跑)=一切按无纸+黄字提示,先部署后 keygen 也安全

## 部署备忘

1. **先跑 keygen**(公钥现在是占位符 `REPLACE_ME_AFTER_KEYGEN`):
   `python scripts/make_license.py keygen` → 备份 `~/.mt5_factory/license_signing.key`
   (丢了=所有纸重签)→ commit `license/canonical.py`(真公钥进 git)
2. **镜像要重建**:requirements 加了 cryptography、Dockerfile 拷了 `license/` →
   `docker compose --env-file env/.dev.env build api worker`
3. 签发:`python scripts/make_license.py sign --user-id N --name xx --expires 2026-12-31
   --workers 2 --max-strategies 100` → 整份 JSON 粘到管理页
4. 验收:用量表出现 `N/额度` 分母与到期日;篡改库里一个字 → 红字"签名无效!"

## 第 3 步实测记录(2026-08-14, 测试密钥, 已销毁)

粘贴有效纸 ✅ · 贴错人 400 ✅ · 库里篡改→invalid ✅ · 贴回恢复 valid ✅ ·
页面 `2199/12000 · 0/3 · 剩139天` ✅ · 清除回无限制 ✅ · 公钥未生成时礼貌拒绝 ✅
