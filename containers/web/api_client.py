"""app API 薄封装: 统一超时和错误处理"""
import os

import requests

# 配置只在一处: 必须由 docker-compose.yml 注入, 代码不留兜底值, 缺了立刻报错
API_URL = os.getenv("API_URL", "").rstrip("/")
if not API_URL:
    raise RuntimeError("API_URL not set — 应由 docker-compose.yml environment 注入")


class ApiError(Exception):
    pass


def _identity_headers() -> dict:
    """当前身份捎给 api(v5.6 通电): X-User-Id = 右上角切换的用户(app.before_request 已落 session)。
    api 读路径按它过滤资产; 登录上线后这里换成真凭据, 视图代码零改动。"""
    try:
        from flask import has_request_context, session
        if has_request_context() and session.get("dev_user_id") is not None:
            return {"X-User-Id": str(session["dev_user_id"])}
    except Exception:
        pass
    return {}


def parse_ids(raw: str) -> list[int]:
    """全站唯一的策略ID串解析(2026-08-15 Frank 定): 规范格式 = 逗号分隔 1,2,3(所有输出统一),
    输入宽容 — 方括号/空格/换行/分号/中文逗号一律当分隔符(旧报告复制的 [1, 2, 3] 也认);
    真垃圾(非数字)仍抛 ValueError, 由调用方 flash 提示"""
    import re
    toks = re.split(r"[,\uFF0C;\s]+", (raw or "").replace("[", " ").replace("]", " "))
    return [int(t) for t in toks if t]


def get(path: str, timeout: int = 15, **params):
    # timeout 是保留形参(不进查询串): 长现算端点(如候选族对比)显式放宽
    try:
        r = requests.get(f"{API_URL}{path}", params=params or None, timeout=timeout,
                         headers=_identity_headers())
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise ApiError(str(e))


def _send(method: str, path: str, payload: dict | None, timeout: int = 30):
    try:
        r = requests.request(method, f"{API_URL}{path}", json=payload, timeout=timeout,
                             headers=_identity_headers())
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:200]
            raise ApiError(f"{r.status_code}: {detail}")
        return r.json()
    except requests.RequestException as e:
        raise ApiError(str(e))


def post(path: str, payload: dict | None = None, timeout: int = 30):
    return _send("POST", path, payload, timeout)   # timeout: 长任务(如插件批跑)可放宽


def put(path: str, payload: dict | None = None):
    return _send("PUT", path, payload)


def post_patch(path: str, payload: dict | None = None):
    return _send("PATCH", path, payload)


def delete(path: str):
    return _send("DELETE", path, None)
