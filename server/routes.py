"""API 전부 (Spring의 Controller + 서비스 로직 역할).

서버가 상태의 최종 관리자다. 앱/파이가 요청해도 규칙에 맞지 않으면 상태를 바꾸지 않는다.

[앱용]   POST /signup, POST /login,
         POST /reservations, POST /reservations/<id>/cancel, POST /reservations/<id>/extend,
         GET /reservations?member_id=, GET /spots
[파이용] GET /reservations/check, POST /parking/enter, POST /parking/exit,
         POST /spots/<id>/status
"""
from datetime import date, datetime, timedelta

from flask import Blueprint, current_app, request
from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError

from models import (
    SPOT_COUNT,
    Member,
    ParkingSpot,
    Reservation,
    ReservationStatus as RS,
    SpotStatus as SS,
    db,
    fmt_dt,
)
from utils import (
    ALERT_MINUTES,
    BUFFER_MINUTES,
    DEFAULT_DURATION_MINUTES,
    ENTRY_EARLY_MINUTES,
    EXIT_REQUEST_MINUTES,
    MAX_ADVANCE_DAYS,
    MAX_DURATION_MINUTES,
    MIN_GAP_MINUTES,
    NOSHOW_MINUTES,
    SLOT_MINUTES,
    START_GRACE_MINUTES,
    fail,
    get_body,
    hash_password,
    is_valid_plate,
    is_valid_username,
    minutes_until,
    normalize_plate,
    now_local,
    ok,
    parse_birth,
    parse_datetime,
    to_int,
    verify_password,
)

api = Blueprint("api", __name__)


# =====================================================================
# 노쇼 자동 취소: 어떤 요청이 오든 먼저 처리한다 (별도 스케줄러 없이 조회 시점에 정리)
# =====================================================================
@api.before_request
def _expire_noshows():
    if request.endpoint == "api.health":
        return None
    cutoff = now_local() - timedelta(minutes=NOSHOW_MINUTES)
    db.session.execute(
        update(Reservation)
        .where(Reservation.status == RS.RESERVED, Reservation.start_at < cutoff)
        .values(status=RS.CANCELLED)
    )
    db.session.commit()
    return None


# =====================================================================
# 내부 헬퍼: 잠금
# =====================================================================
def _lock_all_spots():
    """6개 칸 행을 번호 순서대로 모두 잠근다.

    예약 등록/연장은 시간이 겹치는지 확인한 뒤 저장하는 두 단계라서, 동시에 들어오면 둘 다 통과할 수 있다.
    그래서 예약 등록/연장은 이 잠금을 먼저 잡아 한 번에 하나씩만 처리한다. (항상 같은 순서로 잠가 교착 방지)
    """
    db.session.execute(select(ParkingSpot).order_by(ParkingSpot.id).with_for_update()).scalars().all()


