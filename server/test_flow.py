"""서버 전체 흐름 테스트 (서버를 켜 둘 필요 없음).

사용법:  python test_flow.py           # 테스트 기록을 DB에 남긴다 (Workbench에서 직접 확인 가능)
         python test_flow.py --clean   # 끝나면 테스트 기록을 지운다

- .env의 DB 설정으로 접속해서 서버 코드를 직접 호출한다. (HTTP 서버를 띄우지 않는다)
- 시간이 흐르는 상황(노쇼, 종료 임박, 앞차 초과)은 진행 중인 테스트 예약의 시각을 과거로 당겨서 흉내 낸다.
  그래서 DB에 남은 일부 예약은 시각이 과거로 당겨져 있다. (정상)
- 건드리는 데이터: 아이디가 'tester_'로 시작하는 회원/예약, 그리고 4·5·6번 칸의 센서 상태.
  (1~3번 칸과 실제 회원 데이터는 건드리지 않는다.)
- 시나리오가 끝날 때마다 진행 중인 테스트 예약은 삭제하지 않고 상태만 마무리한다. (RESERVED -> CANCELLED, ENTERED -> EXITED)
  그래서 DB에는 기록이 그대로 남고, 4·5·6번 칸은 비워진다.
- 다음에 다시 실행하면 이전 테스트 기록은 지우고 새로 만든다.
- 4·5·6번 칸에 이미 진행 중인 실제 예약이 있으면 겹쳐서 실패할 수 있다.
"""
import random
import sys
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select

from app import app
from models import Member, ParkingSpot, Reservation, db
from utils import (
    BUFFER_MINUTES,
    ENTRY_EARLY_MINUTES,
    MAX_ADVANCE_DAYS,
    MAX_DURATION_MINUTES,
    NOSHOW_MINUTES,
    SLOT_MINUTES,
)

