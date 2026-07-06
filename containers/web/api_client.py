"""app API 薄封装: 统一超时和错误处理"""
import os

import requests

# 配置只在一处: 必须由 docker-compose.yml 注入, 代码不留兜底值, 缺了立刻报错
API_URL = os.getenv("API_URL", "").rstrip("/")
if not API_URL:
    raise RuntimeError("API_URL not set — 应由 docker-compose.yml environment 注入")


class ApiError(Exception):
    pass


def get(path: str, **params):
    try:
        r = requests.get(f"{API_URL}{path}", params=params or None, timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise ApiError(str(e))


def _send(method: str, path: str, payload: dict | None):
    try:
        r = requests.request(method, f"{API_URL}{path}", json=payload, timeout=30)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:200]
            raise ApiError(f"{r.status_code}: {detail}")
        return r.json()
    except requests.RequestException as e:
        raise ApiError(str(e))


def post(path: str, payload: dict | None = None):
    return _send("POST", path, payload)


def put(path: str, payload: dict | None = None):
    return _send("PUT", path, payload)


def post_patch(path: str, payload: dict | None = None):
    return _send("PATCH", path, payload)


def delete(path: str):
    return _send("DELETE", path, None)
