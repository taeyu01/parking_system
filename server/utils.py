"""공통 도구: 설정값, 입력 검증, 시간 처리, 비밀번호 해시, 응답 헬퍼"""
import math
import os
import re
from datetime import datetime

from dotenv import load_dotenv
from flask import jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()  # 아래 설정값이 .env를 읽을 수 있도록 먼저 불러온다


# =====================================================================
# 설정값 (기본값이 있고, 바꾸고 싶으면 .env에 같은 이름으로 적으면 된다)
# =====================================================================
def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


SLOT_MINUTES = _env_int("SLOT_MINUTES", 30)                      # 이용 시간 선택 단위(분)
DEFAULT_DURATION_MINUTES = _env_int("DEFAULT_DURATION_MINUTES", 120)  # 이용 시간을 안 보냈을 때 기본값
MAX_DURATION_MINUTES = _env_int("MAX_DURATION_MINUTES", 720)     # 예약 하나의 최대 이용 시간(12시간)
BUFFER_MINUTES = _env_int("BUFFER_MINUTES", 15)                  # 같은 칸 예약 사이에 필요한 간격(분)
ENTRY_EARLY_MINUTES = _env_int("ENTRY_EARLY_MINUTES", 10)        # 시작 몇 분 전부터 입장 가능한지
NOSHOW_MINUTES = _env_int("NOSHOW_MINUTES", 30)                  # 시작 후 몇 분이 지나면 노쇼로 자동 취소할지
ALERT_MINUTES = _env_int("ALERT_MINUTES", 10)                    # 종료 몇 분 전부터 알림(alert)을 줄지
START_GRACE_MINUTES = 5                                          # 시작 시각을 과거로 보내도 봐주는 오차(분)
MAX_ADVANCE_DAYS = 30                                            # 며칠 뒤까지 예약할 수 있는지

PLATE_RE = re.compile(r"[0-9]{2,3}[가-힣][0-9]{4}")
USERNAME_RE = re.compile(r"[A-Za-z0-9_]{4,20}")
DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
)


# ---------- 요청 ----------
def get_body():
    """요청 JSON을 dict로 반환. JSON이 아니거나 깨졌으면 빈 dict."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def to_int(value):
    """'3' / 3 -> 3, 변환 불가능하면 None"""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------- 검증 ----------
def normalize_plate(value):
    """공백 제거: '12가 3456' -> '12가3456'"""
    return re.sub(r"\s+", "", str(value or ""))


def is_valid_plate(plate):
    return PLATE_RE.fullmatch(plate) is not None


def is_valid_username(username):
    return USERNAME_RE.fullmatch(username) is not None


def parse_birth(value):
    """'2000-01-01' -> date, 형식/날짜가 틀리면 None"""
    try:
        return datetime.strptime(str(value or ""), "%Y-%m-%d").date()
    except ValueError:
        return None


# ---------- 시간 ----------
def now_local():
    """현재 시각 (초 단위로 맞춤. DB에 저장/비교할 때 소수점 초 때문에 어긋나지 않게)"""
    return datetime.now().replace(microsecond=0)


def parse_datetime(value):
    """'2026-10-05 14:00:00' 같은 문자열 -> datetime, 형식이 틀리면 None"""
    text = str(value or "").strip()
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def minutes_until(target, now):
    """target까지 남은 분 (올림). 이미 지났으면 0"""
    seconds = (target - now).total_seconds()
    return max(0, math.ceil(seconds / 60))


# ---------- 비밀번호 ----------
def hash_password(password):
    return generate_password_hash(password)


def verify_password(password_hash, password):
    return check_password_hash(password_hash, password)


# ---------- 응답 ----------
def ok(**data):
    """성공 응답: {"success": true, ...}"""
    payload = {"success": True}
    payload.update(data)
    return jsonify(payload)


def fail(message, status=200):
    """실패 응답: {"success": false, "message": "이유"}

    업무 규칙 위반은 HTTP 200으로 보내고 success 값으로 판단하게 한다.
    (앱에서 HTTP 오류 처리를 따로 안 해도 message를 그대로 띄울 수 있음)
    """
    return jsonify({"success": False, "message": message}), status