client = app.test_client()
passed = 0
CLEAN = "--clean" in sys.argv        # 끝나면 테스트 기록을 지울지
scenario_ids = []                    # (시나리오 이름, [예약 번호들]) -- DB에서 찾아보기 쉽게 기록
_last_id = 0
TEST_SPOTS = (4, 5, 6)
DUR = max(SLOT_MINUTES, (120 // SLOT_MINUTES) * SLOT_MINUTES)   # 2시간 안팎
SHORT = max(SLOT_MINUTES, (60 // SLOT_MINUTES) * SLOT_MINUTES)  # 1시간 안팎


# =====================================================================
# 도구
# =====================================================================
def call(method, path, body=None, params=None, c=None):
    res = (c or client).open(path, method=method, json=body, query_string=params)
    return res.get_json()


def expect(label, condition, detail=None):
    global passed
    if not condition:
        print(f"  [FAIL] {label}")
        if detail is not None:
            print(f"         응답: {detail}")
        sys.exit(1)
    passed += 1
    print(f"  [ OK ] {label}")


def now():
    return datetime.now().replace(microsecond=0)


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


def new_plate(letter):
    return f"{random.randint(10, 99)}{letter}{random.randint(1000, 9999)}"


def spot_status(spot_id):
    spots = call("GET", "/spots")["spots"]
    return next(s["status"] for s in spots if s["spot_id"] == spot_id)


def res_row(member_id, reservation_id):
    rows = call("GET", "/reservations", params={"member_id": member_id})["reservations"]
    return next(r for r in rows if r["reservation_id"] == reservation_id)


def signup(username, plate, name="테스터"):
    r = call("POST", "/signup", {"username": username, "password": "pw1234", "name": name, "birth": "2000-01-01", "plate": plate})
    expect(f"회원가입 {username}", r.get("success") is True and "member_id" in r, r)
    return r["member_id"]


def _test_member_ids():
    return list(db.session.scalars(select(Member.id).where(Member.username.startswith("tester_", autoescape=True))))


def reset_scenario():
    """진행 중인 테스트 예약을 삭제하지 않고 마무리한다. (RESERVED -> CANCELLED, ENTERED -> EXITED)
    그리고 4·5·6번 칸 센서를 비운다. 그래서 다음 시나리오가 깨끗하게 시작하면서 기록은 DB에 남는다."""
    with app.app_context():
        ids = _test_member_ids()
        if ids:
            closed_at = now()
            for r in db.session.scalars(select(Reservation).where(Reservation.member_id.in_(ids), Reservation.status.in_(("RESERVED", "ENTERED")))):
                if r.status == "ENTERED":
                    r.status = "EXITED"
                    r.exited_at = r.exited_at or closed_at
                else:
                    r.status = "CANCELLED"
        for spot in db.session.scalars(select(ParkingSpot).where(ParkingSpot.id.in_(TEST_SPOTS))):
            spot.status = "EMPTY"
        db.session.commit()


def purge_test_data():
    """테스트 회원과 예약을 완전히 삭제하고 4·5·6번 칸을 비운다."""
    with app.app_context():
        ids = _test_member_ids()
        if ids:
            db.session.execute(delete(Reservation).where(Reservation.member_id.in_(ids)))
            db.session.execute(delete(Member).where(Member.id.in_(ids)))
        for spot in db.session.scalars(select(ParkingSpot).where(ParkingSpot.id.in_(TEST_SPOTS))):
            spot.status = "EMPTY"
        db.session.commit()


def time_travel(minutes):
    """시간이 흐른 것처럼 진행 중인 테스트 예약들의 시각을 과거로 당긴다. (이미 끝난 예약은 그대로 둔다)"""
    delta = timedelta(minutes=minutes)
    with app.app_context():
        rows = db.session.scalars(
            select(Reservation).where(Reservation.member_id.in_(_test_member_ids()), Reservation.status.in_(("RESERVED", "ENTERED")))
        )
        for r in rows:
            r.start_at -= delta
            r.end_at -= delta
            if r.entered_at:
                r.entered_at -= delta
        db.session.commit()


def mark(name):
    """방금 끝난 시나리오가 만든 예약 번호를 기록한다."""
    global _last_id
    with app.app_context():
        ids = list(db.session.scalars(select(Reservation.id).where(Reservation.member_id.in_(_test_member_ids()), Reservation.id > _last_id).order_by(Reservation.id)))
    if ids:
        _last_id = ids[-1]
    scenario_ids.append((name, ids))


# =====================================================================
purge_test_data()
with app.app_context():
    _last_id = db.session.scalar(select(func.max(Reservation.id))) or 0
stamp = int(time.time())
plate_a, plate_b, plate_c = new_plate("가"), new_plate("나"), new_plate("다")

print("\n[1] 서버 연결 / 회원가입 / 로그인")
r = call("GET", "/health")
expect("서버 연결 (/health)", r.get("success") is True, r)
ma = signup(f"tester_a_{stamp}", plate_a, "테스터A")
mb = signup(f"tester_b_{stamp}", plate_b, "테스터B")
mc = signup(f"tester_c_{stamp}", plate_c, "테스터C")
r = call("POST", "/signup", {"username": f"tester_a_{stamp}", "password": "pw1234", "name": "중복", "birth": "2000-01-01", "plate": new_plate("라")})
expect("중복 아이디 거부", r.get("success") is False, r)
r = call("POST", "/signup", {"username": f"tester_x_{stamp}", "password": "pw1234", "name": "형식", "birth": "2000-01-01", "plate": "ABC123"})
expect("잘못된 번호판 형식 거부", r.get("success") is False, r)
r = call("POST", "/signup", {"username": f"tester_y_{stamp}", "password": "pw1234", "name": "날짜", "birth": "2000-13-45", "plate": new_plate("라")})
expect("존재하지 않는 생년월일 거부", r.get("success") is False, r)
r = call("POST", "/login", {"username": f"tester_a_{stamp}", "password": "pw1234"})
expect("로그인 성공 (member_id 반환)", r.get("success") is True and r.get("member_id") == ma, r)
r = call("POST", "/login", {"username": f"tester_a_{stamp}", "password": "wrong"})
expect("틀린 비밀번호 거부", r.get("success") is False, r)

# ---------------------------------------------------------------------
print("\n[2] 시간제 예약 등록 / 겹침 / 버퍼 / 같은 번호판")
reset_scenario()
r = call("POST", "/reservations", {"member_id": ma, "spot_id": 5, "duration_minutes": DUR})
expect("A가 5번 칸을 지금부터 예약 (start_at 생략)", r.get("success") is True and "reservation_id" in r, r)
res_a, a_start, a_end = r["reservation_id"], parse(r["start_at"]), parse(r["end_at"])
expect("종료 시각 = 시작 시각 + 이용 시간", a_end - a_start == timedelta(minutes=DUR), r)
expect("예약 번호판은 가입 번호판", r["plate_number"] == plate_a, r)

r = call("POST", "/reservations", {"member_id": ma, "spot_id": 4, "duration_minutes": SLOT_MINUTES + 1})
expect("이용 시간이 단위에 안 맞으면 거부", r.get("success") is False, r)
r = call("POST", "/reservations", {"member_id": ma, "spot_id": 4, "duration_minutes": MAX_DURATION_MINUTES + SLOT_MINUTES})
expect("최대 이용 시간 초과 거부", r.get("success") is False, r)
r = call("POST", "/reservations", {"member_id": ma, "spot_id": 4, "start_at": fmt(now() - timedelta(hours=2))})
expect("과거 시작 시각 거부", r.get("success") is False, r)
r = call("POST", "/reservations", {"member_id": ma, "spot_id": 4, "start_at": fmt(now() + timedelta(days=MAX_ADVANCE_DAYS + 1))})
expect(f"{MAX_ADVANCE_DAYS}일 넘게 먼 예약 거부", r.get("success") is False, r)
r = call("POST", "/reservations", {"member_id": ma, "spot_id": 4, "start_at": "abc"})
expect("start_at 형식 오류 거부", r.get("success") is False, r)
r = call("POST", "/reservations", {"member_id": ma, "spot_id": 9})
expect("없는 칸(9번) 예약 거부", r.get("success") is False, r)

r = call("POST", "/reservations", {"member_id": mb, "spot_id": 5, "duration_minutes": SHORT})
expect("B가 같은 칸 같은 시간 예약하면 거부", r.get("success") is False, r)
b_start = a_end + timedelta(minutes=BUFFER_MINUTES)
r = call("POST", "/reservations", {"member_id": mb, "spot_id": 5, "start_at": fmt(b_start - timedelta(minutes=1)), "duration_minutes": SHORT})
expect(f"앞 예약 종료 후 {BUFFER_MINUTES}분 간격이 안 되면 거부 (1분 부족)", r.get("success") is False, r)
r = call("POST", "/reservations", {"member_id": mb, "spot_id": 5, "start_at": fmt(b_start), "duration_minutes": SHORT})
expect(f"앞 예약 종료 + {BUFFER_MINUTES}분 뒤는 예약 가능", r.get("success") is True, r)

r = call("POST", "/reservations", {"member_id": ma, "spot_id": 6, "duration_minutes": SHORT})
expect("같은 번호판으로 시간이 겹치면 거부 (다른 칸이어도)", r.get("success") is False, r)
other_plate = new_plate("라")
r = call("POST", "/reservations", {"member_id": ma, "spot_id": 6, "duration_minutes": SHORT, "plate": other_plate})
expect("다른 차량번호를 보내면 같은 시간에 여러 칸 예약 가능", r.get("success") is True and r["plate_number"] == other_plate, r)
tomorrow = now() + timedelta(days=1)
r = call("POST", "/reservations", {"member_id": ma, "spot_id": 6, "start_at": fmt(tomorrow), "duration_minutes": SHORT})
expect("같은 번호판이라도 시간이 안 겹치면 예약 가능 (내일)", r.get("success") is True, r)

spots = call("GET", "/spots")["spots"]
expect("주차면 현황 6칸", len(spots) == 6, spots)
expect("지금 예약이 걸린 5번 칸은 RESERVED", spot_status(5) == "RESERVED")
expect("예약 없는 4번 칸은 EMPTY", spot_status(4) == "EMPTY")
spots = call("GET", "/spots", params={"start_at": fmt(a_start), "duration_minutes": SHORT})["spots"]
expect("그 시간대에 5번 칸은 예약 불가(available=false)", next(s for s in spots if s["spot_id"] == 5)["available"] is False, spots)
expect("그 시간대에 4번 칸은 예약 가능(available=true)", next(s for s in spots if s["spot_id"] == 4)["available"] is True, spots)
far = now() + timedelta(days=3)
spots = call("GET", "/spots", params={"start_at": fmt(far), "duration_minutes": SHORT})["spots"]
expect("3일 뒤 시간대에는 5번 칸도 예약 가능", next(s for s in spots if s["spot_id"] == 5)["available"] is True, spots)

rows = call("GET", "/reservations", params={"member_id": ma})["reservations"]
needed = {"reservation_id", "spot_id", "plate_number", "start_at", "end_at", "status", "entered_at", "exited_at", "minutes_left", "alert"}
expect("내 예약 조회: 필요한 키가 모두 있음", len(rows) == 3 and needed <= set(rows[0]), rows[:1])
expect("내 예약 조회: 시작 시각이 늦은 순으로 정렬", rows[0]["start_at"] >= rows[1]["start_at"] >= rows[2]["start_at"], rows)
mark("[2] 시간제 예약 등록 / 겹침 / 버퍼 / 같은 번호판")

# ---------------------------------------------------------------------
print("\n[3] 입구 확인 / 입차 / 칸 확인 / IR / 앞차 미출차 / 출차")
reset_scenario()
r = call("POST", "/reservations", {"member_id": ma, "spot_id": 5, "duration_minutes": DUR})
res_a, a_end = r["reservation_id"], parse(r["end_at"])
b_start = a_end + timedelta(minutes=BUFFER_MINUTES)
r = call("POST", "/reservations", {"member_id": mb, "spot_id": 5, "start_at": fmt(b_start), "duration_minutes": SHORT})
res_b = r["reservation_id"]

r = call("GET", "/reservations/check", params={"plate": plate_a})
expect("입구: 예약 시간인 A는 허용, action=enter", r.get("allowed") is True and r.get("action") == "enter" and r.get("reservation_id") == res_a and r.get("spot_id") == 5, r)
r = call("GET", "/reservations/check", params={"plate": plate_b})
expect("입구: 예약 시간 전인 B는 불허", r.get("allowed") is False and "입장" in r.get("message", ""), r)
r = call("GET", "/reservations/check", params={"plate": new_plate("마")})
expect("입구: 예약 없는 차량은 불허", r.get("allowed") is False, r)

r = call("POST", "/parking/enter", {"reservation_id": res_a})
expect("입차 기록 (RESERVED -> ENTERED)", r.get("success") is True, r)
r = call("POST", "/parking/enter", {"reservation_id": res_a})
expect("이미 입차한 예약의 재입차 거부", r.get("success") is False, r)
row = res_row(ma, res_a)
expect("예약 상태 ENTERED + 입차 시각 기록", row["status"] == "ENTERED" and row["entered_at"] is not None, row)
r = call("GET", "/reservations/check", params={"plate": plate_a})
expect("주차장 안의 차는 action=exit", r.get("allowed") is True and r.get("action") == "exit" and r.get("reservation_id") == res_a, r)

r = call("GET", "/reservations/check", params={"plate": plate_a, "spot_id": 5})
expect("칸 카메라: 예약한 5번 칸은 허용", r.get("allowed") is True, r)
r = call("GET", "/reservations/check", params={"plate": plate_a, "spot_id": 6})
expect("칸 카메라: 다른 칸(6번)은 불허", r.get("allowed") is False, r)

r = call("POST", "/spots/5/status", {"status": "PARKED"})
expect("IR: 5번 칸 PARKED", r.get("success") is True and spot_status(5) == "PARKED", r)
r = call("POST", "/spots/5/status", {"status": "EMPTY"})
expect("IR: 자리에서 빠짐 -> 센서 EMPTY, 앱에는 예약 중(RESERVED)으로 보임", r.get("success") is True and spot_status(5) == "RESERVED", r)
expect("자리에서 빠져도 예약은 ENTERED 유지 (출차 아님)", res_row(ma, res_a)["status"] == "ENTERED")
r = call("POST", "/spots/5/status", {"status": "PARKED"})
expect("IR: 다시 주차 -> PARKED", r.get("success") is True and spot_status(5) == "PARKED", r)
r = call("POST", "/spots/4/status", {"status": "PARKED"})
expect("입차한 예약이 없는 칸(4번) PARKED 거부", r.get("success") is False, r)
r = call("POST", "/spots/5/status", {"status": "BROKEN"})
expect("잘못된 status 값 거부", r.get("success") is False, r)

# A가 종료 시각을 넘겨서 아직 안 나갔고, 뒤 예약 B의 입장 시간이 된 상황
time_travel(DUR + BUFFER_MINUTES - ENTRY_EARLY_MINUTES + 2)
r = call("GET", "/reservations/check", params={"plate": plate_b})
expect("앞차(A)가 안 나갔으면 B의 입장 불허", r.get("allowed") is False and "출차" in r.get("message", ""), r)
row = res_row(ma, res_a)
expect("종료가 지났는데 안 나간 A는 alert=leave, 남은 시간 0", row["alert"] == "leave" and row["minutes_left"] == 0, row)

r = call("GET", "/reservations/check", params={"plate": plate_a})
expect("A는 출차(action=exit) 대상", r.get("allowed") is True and r.get("action") == "exit", r)
r = call("POST", "/parking/exit", {"reservation_id": res_b})
expect("입차하지 않은 예약(B)의 출차 거부", r.get("success") is False, r)
r = call("POST", "/parking/exit", {"reservation_id": res_a})
expect("출차 기록 (ENTERED -> EXITED)", r.get("success") is True, r)
row = res_row(ma, res_a)
expect("예약 상태 EXITED + 출차 시각 기록", row["status"] == "EXITED" and row["exited_at"] is not None, row)
expect("출차하면 칸이 PARKED에서 풀림", spot_status(5) != "PARKED")
r = call("POST", "/parking/exit", {"reservation_id": res_a})
expect("이미 출차한 예약의 재출차 거부", r.get("success") is False, r)

r = call("GET", "/reservations/check", params={"plate": plate_b})
expect("앞차가 나가면 B는 입장 허용", r.get("allowed") is True and r.get("action") == "enter", r)
r = call("POST", "/parking/enter", {"reservation_id": res_b})
expect("B 입차 기록", r.get("success") is True, r)
r = call("POST", "/parking/exit", {"reservation_id": res_b})
expect("B 출차 기록", r.get("success") is True, r)
expect("모두 나가면 5번 칸은 EMPTY", spot_status(5) == "EMPTY")
mark("[3] 입차 / 칸 확인 / IR / 앞차 미출차 / 출차")

# ---------------------------------------------------------------------
print("\n[4] 종료 알림(minutes_left / alert) / 연장")
reset_scenario()
r = call("POST", "/reservations", {"member_id": mc, "spot_id": 4, "duration_minutes": SHORT})
res_c, c_end = r["reservation_id"], parse(r["end_at"])
call("POST", "/parking/enter", {"reservation_id": res_c})
r = call("POST", "/reservations", {"member_id": mb, "spot_id": 4, "start_at": fmt(c_end + timedelta(minutes=BUFFER_MINUTES)), "duration_minutes": SHORT})
res_n = r["reservation_id"]
row = res_row(mc, res_c)
expect("종료까지 한참 남으면 alert 없음", row["alert"] is None and row["minutes_left"] >= SHORT - 1, row)

time_travel(SHORT - 8)  # 종료 8분 전
row = res_row(mc, res_c)
expect("종료 8분 전 + 뒤에 예약이 있으면 alert=leave (출차해 주세요)", row["minutes_left"] in (7, 8) and row["alert"] == "leave", row)
r = call("POST", f"/reservations/{res_c}/extend", {"add_minutes": SLOT_MINUTES})
expect("뒤 예약 때문에 연장 불가", r.get("success") is False and "예약" in r.get("message", ""), r)
r = call("POST", f"/reservations/{res_n}/cancel")
expect("뒤 예약이 취소됨", r.get("success") is True, r)
row = res_row(mc, res_c)
expect("뒤 예약이 없어지면 alert=extend (연장하시겠습니까?)", row["alert"] == "extend", row)
r = call("POST", f"/reservations/{res_c}/extend", {"add_minutes": SLOT_MINUTES + 1})
expect("연장 단위가 안 맞으면 거부", r.get("success") is False, r)
r = call("POST", f"/reservations/{res_c}/extend", {"add_minutes": MAX_DURATION_MINUTES})
expect("최대 이용 시간을 넘는 연장 거부", r.get("success") is False, r)
r = call("POST", f"/reservations/{res_c}/extend", {"add_minutes": SLOT_MINUTES})
expect("연장 성공", r.get("success") is True and "end_at" in r, r)
row = res_row(mc, res_c)
expect("연장하면 남은 시간이 늘고 alert 해제", row["minutes_left"] > 8 and row["alert"] is None, row)

time_travel(SHORT + SLOT_MINUTES + 5)  # 종료 시각이 한참 지남
row = res_row(mc, res_c)
expect("종료 후에도 안 나가면 alert=leave", row["alert"] == "leave" and row["minutes_left"] == 0, row)
r = call("POST", f"/reservations/{res_c}/extend", {"add_minutes": SLOT_MINUTES})
expect("종료 시각이 지난 뒤에는 연장 불가", r.get("success") is False, r)
mark("[4] 종료 알림 / 연장")

# ---------------------------------------------------------------------
print("\n[5] 노쇼 자동 취소")
reset_scenario()
r = call("POST", "/reservations", {"member_id": ma, "spot_id": 6, "duration_minutes": DUR})
res_a = r["reservation_id"]
time_travel(NOSHOW_MINUTES - 5)
expect(f"시작 후 {NOSHOW_MINUTES - 5}분까지는 예약 유지", res_row(ma, res_a)["status"] == "RESERVED")
time_travel(10)
expect(f"시작 후 {NOSHOW_MINUTES + 5}분이 지나면 자동 취소(CANCELLED)", res_row(ma, res_a)["status"] == "CANCELLED")
r = call("POST", "/reservations", {"member_id": mb, "spot_id": 6, "duration_minutes": SHORT})
expect("노쇼로 풀린 6번 칸을 다른 사람이 예약 가능", r.get("success") is True, r)
mark("[5] 노쇼 자동 취소")

# ---------------------------------------------------------------------
print("\n[6] 입장 허용 시간 / 취소 규칙")
reset_scenario()
r = call("POST", "/reservations", {"member_id": ma, "spot_id": 5, "start_at": fmt(now() + timedelta(hours=2)), "duration_minutes": SHORT})
expect("2시간 뒤 예약", r.get("success") is True, r)
res_a = r["reservation_id"]
r = call("GET", "/reservations/check", params={"plate": plate_a})
expect("입장 시간 전에는 불허 + 다음 예약 안내", r.get("allowed") is False and "입장" in r.get("message", ""), r)
time_travel(120 - ENTRY_EARLY_MINUTES + 2)
r = call("GET", "/reservations/check", params={"plate": plate_a})
expect(f"시작 {ENTRY_EARLY_MINUTES}분 전 범위에 들어오면 허용", r.get("allowed") is True and r.get("action") == "enter", r)
r = call("POST", f"/reservations/{res_a}/cancel")
expect("예약 취소 (RESERVED -> CANCELLED)", r.get("success") is True, r)
r = call("POST", f"/reservations/{res_a}/cancel")
expect("이미 취소된 예약 재취소 거부", r.get("success") is False, r)
r = call("GET", "/reservations/check", params={"plate": plate_a})
expect("취소된 예약의 차량은 불허", r.get("allowed") is False, r)
r = call("POST", "/reservations", {"member_id": ma, "spot_id": 5, "duration_minutes": SHORT})
res_a2 = r["reservation_id"]
call("POST", "/parking/enter", {"reservation_id": res_a2})
r = call("POST", f"/reservations/{res_a2}/cancel")
expect("입차한(ENTERED) 예약은 취소 거부", r.get("success") is False, r)
mark("[6] 입장 허용 시간 / 취소 규칙")

# ---------------------------------------------------------------------
print("\n[7] 동시 요청 (같은 시간에 여러 명이 누를 때)")
reset_scenario()
racers = [signup(f"tester_r{i}_{stamp}", new_plate("라"), f"레이서{i}") for i in range(8)]
results = []
gate = threading.Barrier(len(racers))


def race(member_id):
    c = app.test_client()
    gate.wait()
    results.append(call("POST", "/reservations", {"member_id": member_id, "spot_id": 6, "duration_minutes": SHORT}, c=c))


threads = [threading.Thread(target=race, args=(m,)) for m in racers]
[t.start() for t in threads]
[t.join() for t in threads]
success = [x for x in results if x and x.get("success")]
expect(f"8명이 같은 칸·같은 시간에 동시 예약 -> 성공 1건 (결과: {len(success)}건)", len(success) == 1, results)

reset_scenario()
results = []
gate = threading.Barrier(2)


def race_plate(spot_id):
    c = app.test_client()
    gate.wait()
    results.append(call("POST", "/reservations", {"member_id": ma, "spot_id": spot_id, "duration_minutes": SHORT}, c=c))


threads = [threading.Thread(target=race_plate, args=(s,)) for s in (4, 5)]
[t.start() for t in threads]
[t.join() for t in threads]
success = [x for x in results if x and x.get("success")]
expect(f"같은 번호판이 두 칸에 동시 예약 -> 성공 1건 (결과: {len(success)}건)", len(success) == 1, results)

mark("[7] 동시 요청")

reset_scenario()  # 남은 진행 중 예약을 마무리하고 4·5·6번 칸을 비운다
print(f"\n전체 통과: {passed}개 항목")
if CLEAN:
    purge_test_data()
    print("테스트 기록을 삭제했어요. (--clean)")
else:
    first = min(ids[0] for _, ids in scenario_ids if ids)
    print("\n[DB에 남은 테스트 기록] Workbench에서 직접 확인해 보세요.")
    for name, ids in scenario_ids:
        if ids:
            print(f"  {name}: 예약 번호 {ids[0]}~{ids[-1]} ({len(ids)}건)")
    print("\n  SELECT * FROM members WHERE username LIKE 'tester%';")
    print(f"  SELECT * FROM reservations WHERE id >= {first} ORDER BY id;")
    print("  SELECT * FROM parking_spots;")
    print("\n  * 일부 예약은 시간이 흐른 상황을 흉내 내려고 시각이 과거로 당겨져 있어요. (정상)")
    print("  * 다음 실행 때 이 테스트 기록은 지우고 새로 만들어요. 바로 지우려면: python test_flow.py --clean")
