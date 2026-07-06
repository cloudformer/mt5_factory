"""app API 薄封装: 统一超时和错误处理"""
import os

import requests

APP_URL = os.getenv("APP_URL", "http://app:8000").rstrip("/")


class ApiError(Exception):
    pass


def get(path: str, **params):
    try:
        r = requests.get(f"{APP_URL}{path}", params=params or None, timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise ApiError(str(e))


def _send(method: str, path: str, payload: dict | None):
    try:
        r = requests.request(method, f"{APP_URL}{path}", json=payload, timeout=30)
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
