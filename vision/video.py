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
import lgpio             # GPIO21 수동 부저와 RGB LED 제어
import threading         # 추론 중에도 LED 점멸을 유지

model = YOLO("models/best_plate_yolo26.pt")
reader = easyocr.Reader(["ko", "en"], gpu=False)

SERVER = "http://192.168.10.12:5000" 
# SERVER = "http://192.168.0.97:5000"

SERVO = (Path(__file__).resolve().parent.parent/"hardware"/"servo"/"sg90")
IR = (Path(__file__).resolve().parent.parent/"hardware"/"ir"/"ir")

# 부저의 S 핀이 연결된 BCM GPIO 번호 (물리 핀 40번)
BUZZER_GPIO = 21
buzzer_handle = None

# RGB LED: 공통 캐소드(공통 GND), 각 색상 핀에 별도 전류 제한 저항
LED_RED = 26    # 물리 37번
LED_GREEN = 20  # 물리 38번
LED_BLUE = 16   # 물리 36번
LED_PINS = (LED_RED, LED_GREEN, LED_BLUE)
LED_COMMON_ANODE = False
led_handle = None
led_claimed = []
led_mode = "WAITING"
led_lock = threading.Lock()
led_stop = threading.Event()
led_thread = None


def setup_led():
    """LED GPIO를 점유하고 점멸용 백그라운드 스레드를 시작한다."""
    global led_handle, led_thread
    try:
        led_handle = lgpio.gpiochip_open(0)
        initial = 1 if LED_COMMON_ANODE else 0
        for pin in LED_PINS:
            lgpio.gpio_claim_output(led_handle, pin, initial)
            led_claimed.append(pin)
        led_stop.clear()
        led_thread = threading.Thread(target=led_loop, name="rgb-led", daemon=True)
        led_thread.start()
        print("RGB LED 준비 완료 (R26 G20 B16)")
    except (OSError, lgpio.error) as error:
        print("RGB LED 초기화 실패 (LED 없이 진행):", error)
        cleanup_led()


def set_led_mode(mode):
    """WAITING, RECOGNIZING, OPEN, REJECTED, CLEARING 중 하나."""
    global led_mode
    with led_lock:
        led_mode = mode


def led_output(r, g, b):
    if led_handle is None:
        return
    for pin, enabled in zip(LED_PINS, (r, g, b)):
        level = int(not enabled) if LED_COMMON_ANODE else int(bool(enabled))
        lgpio.gpio_write(led_handle, pin, level)


def led_loop():
    """YOLO/OCR이나 IR 센서가 블로킹돼도 점멸 유지."""
    blink_on = True
    previous_mode = None
    while not led_stop.is_set():
        with led_lock:
            mode = led_mode
        if mode != previous_mode:
            blink_on = True
            previous_mode = mode
        if mode == "WAITING":
            color, interval = (1, 0, 0), 0.1
        elif mode == "RECOGNIZING":
            color, interval = ((1, 1, 0) if blink_on else (0, 0, 0)), 0.35
        elif mode == "OPEN":
            color, interval = (0, 1, 0), 0.1
        elif mode == "REJECTED":
            color, interval = ((1, 0, 0) if blink_on else (0, 0, 0)), 0.15
        elif mode == "CLEARING":
            color, interval = (0, 0, 1), 0.1
        else:
            color, interval = (0, 0, 0), 0.1
        try:
            led_output(*color)
        except (OSError, lgpio.error) as error:
            print("RGB LED 제어 실패:", error)
            break
        if led_stop.wait(interval):
            break
        if mode in ("RECOGNIZING", "REJECTED"):
            blink_on = not blink_on
    try:
        led_output(0, 0, 0)
    except (OSError, lgpio.error):
        pass


def cleanup_led():
    """점멸 스레드를 멈춘 다음 GPIO 자원을 반납."""
    global led_handle, led_thread
    led_stop.set()
    if led_thread is not None:
        led_thread.join(timeout=2)
        led_thread = None
    if led_handle is not None:
        try:
            for pin in led_claimed:
                off = 1 if LED_COMMON_ANODE else 0
                lgpio.gpio_write(led_handle, pin, off)
        except (OSError, lgpio.error) as error:
            print("RGB LED 소등 실패:", error)
        finally:
            lgpio.gpiochip_close(led_handle)
            led_handle = None
            led_claimed.clear()


