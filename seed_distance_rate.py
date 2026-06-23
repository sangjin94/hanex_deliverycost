"""
거점 변동용차 거리별 단가 (VehicleDistanceRate) 시드 스크립트
실행: python seed_distance_rate.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from app import app, db
from models import VehicleDistanceRate
from hub_rate_data import DISTANCE_RATES, VEHICLE_TYPES


def seed():
    with app.app_context():
        existing = db.session.query(VehicleDistanceRate).count()
        if existing >= 5000:
            print(f"이미 {existing}건 존재 - 스킵")
            return

        if existing > 0:
            db.session.query(VehicleDistanceRate).delete()
            db.session.commit()
            print(f"기존 {existing}건 삭제")

        rows = []
        for entry in DISTANCE_RATES:
            km = entry[0]
            for i, vtype in enumerate(VEHICLE_TYPES):
                rows.append(VehicleDistanceRate(
                    vehicle_type=vtype,
                    km=km,
                    unit_price=entry[i + 1]
                ))

        db.session.bulk_save_objects(rows)
        db.session.commit()
        print(f"OK {len(rows)}건 삽입 완료 ({len(DISTANCE_RATES)} km x {len(VEHICLE_TYPES)} 차종)")


if __name__ == '__main__':
    seed()
