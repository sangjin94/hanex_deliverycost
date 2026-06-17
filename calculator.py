import math
from models import VehicleRate, VehicleCapacity, ProductMaster, StoreMaster, JointDeliveryRate, SystemConfig

SIDO_NORMALIZE = {
    '경기': '경기도', '강원': '강원도', '충북': '충청북도', '충남': '충청남도',
    '전북': '전라북도', '전남': '전라남도', '경북': '경상북도', '경남': '경상남도',
    '서울': '서울특별시', '부산': '부산광역시', '대구': '대구광역시', '인천': '인천광역시',
    '광주': '광주광역시', '대전': '대전광역시', '울산': '울산광역시', '세종': '세종특별자치시',
    '제주': '제주특별자치도',
    '경기도': '경기도', '강원도': '강원도', '충청북도': '충청북도', '충청남도': '충청남도',
    '전라북도': '전라북도', '전라남도': '전라남도', '경상북도': '경상북도', '경상남도': '경상남도',
    '서울특별시': '서울특별시', '부산광역시': '부산광역시', '대구광역시': '대구광역시',
    '인천광역시': '인천광역시', '광주광역시': '광주광역시', '대전광역시': '대전광역시',
    '울산광역시': '울산광역시', '제주특별자치도': '제주특별자치도',
}


def normalize_sido(raw_sido):
    return SIDO_NORMALIZE.get(raw_sido, raw_sido)


def extract_sido_sigungu(address):
    if not address:
        return None, None
    parts = address.strip().split()
    if len(parts) < 2:
        return None, None
    return normalize_sido(parts[0]), parts[1]


def make_destination_key(sido, sigungu):
    if sido and sigungu:
        return f"{sido} {sigungu}"
    return None


def find_destination(address, session):
    sido, sigungu = extract_sido_sigungu(address)
    if not sido or not sigungu:
        return None, (None, None)
    key = make_destination_key(sido, sigungu)
    rate = session.query(VehicleRate).filter(VehicleRate.destination == key).first()
    if rate:
        return key, (sido, sigungu)
    rate = session.query(VehicleRate).filter(
        VehicleRate.destination.like(f'%{sigungu}%')
    ).first()
    if rate:
        return rate.destination, (sido, sigungu)
    return key, (sido, sigungu)


def get_direct_plt_threshold(session):
    """직송 전환 기준 PLT 수 (기본 3)"""
    cfg = session.query(SystemConfig).filter_by(key='direct_plt_threshold').first()
    try:
        return float(cfg.value) if cfg else 3.0
    except Exception:
        return 3.0


def get_joint_rate(destination, session):
    """공동배송 박스당 단가: 도착지 우선, 없으면 '기본' 적용"""
    if destination:
        rate = session.query(JointDeliveryRate).filter_by(destination=destination).first()
        if rate:
            return rate.price_per_box
    default = session.query(JointDeliveryRate).filter_by(destination='기본').first()
    return default.price_per_box if default else None


def find_best_vehicle(plt_count, destination, session):
    caps = session.query(VehicleCapacity).order_by(VehicleCapacity.sort_order).all()
    if not caps:
        return None, None, None
    max_single = max(c.max_plt for c in caps)

    if plt_count > max_single:
        largest = max(caps, key=lambda c: c.max_plt)
        truck_count = math.ceil(plt_count / largest.max_plt)
        rate = session.query(VehicleRate).filter_by(
            destination=destination, vehicle_type=largest.vehicle_type
        ).first()
        if rate:
            return largest.vehicle_type, rate.unit_price * truck_count, truck_count
        return None, None, None

    eligible = {c.vehicle_type for c in caps if c.max_plt >= plt_count}
    rates = session.query(VehicleRate).filter(
        VehicleRate.destination == destination,
        VehicleRate.vehicle_type.in_(eligible)
    ).all()
    if not rates:
        return None, None, None
    best = min(rates, key=lambda r: r.unit_price)
    return best.vehicle_type, best.unit_price, 1