def _lock_spot(spot_id):
    stmt = (
        select(ParkingSpot)
        .where(ParkingSpot.id == spot_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return db.session.execute(stmt).scalars().first()


def _lock_reservation(reservation_id):
    stmt = (
        select(Reservation)
        .where(Reservation.id == reservation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return db.session.execute(stmt).scalars().first()


def _commit():
    """커밋. 실패하면 롤백하고 실패 응답을 돌려준다 (성공이면 None)."""
    try:
        db.session.commit()
        return None
    except Exception:
        db.session.rollback()
        current_app.logger.exception("DB commit 실패")
        return fail("서버 오류가 발생했어요. 잠시 후 다시 시도해 주세요.", 500)


# =====================================================================
# 내부 헬퍼: 시간 규칙
# =====================================================================
def _minutes(value):
    return timedelta(minutes=value)


def _effective_end():
    """겹침 계산에 쓰는 종료 시각. 입차한 차는 종료 시각이 지나도 나가기 전까지는 자리를 쓰는 중으로 본다."""
    return case(
        (Reservation.status == RS.ENTERED, func.greatest(Reservation.end_at, now_local())),
        else_=Reservation.end_at,
    )


def _spot_conflict(spot_id, start, end, exclude_id=None):
    """같은 칸의 다른 진행 중 예약과 겹치는지 (예약 사이 BUFFER_MINUTES 간격 포함)"""
    stmt = select(Reservation.id).where(
        Reservation.spot_id == spot_id,
        Reservation.status.in_(RS.ACTIVE),
        Reservation.start_at < end + _minutes(BUFFER_MINUTES),
        _effective_end() > start - _minutes(BUFFER_MINUTES),
    )
    if exclude_id is not None:
        stmt = stmt.where(Reservation.id != exclude_id)
    return db.session.execute(stmt.limit(1)).first() is not None


def _plate_conflict(plate, start, end, exclude_id=None):
    """같은 차량번호의 다른 진행 중 예약과 시간이 겹치는지 (한 차가 동시에 두 곳에 있을 수 없음)"""
    stmt = select(Reservation.id).where(
        Reservation.plate_number == plate,
        Reservation.status.in_(RS.ACTIVE),
        Reservation.start_at < end,
        _effective_end() > start,
    )
    if exclude_id is not None:
        stmt = stmt.where(Reservation.id != exclude_id)
    return db.session.execute(stmt.limit(1)).first() is not None


def _in_entry_window(reservation, now):
    """입장 가능한 시간인지: 시작 ENTRY_EARLY_MINUTES분 전 ~ 종료"""
    return reservation.start_at - _minutes(ENTRY_EARLY_MINUTES) <= now < reservation.end_at


def _previous_car_inside(reservation):
    """같은 칸에 아직 출차하지 않은 다른 차(ENTERED)가 있는지"""
    stmt = (
        select(Reservation.id)
        .where(
            Reservation.spot_id == reservation.spot_id,
            Reservation.status == RS.ENTERED,
            Reservation.id != reservation.id,
        )
        .limit(1)
    )
    return db.session.execute(stmt).first() is not None


def _release_spot_if_free(spot, exclude_reservation_id):
    """그 칸에 입차한 차가 더 없으면 센서 상태를 EMPTY로 되돌린다."""
    if spot is None:
        return
    other = db.session.execute(
        select(Reservation.id)
        .where(
            Reservation.spot_id == spot.id,
            Reservation.status == RS.ENTERED,
            Reservation.id != exclude_reservation_id,
        )
        .limit(1)
    ).first()
    if other is None:
        spot.status = SS.EMPTY


def _display_status(spot, now):
    """앱에 보여 줄 칸 상태: 센서가 PARKED면 PARKED, 지금 예약이 걸려 있으면 RESERVED, 아니면 EMPTY"""
    if spot.status == SS.PARKED:
        return SS.PARKED
    rows = db.session.scalars(
        select(Reservation).where(Reservation.spot_id == spot.id, Reservation.status.in_(RS.ACTIVE))
    ).all()
    for r in rows:
        if r.status == RS.ENTERED or _in_entry_window(r, now):
            return SS.RESERVED
    return SS.EMPTY


def _extension_error(reservation, add_minutes, now):
    """연장이 안 되면 이유(문자열), 되면 None"""
    if now >= reservation.end_at:
        return "이미 종료 시각이 지나 연장할 수 없어요."
    new_end = reservation.end_at + _minutes(add_minutes)
    if (new_end - reservation.start_at) > _minutes(MAX_DURATION_MINUTES):
        return f"이용 시간은 최대 {MAX_DURATION_MINUTES}분까지 가능해요."
    if _spot_conflict(reservation.spot_id, reservation.start_at, new_end, exclude_id=reservation.id):
        return "다음 예약이 있어 연장할 수 없어요."
    if _plate_conflict(reservation.plate_number, reservation.start_at, new_end, exclude_id=reservation.id):
        return "같은 차량번호의 다른 예약과 시간이 겹쳐 연장할 수 없어요."
    return None


def _hhmm(value):
    return value.strftime("%H:%M")


def _next_start_after(spot_id, start):
    """같은 칸에서 start 이후에 시작하는 가장 이른 진행 중 예약의 시작 시각 (없으면 None)"""
    return db.session.scalar(
        select(func.min(Reservation.start_at)).where(
            Reservation.spot_id == spot_id,
            Reservation.status.in_(RS.ACTIVE),
            Reservation.start_at >= start,
        )
    )


def _available_until(spot_id, start):
    """start부터 이 칸을 쓸 수 있는 마지막 시각 (다음 예약 시작 - 간격). 뒤에 예약이 없으면 None(제한 없음)"""
    nxt = _next_start_after(spot_id, start)
    return None if nxt is None else nxt - _minutes(BUFFER_MINUTES)


def _gap_check(spot_id, start, end):
    """예약 가능 여부와 안내 문구를 함께 판단한다. 반환: (가능 여부, available_until, 안내 문구 또는 None)

    - start 시점에 칸이 이미 겹쳐 있으면 불가 (available_until은 None)
    - 다음 예약 때문에 쓸 수 있는 시간이 MIN_GAP_MINUTES보다 짧으면 불가
    - 요청한 end가 available_until을 넘으면 불가 (몇 시 이전까지만 가능한지 안내)
    """
    if _spot_conflict(spot_id, start, start + _minutes(1)):
        return False, None, f"해당 시간대에는 이미 예약이 있어요. (예약 사이에는 {BUFFER_MINUTES}분 간격이 필요해요)"
    until = _available_until(spot_id, start)
    if until is not None:
        usable = (until - start).total_seconds() / 60
        if usable < MIN_GAP_MINUTES:
            return False, until, (
                f"다음 예약 때문에 이 시간대는 {_hhmm(until)} 이전까지만 쓸 수 있어요. "
                f"이용 가능한 시간이 {MIN_GAP_MINUTES}분보다 짧아 예약할 수 없어요."
            )
        if end > until:
            return False, until, f"{_hhmm(until)} 이전까지만 이용할 수 있어요. 이용 시간을 줄이거나 다른 자리를 선택해 주세요."
    if _spot_conflict(spot_id, start, end):  # 위에서 안 걸러진 나머지 겹침
        return False, until, f"해당 시간대에는 이미 예약이 있어요. (예약 사이에는 {BUFFER_MINUTES}분 간격이 필요해요)"
    return True, until, None


def _exit_request_state(reservation, now):
    """[이용 종료] 요청 상태: NONE / PENDING(파이가 아직 안 열음) / OPENED(열림, 출차 대기) / EXPIRED(시간 지남)
    반환: (상태, 요청 시각, 만료 시각)"""
    requested = reservation.exit_requested_at
    if requested is None or reservation.status != RS.ENTERED:
        return "NONE", None, None
    expires = requested + _minutes(EXIT_REQUEST_MINUTES)
    if now >= expires:
        return "EXPIRED", requested, expires
    if reservation.exit_gate_opened_at is not None:
        return "OPENED", requested, expires
    return "PENDING", requested, expires


def _exit_request_dict(reservation, now):
    state, requested, expires = _exit_request_state(reservation, now)
    return {
        "exit_request": state,
        "exit_requested_at": fmt_dt(requested),
        "exit_expires_at": fmt_dt(expires),
    }


def compute_alert(reservation, now=None):
    """종료 알림 판단. 앱이 주기적으로 조회할 때(폴링)도, 나중에 푸시(FCM)를 붙일 때도 이 함수를 같이 쓴다.

    반환: {"minutes_left": 남은 분(올림, 진행 중이 아니면 None), "alert": "extend" | "leave" | None}
    - 입차한 예약(ENTERED)이 종료 ALERT_MINUTES분 이내가 되면 알림을 준다.
    - 뒤 예약 때문에 연장할 수 없으면 "leave"(출차해 주세요), 연장할 수 있으면 "extend"(연장하시겠습니까?)
    - 종료 시각이 지났는데 아직 안 나갔으면 "leave"
    """
    now = now or now_local()
    if reservation.status not in RS.ACTIVE:
        return {"minutes_left": None, "alert": None}

    minutes_left = minutes_until(reservation.end_at, now)
    alert = None
    if reservation.status == RS.ENTERED:
        if now >= reservation.end_at:
            alert = "leave"
        elif minutes_left <= ALERT_MINUTES:
            can_extend = _extension_error(reservation, SLOT_MINUTES, now) is None
            alert = "extend" if can_extend else "leave"
    return {"minutes_left": minutes_left, "alert": alert}


def _parse_duration(raw):
    """이용 시간(분) 검증. 반환: (분, 오류 메시지)"""
    duration = DEFAULT_DURATION_MINUTES if raw is None or raw == "" else to_int(raw)
    if duration is None or duration % SLOT_MINUTES != 0 or not SLOT_MINUTES <= duration <= MAX_DURATION_MINUTES:
        return None, f"이용 시간은 {SLOT_MINUTES}분 단위로 {SLOT_MINUTES}~{MAX_DURATION_MINUTES}분 사이여야 해요."
    return duration, None


def _parse_start(raw, now):
    """시작 시각 검증. 안 보냈으면 지금. 반환: (시각, 오류 메시지)"""
    if raw is None or str(raw).strip() == "":
        return now, None
    start = parse_datetime(raw)
    if start is None:
        return None, "start_at은 2026-10-05 14:00:00 형식으로 입력해 주세요."
    if start < now - _minutes(START_GRACE_MINUTES):
        return None, "시작 시각은 현재 이후여야 해요."
    if start > now + timedelta(days=MAX_ADVANCE_DAYS):
        return None, f"예약은 {MAX_ADVANCE_DAYS}일 이내로만 할 수 있어요."
    return max(start, now), None


# =====================================================================
# 앱용 API
# =====================================================================
@api.post("/signup")
def signup():
    body = get_body()
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    name = str(body.get("name") or "").strip()
    birth = parse_birth(body.get("birth"))
    plate = normalize_plate(body.get("plate"))

    if not is_valid_username(username):
        return fail("아이디는 영문/숫자/_ 4~20자로 입력해 주세요.")
    if len(password) < 4:
        return fail("비밀번호는 4자 이상이어야 해요.")
    if not name or len(name) > 50:
        return fail("이름을 입력해 주세요.")
    if birth is None or birth > date.today():
        return fail("생년월일은 2000-01-01 형식으로 입력해 주세요.")
    if not is_valid_plate(plate):
        return fail("차량번호는 12가3456 형식으로 입력해 주세요.")

    if db.session.scalar(select(Member.id).where(Member.username == username)):
        return fail("이미 사용 중인 아이디예요.")
    if db.session.scalar(select(Member.id).where(Member.plate_number == plate)):
        return fail("이미 등록된 차량번호예요.")

    member = Member(
        username=username,
        password_hash=hash_password(password),
        name=name,
        birth_date=birth,
        plate_number=plate,
    )
    db.session.add(member)
    try:
        db.session.commit()
    except IntegrityError:  # 동시에 같은 아이디/번호로 가입한 경우
        db.session.rollback()
        return fail("이미 사용 중인 아이디 또는 차량번호예요.")
    return ok(member_id=member.id)


@api.post("/login")
def login():
    body = get_body()
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")

    member = db.session.scalar(select(Member).where(Member.username == username))
    if member is None or not verify_password(member.password_hash, password):
        return fail("아이디 또는 비밀번호가 올바르지 않아요.")
    return ok(member_id=member.id, name=member.name)


@api.post("/reservations")
def create_reservation():
    body = get_body()
    now = now_local()
    member_id = to_int(body.get("member_id"))
    spot_id = to_int(body.get("spot_id"))

    if member_id is None:
        return fail("member_id가 필요해요.")
    if spot_id is None or not 1 <= spot_id <= SPOT_COUNT:
        return fail(f"spot_id는 1~{SPOT_COUNT} 사이여야 해요.")
    duration, error = _parse_duration(body.get("duration_minutes"))
    if error:
        return fail(error)
    start, error = _parse_start(body.get("start_at"), now)
    if error:
        return fail(error)
    end = start + _minutes(duration)

    member = db.session.get(Member, member_id)
    if member is None:
        return fail("존재하지 않는 회원이에요.")
    plate = normalize_plate(body.get("plate") or member.plate_number)  # 안 보내면 가입한 번호
    if not is_valid_plate(plate):
        return fail("차량번호는 12가3456 형식으로 입력해 주세요.")

    # 동시에 같은 시간대를 예약하는 요청을 막기 위해 잠금을 잡고, 겹치는지 다시 확인한다.
    _lock_all_spots()
    can_book, _until, message = _gap_check(spot_id, start, end)
    if not can_book:
        return fail(message)
    if _plate_conflict(plate, start, end):
        return fail("같은 차량번호로 시간이 겹치는 예약이 이미 있어요.")

    reservation = Reservation(
        member_id=member_id,
        spot_id=spot_id,
        plate_number=plate,
        start_at=start,
        end_at=end,
        status=RS.RESERVED,
    )
    db.session.add(reservation)
    error = _commit()
    if error:
        return error
    return ok(
        reservation_id=reservation.id,
        spot_id=spot_id,
        plate_number=plate,
        start_at=fmt_dt(start),
        end_at=fmt_dt(end),
    )


@api.post("/reservations/<int:reservation_id>/cancel")
def cancel_reservation(reservation_id):
    reservation = _lock_reservation(reservation_id)
    if reservation is None:
        return fail("존재하지 않는 예약이에요.")
    if reservation.status != RS.RESERVED:
        return fail("예약 상태일 때만 취소할 수 있어요.")

    reservation.status = RS.CANCELLED
    error = _commit()
    if error:
        return error
    return ok(reservation_id=reservation.id)


@api.post("/reservations/<int:reservation_id>/extend")
def extend_reservation(reservation_id):
    add_minutes = to_int(get_body().get("add_minutes"))
    if add_minutes is None or add_minutes <= 0 or add_minutes % SLOT_MINUTES != 0:
        return fail(f"add_minutes는 {SLOT_MINUTES}분 단위의 양수여야 해요.")

    reservation = _lock_reservation(reservation_id)
    if reservation is None:
        return fail("존재하지 않는 예약이에요.")
    if reservation.status not in RS.ACTIVE:
        return fail("진행 중인 예약만 연장할 수 있어요.")

    _lock_all_spots()  # 예약 등록과 동시에 들어와도 겹치지 않게 같은 잠금을 사용
    now = now_local()
    error = _extension_error(reservation, add_minutes, now)
    if error:
        return fail(error)

    reservation.end_at = reservation.end_at + _minutes(add_minutes)
    reservation.notified_10 = False  # 종료 시각이 바뀌었으니 알림 기록도 다시 시작
    reservation.notified_5 = False
    error = _commit()
    if error:
        return error
    return ok(
        reservation_id=reservation.id,
        end_at=fmt_dt(reservation.end_at),
        minutes_left=minutes_until(reservation.end_at, now),
    )


@api.post("/reservations/<int:reservation_id>/request-exit")
def request_exit(reservation_id):
    """앱 [이용 종료(차단봉 열기)] 버튼. 입차 중인 예약에만 열기 요청을 기록한다.

    - 상태는 바꾸지 않는다. EXITED 처리는 출구 카메라가 번호판을 확인한 뒤 기존 exit로만 한다.
    - 요청은 EXIT_REQUEST_MINUTES분 동안만 유효하다. 지나면 앱에서 다시 눌러야 한다.
    - 같은 요청이 연달아 와도 유효한 요청이 있으면 그대로 돌려준다. (시간을 새로 늘리지 않음)
    - member_id를 같이 보내면 본인 예약인지 확인한다. (로그인 토큰이 없는 동안의 최소한의 확인)
    """
    body = get_body()
    member_id = to_int(body.get("member_id"))

    reservation = _lock_reservation(reservation_id)
    if reservation is None:
        return fail("존재하지 않는 예약이에요.")
    if member_id is not None and member_id != reservation.member_id:
        return fail("본인의 예약이 아니에요.")
    if reservation.status != RS.ENTERED:
        return fail("입차 중인 예약만 차단봉 열기를 요청할 수 있어요.")

    now = now_local()
    state, _, _ = _exit_request_state(reservation, now)
    if state not in ("PENDING", "OPENED"):  # 요청이 없거나 만료됐으면 새로 요청
        reservation.exit_requested_at = now
        reservation.exit_gate_opened_at = None
        error = _commit()
        if error:
            return error

    info = _exit_request_dict(reservation, now)
    return ok(
        reservation_id=reservation.id,
        spot_id=reservation.spot_id,
        message=f"{EXIT_REQUEST_MINUTES}분 안에 출차해 주세요. 시간이 지나면 다시 눌러야 해요.",
        **info,
    )


@api.get("/reservations")
def list_reservations():
    member_id = to_int(request.args.get("member_id"))
    if member_id is None:
        return fail("member_id가 필요해요.")

    now = now_local()
    rows = db.session.scalars(
        select(Reservation)
        .where(Reservation.member_id == member_id)
        .order_by(Reservation.start_at.desc(), Reservation.id.desc())
    ).all()
    items = []
    for r in rows:
        item = r.to_dict()
        item.update(compute_alert(r, now))  # minutes_left, alert
        item.update(_exit_request_dict(r, now))  # exit_request, exit_requested_at, exit_expires_at
        items.append(item)
    return ok(reservations=items)


@api.get("/spots")
def list_spots():
    """주차면 현황. ?start_at=...&duration_minutes=... 를 붙이면 그 시간대에 예약 가능한지(available)도 알려 준다."""
    now = now_local()
    window = None
    if request.args.get("start_at") is not None or request.args.get("duration_minutes") is not None:
        duration, error = _parse_duration(request.args.get("duration_minutes"))
        if error:
            return fail(error)
        start, error = _parse_start(request.args.get("start_at"), now)
        if error:
            return fail(error)
        window = (start, start + _minutes(duration))

    spots = db.session.scalars(select(ParkingSpot).order_by(ParkingSpot.id)).all()
    items = []
    for spot in spots:
        item = {"spot_id": spot.id, "status": _display_status(spot, now)}
        if window is not None:
            can_book, until, message = _gap_check(spot.id, window[0], window[1])
            item["available"] = can_book
            # 이 시작 시각부터 쓸 수 있는 마지막 시각 (None이면 제한 없음 또는 해당 없음)
            item["available_until"] = fmt_dt(until)
            item["message"] = message  # 예약 불가 이유 / "몇 시 이전까지만" 안내 (가능하면 None)
        items.append(item)
    return ok(spots=items)


# =====================================================================
# 파이용 API
# =====================================================================
@api.get("/reservations/check")
def check_reservation():
    """차단봉을 열어도 되는지 판단한다. (판단은 서버, 파이는 allowed와 action만 보면 됨)

    - plate만 있음         -> 입구/출구 카메라
        · 입차 중(ENTERED)인 예약이 있으면 action="exit"
        · 지금 입장 가능한 예약(RESERVED)이 있고, 같은 칸의 앞차가 나갔으면 action="enter"
    - plate + spot_id 있음  -> 칸 카메라: 입차한 예약의 자리가 spot_id와 같을 때만 허용
    """
    now = now_local()
    plate = normalize_plate(request.args.get("plate"))
    if not is_valid_plate(plate):
        return ok(allowed=False, message="번호판 형식이 올바르지 않아요.")

    rows = db.session.scalars(
        select(Reservation)
        .where(Reservation.plate_number == plate, Reservation.status.in_(RS.ACTIVE))
        .order_by(Reservation.start_at)
    ).all()
    entered = next((r for r in rows if r.status == RS.ENTERED), None)

    spot_param = request.args.get("spot_id")
    if spot_param is not None:  # 칸 카메라
        spot_id = to_int(spot_param)
        if spot_id is None or not 1 <= spot_id <= SPOT_COUNT:
            return ok(allowed=False, message="spot_id가 올바르지 않아요.")
        if entered is None:
            return ok(allowed=False, message="입차 기록이 없어요. 입구를 먼저 통과해야 해요.")
        if entered.spot_id != spot_id:
            return ok(allowed=False, message=f"예약한 자리({entered.spot_id}번)가 아니에요.")
        return ok(allowed=True, reservation_id=entered.id, spot_id=entered.spot_id)

    if entered is not None:  # 주차장 안에 있는 차 -> 출차
        return ok(allowed=True, action="exit", reservation_id=entered.id, spot_id=entered.spot_id)

    reserved = [r for r in rows if r.status == RS.RESERVED]
    if not reserved:
        return ok(allowed=False, message="예약된 차량이 아니에요.")

    current = next((r for r in reserved if _in_entry_window(r, now)), None)
    if current is None:
        upcoming = next((r for r in reserved if r.start_at - _minutes(ENTRY_EARLY_MINUTES) > now), None)
        if upcoming is not None:
            return ok(
                allowed=False,
                message=(
                    f"아직 입장 시간이 아니에요. 예약 시작 {ENTRY_EARLY_MINUTES}분 전부터 입장할 수 있어요. "
                    f"(다음 예약: {fmt_dt(upcoming.start_at)})"
                ),
            )
        return ok(allowed=False, message="예약 시간이 지났어요.")

    if _previous_car_inside(current):
        return ok(allowed=False, message="이전 차량이 아직 출차하지 않았어요. 잠시 후 다시 시도해 주세요.")
    return ok(allowed=True, action="enter", reservation_id=current.id, spot_id=current.spot_id)


@api.post("/parking/enter")
def parking_enter():
    """입구 차단봉을 통과한 뒤 호출하는 기록용 API. RESERVED -> ENTERED (판단은 check에서 끝났다)"""
    reservation_id = to_int(get_body().get("reservation_id"))
    if reservation_id is None:
        return fail("reservation_id가 필요해요.")

    reservation = _lock_reservation(reservation_id)
    if reservation is None:
        return fail("존재하지 않는 예약이에요.")
    if reservation.status != RS.RESERVED:
        return fail("예약 상태가 아니라 입차 처리할 수 없어요.")

    reservation.status = RS.ENTERED
    reservation.entered_at = now_local()
    error = _commit()
    if error:
        return error
    return ok(reservation_id=reservation.id, spot_id=reservation.spot_id)


@api.post("/parking/exit")
def parking_exit():
    """출구 차단봉을 통과한 뒤 호출하는 기록용 API. ENTERED -> EXITED"""
    reservation_id = to_int(get_body().get("reservation_id"))
    if reservation_id is None:
        return fail("reservation_id가 필요해요.")

    reservation = _lock_reservation(reservation_id)
    if reservation is None:
        return fail("존재하지 않는 예약이에요.")
    if reservation.status != RS.ENTERED:
        return fail("입차 중인 예약이 아니라 출차 처리할 수 없어요.")

    spot = _lock_spot(reservation.spot_id)
    reservation.status = RS.EXITED
    reservation.exited_at = now_local()
    _release_spot_if_free(spot, reservation.id)
    error = _commit()
    if error:
        return error
    return ok(reservation_id=reservation.id, spot_id=reservation.spot_id)


@api.get("/gate/pending")
def gate_pending():
    """[파이용] 차단봉을 열어야 하는 요청 목록. 파이가 주기적으로 조회한다. ?spot_id=2 로 칸을 좁힐 수 있다.

    열어야 하는 요청 = 입차 중(ENTERED) + 앱에서 [이용 종료]를 누름 + 아직 안 열었음 + 유효 시간 안
    """
    now = now_local()
    stmt = select(Reservation).where(
        Reservation.status == RS.ENTERED,
        Reservation.exit_requested_at.is_not(None),
        Reservation.exit_requested_at > now - _minutes(EXIT_REQUEST_MINUTES),
        Reservation.exit_gate_opened_at.is_(None),
    )
    spot_param = request.args.get("spot_id")
    if spot_param is not None:
        spot_id = to_int(spot_param)
        if spot_id is None or not 1 <= spot_id <= SPOT_COUNT:
            return fail(f"spot_id는 1~{SPOT_COUNT} 사이여야 해요.")
        stmt = stmt.where(Reservation.spot_id == spot_id)
    rows = db.session.scalars(stmt.order_by(Reservation.exit_requested_at)).all()
    return ok(
        requests=[
            {
                "reservation_id": r.id,
                "spot_id": r.spot_id,
                "plate_number": r.plate_number,
                "requested_at": fmt_dt(r.exit_requested_at),
                "expires_at": fmt_dt(r.exit_requested_at + _minutes(EXIT_REQUEST_MINUTES)),
            }
            for r in rows
        ]
    )


@api.post("/gate/opened")
def gate_opened():
    """[파이용] 차단봉을 열었다고 알리는 API. 요청을 '열림'으로 바꿔 같은 요청이 다시 조회되지 않게 한다."""
    reservation_id = to_int(get_body().get("reservation_id"))
    if reservation_id is None:
        return fail("reservation_id가 필요해요.")

    reservation = _lock_reservation(reservation_id)
    if reservation is None:
        return fail("존재하지 않는 예약이에요.")

    now = now_local()
    state, _, _ = _exit_request_state(reservation, now)
    if state == "OPENED":  # 이미 알렸으면 그대로 성공 (재전송 대비)
        return ok(reservation_id=reservation.id, spot_id=reservation.spot_id)
    if state != "PENDING":
        return fail("유효한 차단봉 열기 요청이 없어요.")

    reservation.exit_gate_opened_at = now
    error = _commit()
    if error:
        return error
    return ok(reservation_id=reservation.id, spot_id=reservation.spot_id)


@api.post("/spots/<int:spot_id>/status")
def update_spot_status(spot_id):
    """칸 IR 센서 값 반영. status는 PARKED 또는 EMPTY.

    - PARKED: 그 칸에 입차한(ENTERED) 예약이 있을 때만 받는다.
    - EMPTY : 센서 상태만 바꾼다. 출차는 처리하지 않는다. (칸에서 빠진 것 != 주차장을 나간 것)
    """
    new_status = str(get_body().get("status") or "").upper()
    if new_status not in (SS.PARKED, SS.EMPTY):
        return fail("status는 PARKED 또는 EMPTY여야 해요.")

    spot = _lock_spot(spot_id)
    if spot is None:
        return fail("존재하지 않는 칸이에요.")

    if new_status == SS.PARKED:
        entered = db.session.scalar(
            select(Reservation.id)
            .where(Reservation.spot_id == spot_id, Reservation.status == RS.ENTERED)
            .limit(1)
        )
        if entered is None:
            return fail("이 칸에 입차한 예약이 없어요.")
    spot.status = new_status

    error = _commit()
    if error:
        return error
    return ok(spot_id=spot.id, status=_display_status(spot, now_local()))


@api.get("/health")
def health():
    """연결 확인용 (앱/파이에서 서버 주소가 맞는지 테스트)"""
    return ok(message="server is running")
