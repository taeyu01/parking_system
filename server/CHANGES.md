# 변경 사항: 일찍 나가기([이용 종료]) + 빈 시간 안내

## 앱용 API
### POST /reservations/{id}/request-exit
- body(선택): `{"member_id": 3}` — 보내면 본인 예약인지 확인
- ENTERED(입차 중)인 예약만 가능. 상태는 바꾸지 않고 요청만 기록 (EXITED는 출구 카메라 확인 후 기존 `exit`가 처리)
- 성공: `exit_request`("PENDING"), `exit_requested_at`, `exit_expires_at`, `message`
- 5분(`EXIT_REQUEST_MINUTES`) 안에 출차해야 하고, 지나면 다시 눌러야 함. 연달아 눌러도 요청 시각은 그대로.

### GET /reservations?member_id= (추가된 필드)
- `exit_request`: NONE / PENDING(파이가 아직 안 열음) / OPENED(열림, 출차 대기) / EXPIRED(5분 지남)
- `exit_requested_at`, `exit_expires_at`
- 앱 문구 예: PENDING "차단봉을 여는 중이에요", OPENED "차단봉이 열렸어요. 5분 안에 출차해 주세요", EXPIRED "시간이 지났어요. 다시 눌러 주세요"

### GET /spots?start_at=&duration_minutes= (추가된 필드)
- `available_until`: 그 시작 시각부터 쓸 수 있는 마지막 시각 (뒤 예약 시작 - 15분). 뒤 예약이 없으면 null
- `message`: 예약 불가 이유 / "16:00 이전까지만 이용할 수 있어요" 같은 안내 (가능하면 null)
- 뒤 예약까지 빈 시간이 60분(`MIN_GAP_MINUTES`)보다 짧으면 `available: false`. 예약 등록(POST /reservations)도 같은 규칙과 문구로 거부.

## 파이용 API
### GET /gate/pending?spot_id=2
- 열어야 할 요청 목록: `{"requests": [{"reservation_id", "spot_id", "plate_number", "requested_at", "expires_at"}]}`
- 파이가 1~2초마다 조회 → 있으면 해당 칸 차단봉 열기

### POST /gate/opened
- body: `{"reservation_id": 31}` — 차단봉을 열었다고 알림. 이후 같은 요청은 조회되지 않음 (재전송해도 성공)

## 기타
- `reservations`에 컬럼 2개 추가 (`exit_requested_at`, `exit_gate_opened_at`). 서버를 켜면 기존 DB에 자동으로 추가됨 (ALTER TABLE, 이미 있으면 건너뜀)
- 출차(EXITED) 처리는 그대로. EXITED 예약은 겹침 계산에서 빠지므로, 일찍 나가면 남은 시간이 자동으로 빈 시간이 됨
- 파이는 차단봉을 열고 일정 시간 뒤 닫는 동작을 파이 쪽에서 직접 처리해야 함
- test_flow.py에 [8] 시나리오 추가 (전체 120개 항목 통과)