def setup_buzzer():
    """부저 준비. 부저 오류가 나더라도 차단기 기능은 계속 사용한다."""
    global buzzer_handle
    try:
        buzzer_handle = lgpio.gpiochip_open(0)  # Raspberry Pi 4: /dev/gpiochip0
        lgpio.gpio_claim_output(buzzer_handle, BUZZER_GPIO, 0)
    except (OSError, lgpio.error) as error:
        print("부저 초기화 실패 (부저 없이 진행):", error)
        if buzzer_handle is not None:
            lgpio.gpiochip_close(buzzer_handle)
            buzzer_handle = None


def play_sound(notes):
    """(주파수 Hz, 지속 시간 초) 목록을 순서대로 재생. 0Hz는 무음."""
    if buzzer_handle is None:
        return
    try:
        for freq, duration in notes:
            lgpio.tx_pwm(buzzer_handle, BUZZER_GPIO, freq, 50 if freq else 0)
            time.sleep(duration)
    except (OSError, lgpio.error) as error:
        print("부저 재생 실패:", error)
    finally:
        try:
            lgpio.tx_pwm(buzzer_handle, BUZZER_GPIO, 0, 0)
        except (OSError, lgpio.error) as error:
            print("부저 PWM 종료 실패:", error)


def entrance_sound():
    """게이트 승인: 삐빅 (올라가는 두 음)."""
    play_sound([(900, 0.12), (0, 0.06), (1400, 0.20)])


def passed_sound():
    """차량 통과 완료: 띠리링."""
    play_sound([(800, 0.08), (1000, 0.08), (1300, 0.08), (1700, 0.25)])


def warning_sound():
    """예약 거절: 삐! 삐! 삐!"""
    play_sound([(1200, 0.15), (0, 0.12),
                (1200, 0.15), (0, 0.12), (1200, 0.15)])


def cleanup_buzzer():
    global buzzer_handle
    if buzzer_handle is not None:
        # play_sound() 종료 시 이미 PWM 정지 요청을 보냈다.
        # 중복 tx_pwm(0, 0) 호출은 일부 lgpio 환경에서 bad PWM micros를 낼 수 있어 생략.
        try:
            lgpio.gpiochip_close(buzzer_handle)
        finally:
            buzzer_handle = None

def control_gate(command):
    subprocess.run(["sudo", "-n", str(SERVO), command], check=True, timeout=5,)

def wait_vehicle_pass():
    subprocess.run(["sudo", "-n", str(IR)], check=True, timeout=30,)

# 공용 차단봉: 모든 주차 공간의 출차 요청을 조회한다.
def get_pending_exit_requests():
    response = requests.get(f"{SERVER}/gate/pending", timeout=5)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("success") is not True:
        raise ValueError("출차 요청 조회 응답 오류")
    pending = data.get("requests")
    if not isinstance(pending, list):
        raise ValueError("출차 요청 목록 형식 오류")
    return pending


