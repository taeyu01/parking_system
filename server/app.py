"""서버 시작점. 실행: python app.py

시작할 때 하는 일
 1) .env에서 DB 접속 정보 읽기
 2) 데이터베이스가 없으면 생성 (CREATE DATABASE IF NOT EXISTS)
 3) 테이블 자동 생성
 4) 주차칸이 비어 있으면 1~6번 칸을 EMPTY로 추가
 5) API 등록 후 0.0.0.0 으로 열기 (앱/파이가 접속할 수 있게)
"""
import os
import re
from urllib.parse import quote_plus

import pymysql
from dotenv import load_dotenv
from flask import Flask
from sqlalchemy import func, select

from models import SPOT_COUNT, ParkingSpot, SpotStatus, db
from routes import api
from utils import fail


def load_db_config():
    load_dotenv()
    config = {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER", "root"),
        "password": os.getenv("DB_PASSWORD", ""),
        "name": os.getenv("DB_NAME", "parking"),
    }
    if not re.fullmatch(r"[A-Za-z0-9_]+", config["name"]):
        raise ValueError("DB_NAME은 영문/숫자/_ 만 쓸 수 있어요.")
    return config


def ensure_database(config):
    """데이터베이스가 없으면 만든다. (테이블 자동 생성은 DB가 있어야 가능)"""
    conn = pymysql.connect(
        host=config["host"],
        port=config["port"],
        user=config["user"],
        password=config["password"],
        charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{config['name']}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
    finally:
        conn.close()


def seed_spots():
    """주차칸이 하나도 없으면 1~SPOT_COUNT번 칸을 EMPTY로 추가"""
    count = db.session.scalar(select(func.count()).select_from(ParkingSpot))
    if count == 0:
        db.session.add_all(
            [ParkingSpot(id=i, status=SpotStatus.EMPTY) for i in range(1, SPOT_COUNT + 1)]
        )
        db.session.commit()


def create_app():
    config = load_db_config()
    ensure_database(config)

    app = Flask(__name__)
    app.json.ensure_ascii = False  # 응답의 한글이 \uXXXX로 깨져 보이지 않게
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        f"mysql+pymysql://{quote_plus(config['user'])}:{quote_plus(config['password'])}"
        f"@{config['host']}:{config['port']}/{config['name']}?charset=utf8mb4"
    )
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,  # 오래 켜 둬서 끊긴 연결을 자동 복구
        "pool_recycle": 280,
        # 매 쿼리가 "그 순간 확정된 최신 데이터"를 보게 한다. (동시 예약의 겹침 확인이 옛 데이터를 보지 않게)
        "isolation_level": "READ COMMITTED",
    }

    db.init_app(app)
    with app.app_context():
        db.create_all()
        seed_spots()

    app.register_blueprint(api)

    @app.errorhandler(404)
    def not_found(_):
        return fail("존재하지 않는 주소예요.", 404)

    @app.errorhandler(405)
    def method_not_allowed(_):
        return fail("허용되지 않는 요청 방식이에요.", 405)

    @app.errorhandler(500)
    def server_error(_):
        db.session.rollback()
        return fail("서버 오류가 발생했어요.", 500)

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("SERVER_PORT", "5000"))
    # host="0.0.0.0": 같은 네트워크의 앱/파이가 접속할 수 있다. (기본값은 자기 자신만 접속 가능)
    app.run(host="0.0.0.0", port=port, debug=False)
