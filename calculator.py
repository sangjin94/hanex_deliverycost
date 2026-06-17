import math
import re
from models import VehicleRate, VehicleCapacity, ProductMaster, StoreMaster

# 시도 표준화 매핑
SIDO_NORMALIZE = {
    '경기': '경기도', '강원': '강원도', '충북': '충청북도', '충남': '충청남도',
    '전북': '전라북도', '전남': '전라남도', '경북': '경상북도', '경남': '경상남도',
    '서울': '서울특별시', '부산': '부산광역시', '대구': '대구광역시', '인천': '인천광역시',
    '광주': '광주광역시', '대전': '대전광역시', '울산': '울산광역시', '세종': '세종특별자치시',
    '제주': '제주특별자치도',
    # 풀네임도 포함
    '경기도': '경기도', '강원도': '강원도', '충청북도': '충청북도', '충청남도': '충청남도',
    '전라북도': '전라북도', '전라남도': '전라남도', '경상북도': '경상북도', '경상남도': '경상남도',
    '서울특별시': '서울특별시', '부산광역시': '부산광역시', '대구광역시': '대구광역시',
    '인천광역시': '인천광역시', '광주광역시': '광주광역시', '대전광역시': '대전광역시',
    '울산광역시': '울산광역시', '제주특별자치도': '제주특별자치도',
}


def normalize_sido(raw_sido):
    return SIDO_NORMALIZE.get(raw_sido, raw_sido)


def extract_sido_sigungu(address):
    """
    주소 문자열에서 시도 + 시군구 추출.
    예) "경기 용인시 기흥구 용구대로..." → ("경기도", "용인시")
        "강원도 강릉시 포남동..."        → ("강원도", "강릉시")
    """
    if not address:
        return None, None
    parts = address.strip().split()
    if len(parts) < 2:
        return None, None
    sido = normalize_sido(parts[0])
    sigungu = parts[1]
    # 특별시/광역시는 시군구가 '구'로 시작할 수 있음
    return sido, sigungu


def make_destination_key(sido, sigungu):
    """차량마스터 도착지 키 생성"""
    if sido and sigungu:
        return f"{sido} {sigungu}"
    return None


def find_destination(address, session):
    """주소 → 차량마스터 도착지 텍스트 매핑"""
    sido, sigungu = extract_sido_sigungu(address)
    if not sido or not sigungu:
        return None, None
    key = make_destination_key(sido, sigungu)
    # 정확 매핑
    rate = session.query(VehicleRate).filter(VehicleRate.destination == key).first()
    if rate:
        return key, (sido, sigungu)
    # 시군구만으로 부분 매핑 시도
    rate = session.query(VehicleRate).filter(
        VehicleRate.destination.like(f'%{sigungu}%')
    ).first()
    if rate:
        return rate.destination, (sido, sigungu)
    return key, (sido, sigungu)   # 매핑 안 돼도 주소 정보는 반환


def get_vehicle_capacities(session):
    """차량 적재량 테이블 (정렬: 소형→대형)"""
    caps = session.query(VehicleCapacity).order_by(VehicleCapacity.sort_order).all()
    return caps


def find_best_vehicle(plt_count, destination, session):
    """
    PLT 수 + 도착지에 맞는 최적 차량 선택.
    규칙: 최대적재량 >= plt_count 중 가장 저렴한 차량.
    초과 시 11톤 복수 투입.
    """
    caps = get_vehicle_capacities(session)
    if not caps:
        return None, None, None, None

    # 최대 단일 차량 적재량
    max_single = max(c.max_plt for c in caps)

    if plt_count > max_single:
        # 11톤 복수 투입 계산
        largest = max(caps, key=lambda c: c.max_plt)
        truck_count = math.ceil(plt_count / largest.max_plt)
        rate = session.query(VehicleRate).filter_by(
            destination=destination, vehicle_type=largest.vehicle_type
        ).first()
        if rate:
            total_cost = rate.unit_price * truck_count
            return largest.vehicle_type, total_cost, truck_count, rate.unit_price
        return None, None, None, None

    # 적재 가능한 차량들
    eligible_types = {c.vehicle_type for c in caps if c.max_plt >= plt_count}
    rates = session.query(VehicleRate).filter(
        VehicleRate.destination == destination,
        VehicleRate.vehicle_type.in_(eligible_types)
    ).all()

    if not rates:
        # 도착지 미매핑 → None
        return None, None, None, None

    best = min(rates, key=lambda r: r.unit_price)
    return best.vehicle_type, best.unit_price, 1, best.unit_price


