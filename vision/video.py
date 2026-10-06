import cv2          # 이미지 처리: 색상 변환, 확대, 저장
import re           # 문자열 정리 및 번호판 형식 검사
import easyocr      # 번호판 이미지에서 문자 인식
import requests     # Flask 서버에 HTTP 요청 전송
import time         # 시간 측정 및 대기
from ultralytics import YOLO                # 번호판 위치 검출
from collections import Counter, deque      # 득표수 계산, 최근 OCR 결과 보관
from picamera2 import Picamera2, Preview    # Pi 카메라 제어 및 미리보기
import subprocess
from pathlib import Path

model = YOLO("models/best_plate_yolo26.pt")
reader = easyocr.Reader(["ko", "en"], gpu=False)

SERVER = "http://192.168.10.12:5000"

SERVO = (Path(__file__).resolve().parent.parent/"hardware"/"servo"/"sg90")

def control_gate(command):
    subprocess.run(["sudo", "-n", str(SERVO), command], check=True, timeout=5,)

def check_gate_reservation(plate):
    response = requests.get(
        f"{SERVER}/reservations/check",
        params={"plate": plate},  # 입구 확인이므로 spot_id는 보내지 않음
        timeout=5,
    )
    response.raise_for_status()
    data = response.json()

    if not isinstance(data, dict):
        raise ValueError("서버 응답 형식이 잘못됐습니다")

    if data.get("success") is not True:
        raise ValueError(data.get("message") or "예약 조회 실패")

    allowed = data.get("allowed")
    if not isinstance(allowed, bool):
        raise ValueError("서버 응답에 승인 여부가 없거나 형식이 잘못됐습니다")

    if allowed:
        reservation_id = data.get("reservation_id")
        spot_id = data.get("spot_id")
        action = data.get("action")

        # bool은 Python에서 int의 하위 타입이므로 type으로 검사
        if type(reservation_id) is not int or reservation_id <= 0:
            raise ValueError("예약 번호가 잘못됐습니다")
        if type(spot_id) is not int or not 1 <= spot_id <= 6:
            raise ValueError("주차칸 번호가 잘못됐습니다")
        if action not in ("enter", "exit"):
            raise ValueError("게이트 동작 정보가 잘못됐습니다")

    return data

def request_parking_enter(reservation_id):
    response = requests.post(
        f"{SERVER}/parking/enter",
        json={"reservation_id": reservation_id},
        timeout=5,
    )
    response.raise_for_status()
    data = response.json()

    if not isinstance(data, dict):
        raise ValueError("서버 응답 형식이 잘못됐습니다")

    if data.get("success") is not True:
        raise ValueError(data.get("message") or "입차 처리 실패")

    return data

def request_parking_exit(reservation_id):
    response = requests.post(
        f"{SERVER}/parking/exit",
        json={"reservation_id": reservation_id},
        timeout=5,
    )
    response.raise_for_status()
    data = response.json()

    if not isinstance(data, dict):
        raise ValueError("서버 응답 형식이 잘못됐습니다")

    if data.get("success") is not True:
        raise ValueError(data.get("message") or "출차 처리 실패")

    return data

def recognize_plate(plate):
    gray = cv2.cvtColor(plate, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)) # 부분별 대비 개선 설정
    gray = clahe.apply(gray)    # 대비 개선 적용
    gray = cv2.resize(gray, None, fx=3, fy=3, 
                      interpolation=cv2.INTER_CUBIC) # 보간법

    start = time.perf_counter() # OCR 시작 시간 기록
    ocr_result = reader.recognize(gray, detail=1)   # 문자와 신뢰도 등을 반환
    print("OCR:", (time.perf_counter() - start) * 1000, "ms")   # OCR 소요 시간 출력

    if len(ocr_result) == 0:
        return None, 0

    text = ocr_result[0][1].replace(" ", "")
    text = re.sub(r"[^0-9가-힣]", "", text)
    ocr_conf = ocr_result[0][2]

    print("OCR TEXT:", text)
    print("OCR CONF:", ocr_conf)
    return text, ocr_conf

# OCR로 읽은 문자열이 번호판 형식에 맞는지 검사
def is_valid_plate(text):
    if text is None:
        return False
    return re.fullmatch(r"[0-9]{2,3}[가-힣][0-9]{4}", text) is not None

# 카메라를 제어할 객체 생성, 이후 picam2를 통해 촬영, 설정, 종료 수행
picam2 = Picamera2()

# 카메라 설정 
camera_config = picam2.create_preview_configuration(main={"size": (1920, 1080), "format": "XRGB8888"})

picam2.configure(camera_config) # 만든 설정을 카메라에 적용
# picam2.start_preview(Preview.QTGL) # 카메라 영상을 화면에 보여주는 미리보기 창 준비
picam2.start()
time.sleep(1)

# 최근 유효한 OCR 결과를 최대 5개 저장, 6번째가 들어오면 가장 오래된 결과가 자동 삭제됨
recent_plates = deque(maxlen=5)  

final_plate = None  

gate_reservation_id = None
gate_spot_id = None
gate_action = None
gate_state = "WAITING"

