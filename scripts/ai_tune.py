#!/usr/bin/env python3
"""AI 调参一轮(Claude 订阅版, 无 API key): 在装了 Claude Code 且已登录的机器上跑。

    python3 scripts/ai_tune.py 177                # 默认 10 组, web=http://192.168.4.139:8000
    python3 scripts/ai_tune.py 177 --count 12 --base http://localhost:8000

流程: 拉 /strategies/ai/prompt.txt(成绩单+纪律已拼好) → 喂 `claude -p`(用你登录的订阅额度)
      → 提取返回 JSON → POST 回 /strategies/ai/submit(校验/生成子代; 回测在页面第3步手动)
产出: 打印新生成的策略 ID 清单; 之后去「AI 策略分析」页看家族对比表。
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("strategy_id", type=int)
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--base", default="http://192.168.4.139:8000", help="web 地址")
    a = ap.parse_args()

    # 1. 拉提示词(服务端已拼好: 指令纪律 + 参数空间 + 完整成绩单)
    url = f"{a.base}/strategies/ai/prompt.txt?strategy_id={a.strategy_id}&count={a.count}"
    prompt = urllib.request.urlopen(url, timeout=60).read().decode()
    if prompt.startswith("error:"):
        sys.exit(f"取提示词失败: {prompt}")
    print(f"[1/3] 提示词 {len(prompt)} 字符, 喂给 claude -p ...")

    # 2. Claude Code 无头模式(订阅额度, 无需 API key)
    r = subprocess.run(["claude", "-p", "--output-format", "text"],
                       input=prompt, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        sys.exit(f"claude -p 失败: {r.stderr[:500]}")
    out = r.stdout.strip()

    # 3. 提取 JSON(容错: 剥代码围栏/前后杂文字)
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        sys.exit(f"Claude 输出里找不到 JSON:\n{out[:800]}")
    combos = json.loads(m.group(0))
    n = len(combos.get("combos", combos) if isinstance(combos, dict) else combos)
    print(f"[2/3] Claude 返回 {n} 组参数, 提交校验+生成子代(回测由页面第3步手动触发)...")

    # 4. 提交(走 web 表单端点, 与页面第2步同一入口)
    form = {"strategy_id": str(a.strategy_id),
            "combos_json": json.dumps(combos, ensure_ascii=False)}
    req = urllib.request.Request(f"{a.base}/strategies/ai/submit",
                                 data=urllib.parse.urlencode(form).encode())
    resp = urllib.request.urlopen(req, timeout=120)
    print(f"[3/3] 子代已生成(HTTP {resp.status})。"
          f"去「AI 策略分析」页输入 {a.strategy_id}: 第2步结果表看新ID与核验,"
          f" 第3步手动启动回测, 第4步看家族对比。")


if __name__ == "__main__":
    main()