def notify_gate_opened(reservation_id):
    response = requests.post(
        f"{SERVER}/gate/opened",
        json={"reservation_id": reservation_id},
        timeout=5,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("success") is not True:
        raise ValueError("차단봉 개방 확인 API 오류")
    return data


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
   
    # 대비 개선 적용
    gray = cv2.cvtColor(plate, cv2.COLOR_BGR2GRAY)

    gray = cv2.resize(
        gray, None,
        fx=2, fy=2,
        interpolation=cv2.INTER_CUBIC
    )

    cv2.imwrite("debug_plate_original.jpg", plate)
    cv2.imwrite("debug_plate_processed.jpg", gray)
    start = time.perf_counter() # OCR 시작 시간 기록
    ocr_result = reader.recognize(gray, detail=1)   # 문자와 신뢰도 등을 반환
    print("OCR:", (time.perf_counter() - start) * 1000, "ms")   # OCR 소요 시간 출력

    if len(ocr_result) == 0:
        return None, 0

    text = ocr_result[0][1].replace(" ", "")
    text = re.sub(r"[^0-9가-힣]", "", text)

    # OCR이 '호'를 '3'으로 잘못 읽는 현상 보정
    if re.fullmatch(r"[0-9]{3}3[0-9]{4}", text):
        text = text[:3] + "호" + text[4:]

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
PENDING_CHECK_INTERVAL = 2.0
last_pending_check = 0.0
# 앱 출차 요청으로 열었는지 구분한다. 앱 출차는 카메라 확인 없이 EXITED 처리하지 않는다.
gate_source = None

setup_buzzer()
setup_led()

try:
    while True:
        # 게이트가 대기 중일 때만 서버의 앱 출차 요청을 확인한다.
        # YOLO/OCR 처리 시간이 있어 정확히 2초마다 실행되지는 않을 수 있다.
        if gate_state == "WAITING" and time.monotonic() - last_pending_check >= PENDING_CHECK_INTERVAL:
            last_pending_check = time.monotonic()
            try:
                pending_requests = get_pending_exit_requests()
            except (requests.exceptions.RequestException, ValueError) as error:
                print("출차 요청 조회 실패:", error)
            else:
                if pending_requests:
                    req = pending_requests[0]  # 공용 차단봉은 한 번에 한 대만 처리
                    reservation_id = req.get("reservation_id")
                    if type(reservation_id) is not int or reservation_id <= 0:
                        print("잘못된 출차 요청:", req)
                    else:
                        print("앱 출차 요청:", req)
                        # 물리적 안전을 위해 실제 차량 위치를 확인하고 시연할 것.
                        entrance_sound()  # 게이트를 열기 전에 승인 알림음
                        try:
                            control_gate("open")
                        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
                            print("차단봉 열기 실패:", error)
                            break
                        try:
                            notify_gate_opened(reservation_id)
                        except (requests.exceptions.RequestException, ValueError) as error:
                            print("개방 확인 전송 실패:", error)
                            print("서버 상태 확인 필요. 안전을 위해 게이트를 닫고 종료합니다")
                            try:
                                control_gate("close")
                            except Exception as close_error:
                                print("차단봉 닫기 실패:", close_error)
                            break
                        gate_reservation_id = reservation_id
                        gate_spot_id = req.get("spot_id")
                        gate_action = "exit"
                        gate_source = "app"
                        gate_state = "OPEN"
                        set_led_mode("OPEN")
                        continue

        if gate_state == "REJECTED":
            if time.monotonic() - rejected_time >= 5:
                print("재인식 대기 상태로 복귀")
                gate_state = "WAITING"
                set_led_mode("WAITING")
                recent_plates.clear()
                final_plate = None
                recognition_start = None
                rejected_time = None
            else:
                time.sleep(0.1)
            continue

        if gate_state == "OPEN":
            print(f"차단봉 열림 [{gate_action}]")
            print("차량 통과 대기 중...")

            try:
                wait_vehicle_pass()
                print("차량 통과 완료")

            except (
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                OSError,
            ) as error:
                print("IR 차량 통과 감지 실패:", error)
                print("안전을 위해 주차 상태를 변경하지 않습니다")

                try:
                    control_gate("close")
                except Exception as close_error:
                    print("차단봉 닫기 실패:", close_error)

                break

            try:
                if gate_action == "enter":
                    result = request_parking_enter(gate_reservation_id)
                    print("입차 처리 성공")

                elif gate_action == "exit" and gate_source == "app":
                    # 서버 요구사항: EXITED는 출구 카메라 확인 후 기존 exit API가 처리.
                    # IR 센서만으로 차량 번호를 검증할 수 없으므로 여기서는 EXITED 변경 안 함.
                    result = None
                    print("앱 출차 차량 통과 감지. EXITED는 출구 카메라 확인 후 처리 필요")

                elif gate_action == "exit":
                    result = request_parking_exit(gate_reservation_id)
                    print("출차 처리 성공")

                else:
                    raise ValueError("알 수 없는 게이트 동작입니다")

                if result is not None:
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
            passed_sound()  # IR로 통과 확인 후 차단봉을 닫은 다음 완료음

            # 다음 차량을 위해 상태 초기화. 같은 번호판 재인식 방지용 CLEARING 진입.
            gate_reservation_id = None
            gate_spot_id = None
            gate_action = None
            gate_source = None
            final_plate = None
            recent_plates.clear()
            recognition_start = None
            last_detected_time = None
            clearing_since = None
            gate_state = "CLEARING"
            set_led_mode("CLEARING")
            print("다음 차량 준비: 번호판이 3초간 보이지 않을 때까지 대기")
            continue
            

        frame = picam2.capture_array() # 카메라에서 영상 한 프레임을 NumPy 배열로 가져옴
        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR) # 4채널 -> BGR 3채널로 변환

        start = time.perf_counter()
        results = model(frame, conf=0.5, imgsz=640, classes=[1], verbose=False)
        print("YOLO:", (time.perf_counter() - start) * 1000, "ms")

        result = results[0] # 첫 번째 이미지의 검출 결과 꺼내기

        # 차량이 지나간 뒤에는 이전 번호판이 시야에서 사라진 것을 확인한다.
        # YOLO 탐지가 3초 동안 없을 때만 다음 차량을 받는다.
        if gate_state == "CLEARING":
            if len(result.boxes) == 0:
                if clearing_since is None:
                    clearing_since = time.monotonic()
                elif time.monotonic() - clearing_since >= 3.0:
                    gate_state = "WAITING"
                    set_led_mode("WAITING")
                    last_pending_check = time.monotonic()
                    print("다음 차량 대기 상태로 복귀")
            else:
                clearing_since = None
            continue

        if len(result.boxes) == 0:
            if last_detected_time is not None:
                if time.monotonic() - last_detected_time >= absence_timeout:
                    recent_plates.clear()
                    recognition_start = None
                    last_detected_time = None
                    print("번호판 미검출 3초 → Voting 초기화")
                    if gate_state == "WAITING":
                        set_led_mode("WAITING")
            continue

        last_detected_time = time.monotonic()
        if gate_state == "WAITING":
            set_led_mode("RECOGNIZING")

        best_box = max(result.boxes, key=lambda box: float(box.conf[0]))
        x1, y1, x2, y2 = map(int, best_box.xyxy[0])

        h, w = frame.shape[:2]
        # 번호판이 잘리지 않도록 여백 추가
        pad_x = 30
        pad_y = 10

        x1 = max(0, x1 - pad_x)
        x2 = min(w, x2 + pad_x)
        y1 = max(0, y1 - pad_y)
        y2 = min(h, y2 + pad_y)

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
                    set_led_mode("REJECTED")
                    rejected_time = time.monotonic()

                else:
                    if gate["allowed"]:
                        gate_reservation_id = gate["reservation_id"]
                        gate_spot_id = gate["spot_id"]
                        gate_action = gate["action"]
                        gate_source = "camera"

                        print("게이트 승인:", final_plate)
                        print("동작:", gate_action)
                        print("예약 번호:", gate_reservation_id)
                        print("주차칸:", gate_spot_id)
                        entrance_sound()  # 게이트를 열기 전에 승인 알림음
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
                            set_led_mode("OPEN")
                    else:
                        print("게이트 불허:", gate.get("message", "예약 확인 불가"))
                        warning_sound()  # 번호판/예약 승인 거절 경고음

                        gate_reservation_id = None
                        gate_spot_id = None
                        gate_action = None

                        gate_state = "REJECTED"
                        set_led_mode("REJECTED")
                        rejected_time = time.monotonic()
                                    
except KeyboardInterrupt:
    print("\n프로그램 종료")

finally:
    try:
        cleanup_led()
    except (OSError, lgpio.error) as error:
        print("RGB LED 정리 실패:", error)
    try:
        cleanup_buzzer()  # 종료할 때 GPIO21 PWM과 GPIO 칩 해제
    except (OSError, lgpio.error) as error:
        print("부저 정리 실패:", error)
    picam2.stop()
    picam2.close()
    print("정상 종료")