recognition_start = None
rejected_time = None
last_detected_time = None
absence_timeout = 3.0

try:
    while True:
        if gate_state == "REJECTED":
            if time.monotonic() - rejected_time >= 5:
                print("재인식 대기 상태로 복귀")
                gate_state = "WAITING"
                recent_plates.clear()
                final_plate = None
                recognition_start = None
                rejected_time = None
            else:
                time.sleep(0.1)
            continue

        if gate_state == "OPEN":
            input(
                f"차단봉 열림 [{gate_action}]. "
                "차량이 완전히 통과한 뒤 Enter를 누르세요 [수동 테스트]: "
            )

            try:
                if gate_action == "enter":
                    result = request_parking_enter(gate_reservation_id)
                    print("입차 처리 성공")

                elif gate_action == "exit":
                    result = request_parking_exit(gate_reservation_id)
                    print("출차 처리 성공")

                else:
                    raise ValueError("알 수 없는 게이트 동작입니다")

                print("예약 번호:", result["reservation_id"])
                print("주차칸:", result["spot_id"])

            except (requests.exceptions.RequestException, ValueError) as error:
                print("주차 상태 변경 API 실패:", error)
                print("서버의 예약 상태를 확인해야 합니다")

            try:
                control_gate("close")

            except (
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                OSError,
            ) as error:
                print("차단봉 닫기 명령 실패:", error)
                print("장치 확인 필요 -> 테스트 종료")
                break

            print("차단봉 닫기 명령 전송 완료")

            # 이번에는 한 차량만 테스트
            break
            

        frame = picam2.capture_array() # 카메라에서 영상 한 프레임을 NumPy 배열로 가져옴
        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR) # 4채널 -> BGR 3채널로 변환

        start = time.perf_counter()
        results = model(frame, conf=0.5, imgsz=640, classes=[1], verbose=False)
        print("YOLO:", (time.perf_counter() - start) * 1000, "ms")

        result = results[0] # 첫 번째 이미지의 검출 결과 꺼내기

        if len(result.boxes) == 0:
            if last_detected_time is not None:
                if time.monotonic() - last_detected_time >= absence_timeout:
                    recent_plates.clear()
                    recognition_start = None
                    last_detected_time = None
                    print("번호판 미검출 3초 → Voting 초기화")
            continue

        last_detected_time = time.monotonic()

        best_box = max(result.boxes, key=lambda box: float(box.conf[0]))
        x1, y1, x2, y2 = map(int, best_box.xyxy[0])

        h, w = frame.shape[:2]
        x1, x2 = max(0, x1), min(w, x2)
        y1, y2 = max(0, y1), min(h, y2)

        if x2 <= x1 or y2 <= y1:
            continue

        print("CONF:", float(best_box.conf[0]))
        print("CLASS:", int(best_box.cls[0]))
        print("BOX:", x1, y1, x2, y2)

        plate = frame[y1:y2, x1:x2].copy()
        text, ocr_conf = recognize_plate(plate)
      
        if is_valid_plate(text):
            if recognition_start is None:
                recognition_start = time.perf_counter()

            recent_plates.append(text)
            votes = Counter(recent_plates)
            candidate, count = votes.most_common(1)[0]

            print("최근 OCR:", list(recent_plates))
            print(candidate, "→", count, "표")

            if count >= 3:
                final_plate = candidate
                recognition_time = time.perf_counter() - recognition_start

                print("최종 번호판:", final_plate)
                print(f"번호판 확정 시간: {recognition_time:.2f}초")

                recent_plates.clear()
                recognition_start = None

                try:
                    gate = check_gate_reservation(final_plate)

                except (requests.exceptions.RequestException, ValueError) as error:
                    print("API 조회 실패:", error)
                    print("승인 확인 불가 → 차단기 유지")
                    gate_reservation_id = None
                    gate_spot_id = None
                    gate_action = None
                    gate_state = "REJECTED"
                    rejected_time = time.monotonic()

                else:
                    if gate["allowed"]:
                        gate_reservation_id = gate["reservation_id"]
                        gate_spot_id = gate["spot_id"]
                        gate_action = gate["action"]

                        print("게이트 승인:", final_plate)
                        print("동작:", gate_action)
                        print("예약 번호:", gate_reservation_id)
                        print("주차칸:", gate_spot_id)
                        try:
                            control_gate("open")
                        except (subprocess.CalledProcessError,
                                subprocess.TimeoutExpired,
                                OSError,
                        ) as error:
                            print("차단봉 열기 명령 실패:", error)
                            print("장치 확인 필요 -> 테스트 종료")
                            break
                        else:
                            print("차단봉 열기 명령 전송 완료")
                            gate_state = "OPEN"
                    else:
                        print("게이트 불허:", gate.get("message", "예약 확인 불가"))

                        gate_reservation_id = None
                        gate_spot_id = None
                        gate_action = None

                        gate_state = "REJECTED"
                        rejected_time = time.monotonic()
                                    
except KeyboardInterrupt:
    print("\n프로그램 종료")

finally:
    picam2.stop()
    picam2.close()
    print("정상 종료")