def calculate_from_history(history_rows, customer_id, calc_name, session):
    """
    출고내역 → 배송비 단가 산정.
    배송처코드 + 납품일자 기준으로 그룹핑 후 PLT 합산.
    """
    # 그룹핑: {(store_code or store_name, shipping_date) → rows}
    groups = {}
    for row in history_rows:
        key = (row.store_code or row.store_name, row.shipping_date)
        if key not in groups:
            groups[key] = []
        groups[key].append(row)

    results = []
    errors = []

    for (store_key, ship_date), rows in groups.items():
        # 대표 정보 (첫 번째 행)
        rep = rows[0]
        address = rep.address
        store_name = rep.store_name
        store_code = rep.store_code

        # 총 박스, 총 PLT
        total_box = sum(r.box_qty or 0 for r in rows)
        total_plt_dec = sum(r.plt_qty_decimal or 0 for r in rows)
        total_plt_int = math.ceil(total_plt_dec) if total_plt_dec > 0 else None

        # PLT 환산이 없으면 상품마스터에서 계산
        if total_plt_dec == 0:
            calc_plt = 0
            for r in rows:
                if r.product_code and r.box_qty:
                    product = session.query(ProductMaster).filter_by(
                        customer_id=customer_id, product_code=r.product_code
                    ).first()
                    if product and product.plt_per_box and product.plt_per_box > 0:
                        calc_plt += r.box_qty / product.plt_per_box
            if calc_plt > 0:
                total_plt_dec = calc_plt
                total_plt_int = math.ceil(calc_plt)

        if not total_plt_int or total_plt_int == 0:
            errors.append(f"PLT 미산출: {store_name or store_code} ({ship_date})")
            continue

        # 도착지 매핑
        destination, sido_sigungu = find_destination(address, session)

        # 점포마스터에서 도착지 보완
        if not destination and store_code:
            store = session.query(StoreMaster).filter_by(
                customer_id=customer_id, store_code=store_code
            ).first()
            if store and store.destination:
                destination = store.destination
            elif store and store.address:
                destination, sido_sigungu = find_destination(store.address, session)

        if not destination:
            errors.append(f"도착지 미매핑: {store_name or store_code} | 주소: {address}")
            # 결과에는 포함하되 단가 없음으로 표시
            results.append({
                'store_code': store_code,
                'store_name': store_name,
                'address': address,
                'destination': None,
                'shipping_date': ship_date,
                'total_box_qty': total_box,
                'total_plt_decimal': total_plt_dec,
                'total_plt_count': total_plt_int,
                'delivery_mode': '직송',
                'vehicle_type': None,
                'delivery_cost': None,
                'cost_per_box': None,
                'memo': '도착지 미매핑',
            })
            continue

        vehicle_type, delivery_cost, truck_count, unit_price = find_best_vehicle(
            total_plt_int, destination, session
        )

        if delivery_cost is None:
            errors.append(f"단가 없음: {destination} (PLT={total_plt_int})")
            results.append({
                'store_code': store_code,
                'store_name': store_name,
                'address': address,
                'destination': destination,
                'shipping_date': ship_date,
                'total_box_qty': total_box,
                'total_plt_decimal': total_plt_dec,
                'total_plt_count': total_plt_int,
                'delivery_mode': '직송',
                'vehicle_type': None,
                'delivery_cost': None,
                'cost_per_box': None,
                'memo': f'차량단가 없음 ({destination})',
            })
            continue

        cost_per_box = round(delivery_cost / total_box, 1) if total_box > 0 else None
        memo = f'{truck_count}대 투입' if truck_count and truck_count > 1 else None

        results.append({
            'store_code': store_code,
            'store_name': store_name,
            'address': address,
            'destination': destination,
            'shipping_date': ship_date,
            'total_box_qty': total_box,
            'total_plt_decimal': total_plt_dec,
            'total_plt_count': total_plt_int,
            'delivery_mode': '직송',
            'vehicle_type': vehicle_type,
            'delivery_cost': delivery_cost,
            'cost_per_box': cost_per_box,
            'memo': memo,
        })

    return results, errors


def summarize_results(results):
    """집계 요약"""
    if not results:
        return {}

    valid = [r for r in results if r['delivery_cost'] is not None]
    if not valid:
        return {'total_deliveries': len(results), 'valid_count': 0}

    total_cost = sum(r['delivery_cost'] for r in valid)
    total_boxes = sum(r['total_box_qty'] for r in valid if r['total_box_qty'])

    by_dest = {}
    for r in valid:
        d = r['destination'] or '미매핑'
        if d not in by_dest:
            by_dest[d] = {'count': 0, 'total_cost': 0, 'total_boxes': 0}
        by_dest[d]['count'] += 1
        by_dest[d]['total_cost'] += r['delivery_cost']
        by_dest[d]['total_boxes'] += r['total_box_qty'] or 0

    dest_summary = []
    for dest, data in by_dest.items():
        avg_cpb = round(data['total_cost'] / data['total_boxes'], 1) if data['total_boxes'] > 0 else None
        dest_summary.append({
            'destination': dest,
            'count': data['count'],
            'total_cost': data['total_cost'],
            'total_boxes': data['total_boxes'],
            'avg_cost_per_box': avg_cpb,
        })

    return {
        'total_deliveries': len(results),
        'valid_count': len(valid),
        'error_count': len(results) - len(valid),
        'total_cost': total_cost,
        'total_boxes': total_boxes,
        'avg_cost_per_box': round(total_cost / total_boxes, 1) if total_boxes > 0 else None,
        'avg_cost_per_delivery': round(total_cost / len(valid)) if valid else None,
        'dest_summary': sorted(dest_summary, key=lambda x: x['total_cost'], reverse=True),
    }
