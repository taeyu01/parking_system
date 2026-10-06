"""테이블 정의 (Spring의 Entity 역할).

테이블: members, parking_spots, reservations
JSON 키 이름은 모바일/파이 담당과 약속한 이름이므로 to_dict()에서만 정한다.
"""
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

SPOT_COUNT = 6  # 주차칸 수 (실제 센서는 1칸, 나머지는 가정)


class ReservationStatus:
    RESERVED = "RESERVED"    # 예약됨 (아직 입차 전)
    ENTERED = "ENTERED"      # 입차(주차장 안에 있음)
    EXITED = "EXITED"        # 출차 완료
    CANCELLED = "CANCELLED"  # 취소 또는 노쇼 자동 취소

    ACTIVE = (RESERVED, ENTERED)  # 진행 중인 예약


class SpotStatus:
    EMPTY = "EMPTY"        # 비어 있음
    RESERVED = "RESERVED"  # 지금 예약이 걸린 칸 (저장하지 않고 조회할 때 계산)
    PARKED = "PARKED"      # 주차 중 (IR 센서 감지)


def fmt_dt(value):
    """datetime -> '2026-09-30 14:20:00', 값이 없으면 None(JSON null)"""
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else None


class Member(db.Model):
    __tablename__ = "members"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(30), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    birth_date = db.Column(db.Date, nullable=False)
    plate_number = db.Column(db.String(10), unique=True, nullable=False)  # 가입 때 등록한 기본 차량번호 (공백 없이 12가3456)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


class ParkingSpot(db.Model):
    __tablename__ = "parking_spots"

    id = db.Column(db.Integer, primary_key=True, autoincrement=False)  # 칸 번호 1~6
    # IR 센서가 알려 준 실시간 상태만 저장한다: EMPTY 또는 PARKED
    # (앱에 보여 줄 RESERVED는 예약 시간을 보고 조회할 때 계산한다)
    status = db.Column(db.String(10), nullable=False, default=SpotStatus.EMPTY)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class Reservation(db.Model):
    __tablename__ = "reservations"
    __table_args__ = (db.Index("ix_reservations_spot_start", "spot_id", "start_at"),)

    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False, index=True)
    spot_id = db.Column(db.Integer, db.ForeignKey("parking_spots.id"), nullable=False)
    plate_number = db.Column(db.String(10), nullable=False, index=True)  # 이 예약에 쓰는 차량번호
    start_at = db.Column(db.DateTime, nullable=False)  # 이용 시작
    end_at = db.Column(db.DateTime, nullable=False)    # 이용 종료 (연장하면 늘어남)
    status = db.Column(db.String(10), nullable=False, default=ReservationStatus.RESERVED)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    entered_at = db.Column(db.DateTime, nullable=True)  # 실제 입차 시각
    exited_at = db.Column(db.DateTime, nullable=True)   # 실제 출차 시각
    # 종료 10분/5분 전 푸시를 보냈는지 기록 (푸시(FCM)를 붙일 때 사용, 지금은 비워 둠)
    notified_10 = db.Column(db.Boolean, nullable=False, default=False)
    notified_5 = db.Column(db.Boolean, nullable=False, default=False)

    def to_dict(self):
        return {
            "reservation_id": self.id,
            "spot_id": self.spot_id,
            "plate_number": self.plate_number,
            "start_at": fmt_dt(self.start_at),
            "end_at": fmt_dt(self.end_at),
            "status": self.status,
            "created_at": fmt_dt(self.created_at),
            "entered_at": fmt_dt(self.entered_at),
            "exited_at": fmt_dt(self.exited_at),
        }