def calculate_from_history(history_rows, customer_id, calc_name, session):
    threshold = get_direct_plt_threshold(session)

    # 그룹핑: 배송처코드 + 납품일자
    groups = {}
    for row in history_rows:
        key = (row.store_code or row.store_name, row.shipping_date)
        groups.setdefault(key, []).append(row)

    results, errors = [], []

    for (store_key, ship_date), rows in groups.items():
        rep = rows[0]
        address = rep.address
        store_name = rep.store_name
        store_code = rep.store_code

        total_box = sum(r.box_qty or 0 for r in rows)
        total_plt_dec = sum(r.plt_qty_decimal or 0 for r in rows)
        total_plt_int = math.ceil(total_plt_dec) if total_plt_dec > 0 else 0

        # PLT 없으면 상품마스터에서 계산
        if total_plt_dec == 0:
            for r in rows:
                if r.product_code and r.box_qty:
                    p = session.query(ProductMaster).filter_by(
                        customer_id=customer_id, product_code=r.product_code
                    ).first()
                    if p and p.plt_per_box and p.plt_per_box > 0:
                        total_plt_dec += r.box_qty / p.plt_per_box
            total_plt_int = math.ceil(total_plt_dec) if total_plt_dec > 0 else 0

        if total_plt_int == 0 and total_plt_dec == 0:
            errors.append(f"PLT 미산출: {store_name or store_code} ({ship_date})")
            continue

        # 도착지 매핑
        destination, _ = find_destination(address, session)
        if not destination and store_code:
            store = session.query(StoreMaster).filter_by(
                customer_id=customer_id, store_code=store_code
            ).first()
            if store:
                destination = store.destination or (
                    find_destination(store.address, session)[0] if store.address else None
                )

        # ── 직송 vs 공동배송 판단 ───────────────────────────────────────
        if total_plt_dec >= threshold:
            delivery_mode = '직송'
            vehicle_type, delivery_cost, truck_count = find_best_vehicle(
                total_plt_int, destination, session
            )
            if delivery_cost is None:
                memo = f'차량단가 없음 ({destination})'
                if not destination:
                    memo = '도착지 미매핑'
                errors.append(f"{delivery_mode} 단가없음: {store_name or store_code} | {memo}")
            else:
                memo = f'{truck_count}대 투입' if truck_count and truck_count > 1 else None
        else:
            delivery_mode = '공동배송'
            vehicle_type = None
            truck_count = None
            joint_price = get_joint_rate(destination, session)
            if joint_price is not None and total_box > 0:
                delivery_cost = round(joint_price * total_box)
            else:
                delivery_cost = None
                if joint_price is None:
                    errors.append(f"공동배송 단가없음: {store_name or store_code}")
            memo = f'공동배송 {joint_price}원/박스' if joint_price else '공동배송단가 미등록'

        cost_per_box = round(delivery_cost / total_box, 1) if delivery_cost and total_box > 0 else None

        results.append({
            'store_code': store_code,
            'store_name': store_name,
            'address': address,
            'destination': destination,
            'shipping_date': ship_date,
            'total_box_qty': total_box,
            'total_plt_decimal': round(total_plt_dec, 3),
            'total_plt_count': total_plt_int,
            'delivery_mode': delivery_mode,
            'vehicle_type': vehicle_type,
            'delivery_cost': delivery_cost,
            'cost_per_box': cost_per_box,
            'memo': memo,
        })

    return results, errors


def summarize_results(results):
    if not results:
        return {}

    valid = [r for r in results if r.get('delivery_cost') is not None]
    total_cost = sum(r['delivery_cost'] for r in valid)
    total_boxes = sum(r['total_box_qty'] for r in valid)

    direct = [r for r in valid if r['delivery_mode'] == '직송']
    joint = [r for r in valid if r['delivery_mode'] == '공동배송']

    def avg_cpb(rows):
        cost = sum(r['delivery_cost'] for r in rows)
        boxes = sum(r['total_box_qty'] for r in rows)
        return round(cost / boxes, 1) if boxes > 0 else None

    by_dest = {}
    for r in valid:
        d = r['destination'] or '미매핑'
        by_dest.setdefault(d, {'count': 0, 'total_cost': 0, 'total_boxes': 0, 'mode': r['delivery_mode']})
        by_dest[d]['count'] += 1
        by_dest[d]['total_cost'] += r['delivery_cost']
        by_dest[d]['total_boxes'] += r['total_box_qty'] or 0

    dest_summary = sorted([
        {
            'destination': d,
            'mode': v['mode'],
            'count': v['count'],
            'total_cost': v['total_cost'],
            'total_boxes': v['total_boxes'],
            'avg_cost_per_box': round(v['total_cost'] / v['total_boxes'], 1) if v['total_boxes'] > 0 else None,
        }
        for d, v in by_dest.items()
    ], key=lambda x: x['total_cost'], reverse=True)

    return {
        'total_deliveries': len(results),
        'valid_count': len(valid),
        'error_count': len(results) - len(valid),
        'total_cost': total_cost,
        'total_boxes': total_boxes,
        'avg_cost_per_box': round(total_cost / total_boxes, 1) if total_boxes > 0 else None,
        'direct_count': len(direct),
        'joint_count': len(joint),
        'direct_avg_cpb': avg_cpb(direct),
        'joint_avg_cpb': avg_cpb(joint),
        'dest_summary': dest_summary,
    }
