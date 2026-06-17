import math
from models import (VehicleRate, VehicleCapacity, ProductMaster, StoreMaster,
                    SystemConfig, TransferRate, HubVehicleRate, OurCenter)

SIDO_NORMALIZE = {
    '경기': '경기도', '강원': '강원도', '충북': '충청북도', '충남': '충청남도',
    '전북': '전라북도', '전남': '전라남도', '경북': '경상북도', '경남': '경상남도',
    '서울': '서울특별시', '부산': '부산광역시', '대구': '대구광역시', '인천': '인천광역시',
    '광주': '광주광역시', '대전': '대전광역시', '울산': '울산광역시',
    '세종': '세종특별자치시', '제주': '제주특별자치도',
    '경기도': '경기도', '강원도': '강원도', '충청북도': '충청북도', '충청남도': '충청남도',
    '전라북도': '전라북도', '전라남도': '전라남도', '경상북도': '경상북도', '경상남도': '경상남도',
    '서울특별시': '서울특별시', '부산광역시': '부산광역시', '대구광역시': '대구광역시',
    '인천광역시': '인천광역시', '광주광역시': '광주광역시', '대전광역시': '대전광역시',
    '울산광역시': '울산광역시', '제주특별자치도': '제주특별자치도',
    '세종특별자치시': '세종특별자치시',
    '강원특별자치도': '강원도',
    '전북특별자치도': '전라북도',
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


def find_destination(address, center_code, session):
    """주소 → 도착지 키(시도 시군구) 매핑. 차량단가 마스터 기준."""
    sido, sigungu = extract_sido_sigungu(address)
    if not sido or not sigungu:
        return None, (None, None)
    key = make_destination_key(sido, sigungu)
    rate = session.query(VehicleRate).filter_by(center_code=center_code, destination=key).first()
    if rate:
        return key, (sido, sigungu)
    rate = session.query(VehicleRate).filter(
        VehicleRate.center_code == center_code,
        VehicleRate.destination.like(f'%{sigungu}%')
    ).first()
    if rate:
        return rate.destination, (sido, sigungu)
    return key, (sido, sigungu)


def get_direct_plt_threshold(session):
    cfg = session.query(SystemConfig).filter_by(key='direct_plt_threshold').first()
    try:
        return float(cfg.value) if cfg else 3.0
    except Exception:
        return 3.0


def find_best_vehicle(plt_count, destination, center_code, session):
    """직송 최적 차량 선택 (센터 기준, 최저 비용)."""
    caps = session.query(VehicleCapacity).order_by(VehicleCapacity.sort_order).all()
    if not caps:
        return None, None, None
    max_single = max(c.max_plt for c in caps)

    if plt_count > max_single:
        largest = max(caps, key=lambda c: c.max_plt)
        truck_count = math.ceil(plt_count / largest.max_plt)
        rate = session.query(VehicleRate).filter_by(
            center_code=center_code, destination=destination,
            vehicle_type=largest.vehicle_type
        ).first()
        if rate:
            return largest.vehicle_type, rate.unit_price * truck_count, truck_count
        return None, None, None

    eligible = {c.vehicle_type for c in caps if c.max_plt >= plt_count}
    rates = session.query(VehicleRate).filter(
        VehicleRate.center_code == center_code,
        VehicleRate.destination == destination,
        VehicleRate.vehicle_type.in_(eligible)
    ).all()
    if not rates:
        return None, None, None
    best = min(rates, key=lambda r: r.unit_price)
    return best.vehicle_type, best.unit_price, 1


def find_hub_center_code(store_code, customer_id, session):
    """점포마스터의 center_name → OurCenter.center_code."""
    if not store_code:
        return None
    store = session.query(StoreMaster).filter_by(
        customer_id=customer_id, store_code=store_code
    ).first()
    if not store or not store.center_name:
        return None
    center = session.query(OurCenter).filter(
        OurCenter.center_name == store.center_name
    ).first()
    return center.center_code if center else None


def get_joint_cost(plt_dec, main_center_code, hub_center_code, session):
    """
    공동배송 비용 = 이고비(PLT용적률 비례) + 변동용차비(1톤 기준, PLT용적률 비례)
    Returns: (total_cost, transfer_cost, hub_cost, memo)
    """
    caps = {c.vehicle_type: c.max_plt
            for c in session.query(VehicleCapacity).all()}

    # 이고비: 메인센터 → 거점센터, 최저 비용 차량
    transfer_cost = None
    transfer_memo = ''
    if main_center_code and hub_center_code and main_center_code != hub_center_code:
        trs = session.query(TransferRate).filter_by(
            from_center_code=main_center_code,
            to_center_code=hub_center_code
        ).all()
        if trs:
            best = None
            for tr in trs:
                max_plt = caps.get(tr.vehicle_type, 1.0)
                cost = tr.unit_price * (plt_dec / max_plt)
                if best is None or cost < best[0]:
                    best = (cost, tr.vehicle_type, tr.unit_price, max_plt)
            if best:
                transfer_cost = round(best[0])
                transfer_memo = (
                    f'이고({best[1]}) {best[2]:,}원'
                    f'×({round(plt_dec, 2)}÷{best[3]}PLT)'
                )

    # 변동용차비: 거점센터 기준, 1톤 우선 → 없으면 최저가 차량
    hub_cost = None
    hub_memo = ''
    if hub_center_code:
        hvr = session.query(HubVehicleRate).filter_by(
            center_code=hub_center_code, vehicle_type='1톤'
        ).first()
        if not hvr:
            hvrs = session.query(HubVehicleRate).filter_by(
                center_code=hub_center_code
            ).all()
            if hvrs:
                hvr = min(
                    hvrs,
                    key=lambda h: h.unit_price * (plt_dec / caps.get(h.vehicle_type, 1.0))
                )
        if hvr:
            max_plt = caps.get(hvr.vehicle_type, 1.0)
            hub_cost = round(hvr.unit_price * (plt_dec / max_plt))
            hub_memo = (
                f'변동용차({hvr.vehicle_type}) {hvr.unit_price:,}원'
                f'×({round(plt_dec, 2)}÷{max_plt}PLT)'
            )

    parts = [m for m in [transfer_memo, hub_memo] if m]
    total = (transfer_cost or 0) + (hub_cost or 0) if (transfer_cost is not None or hub_cost is not None) else None
    if total is None:
        return None, None, None, '공동배송 단가 없음'
    return total, transfer_cost, hub_cost, ' + '.join(parts)


def calculate_from_history(history_rows, customer_id, calc_name, main_center_code, session):
    threshold = get_direct_plt_threshold(session)

    groups = {}
    for row in history_rows:
        key = (row.store_code or row.store_name, row.shipping_date)
        groups.setdefault(key, []).append(row)

    results, errors = [], []

    for (store_key, ship_date), rows in groups.items():
        rep        = rows[0]
        address    = rep.address
        store_name = rep.store_name
        store_code = rep.store_code

        total_box     = sum(r.box_qty or 0 for r in rows)
        total_plt_dec = sum(r.plt_qty_decimal or 0 for r in rows)
        total_plt_int = math.ceil(total_plt_dec) if total_plt_dec > 0 else 0

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
        destination, _ = find_destination(address, main_center_code, session)
        if not destination and store_code:
            store = session.query(StoreMaster).filter_by(
                customer_id=customer_id, store_code=store_code
            ).first()
            if store:
                destination = store.destination or (
                    find_destination(store.address, main_center_code, session)[0]
                    if store.address else None
                )

        # 직송 vs 공동배송
        if total_plt_dec >= threshold:
            delivery_mode = '직송'
            vehicle_type, delivery_cost, truck_count = find_best_vehicle(
                total_plt_int, destination, main_center_code, session
            )
            if delivery_cost is None:
                memo = f'차량단가 없음 ({destination})' if destination else '도착지 미매핑'
                errors.append(f"직송 단가없음: {store_name or store_code} | {memo}")
            else:
                memo = f'{truck_count}대 투입' if truck_count and truck_count > 1 else None
        else:
            delivery_mode = '공동배송'
            vehicle_type  = None
            truck_count   = None

            hub_center_code = find_hub_center_code(store_code, customer_id, session)

            delivery_cost, _tc, _hc, memo = get_joint_cost(
                total_plt_dec, main_center_code, hub_center_code, session
            )
            if delivery_cost is None:
                if not hub_center_code:
                    errors.append(
                        f"공동배송: 거점센터 미지정 — {store_name or store_code} "
                        f"(점포마스터 → 센터명 입력 필요)"
                    )
                else:
                    errors.append(f"공동배송 단가없음: {store_name or store_code} [{hub_center_code}]")

        cost_per_box = (
            round(delivery_cost / total_box, 1)
            if delivery_cost and total_box > 0 else None
        )

        results.append({
            'store_code':       store_code,
            'store_name':       store_name,
            'address':          address,
            'destination':      destination,
            'shipping_date':    ship_date,
            'total_box_qty':    total_box,
            'total_plt_decimal': round(total_plt_dec, 3),
            'total_plt_count':  total_plt_int,
            'delivery_mode':    delivery_mode,
            'vehicle_type':     vehicle_type,
            'delivery_cost':    delivery_cost,
            'cost_per_box':     cost_per_box,
            'memo':             memo,
        })

    return results, errors


def summarize_results(results):
    if not results:
        return {}

    valid       = [r for r in results if r.get('delivery_cost') is not None]
    total_cost  = sum(r['delivery_cost'] for r in valid)
    total_boxes = sum(r['total_box_qty'] for r in valid)
    direct      = [r for r in valid if r['delivery_mode'] == '직송']
    joint       = [r for r in valid if r['delivery_mode'] == '공동배송']

    def avg_cpb(rows):
        cost  = sum(r['delivery_cost'] for r in rows)
        boxes = sum(r['total_box_qty'] for r in rows)
        return round(cost / boxes, 1) if boxes > 0 else None

    by_dest = {}
    for r in valid:
        d = r['destination'] or '미매핑'
        by_dest.setdefault(d, {'count': 0, 'total_cost': 0, 'total_boxes': 0, 'mode': r['delivery_mode']})
        by_dest[d]['count']       += 1
        by_dest[d]['total_cost']  += r['delivery_cost']
        by_dest[d]['total_boxes'] += r['total_box_qty'] or 0

    dest_summary = sorted([
        {
            'destination':     d,
            'mode':            v['mode'],
            'count':           v['count'],
            'total_cost':      v['total_cost'],
            'total_boxes':     v['total_boxes'],
            'avg_cost_per_box': round(v['total_cost'] / v['total_boxes'], 1) if v['total_boxes'] > 0 else None,
        }
        for d, v in by_dest.items()
    ], key=lambda x: x['total_cost'], reverse=True)

    return {
        'total_deliveries': len(results),
        'valid_count':      len(valid),
        'error_count':      len(results) - len(valid),
        'total_cost':       total_cost,
        'total_boxes':      total_boxes,
        'avg_cost_per_box': round(total_cost / total_boxes, 1) if total_boxes > 0 else None,
        'direct_count':     len(direct),
        'joint_count':      len(joint),
        'direct_avg_cpb':   avg_cpb(direct),
        'joint_avg_cpb':    avg_cpb(joint),
        'dest_summary':     dest_summary,
    }
