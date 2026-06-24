import math
from models import (VehicleRate, VehicleCapacity, ProductMaster, StoreMaster,
                    SystemConfig, TransferRate, HubVehicleRate, OurCenter,
                    VehicleDistanceRate, DestinationCoord)

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


_HUB_PLT_CAP = [
    (2,  '1T'),
    (3,  '2.5T'),
    (5,  '3.5T'),
    (10, '5T'),
    (16, '11T'),
]


def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    r = math.pi / 180
    dlat = (lat2 - lat1) * r
    dlon = (lon2 - lon1) * r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1 * r) * math.cos(lat2 * r) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _hub_vehicle_type_for_plt(plt_dec):
    for max_plt, vtype in _HUB_PLT_CAP:
        if plt_dec <= max_plt:
            return vtype, max_plt
    return '11T', 16


def _fixed_hub_cost_calc(plt_dec, hub_center_code, caps, session):
    """HubVehicleRate 고정단가 기반 변동용차비 (PLT 비례)."""
    hvr = session.query(HubVehicleRate).filter_by(
        center_code=hub_center_code, vehicle_type='1톤'
    ).first()
    if not hvr:
        hvrs = session.query(HubVehicleRate).filter_by(center_code=hub_center_code).all()
        if hvrs:
            hvr = min(hvrs, key=lambda h: h.unit_price * (plt_dec / max(caps.get(h.vehicle_type, 1.0), 0.001)))
    if hvr:
        max_plt = caps.get(hvr.vehicle_type, 1.0)
        return round(hvr.unit_price * (plt_dec / max_plt))
    return 0


def _plan_routes_hub_cost(hub_lat, hub_lon, dest_plts, stops_per_vehicle, dist_rate_map):
    """
    planRoutes 실행 → 총 변동용차비 및 도착지별 배분 비용 반환.
    dest_plts: list of (lat, lon, plt_dec, dest_key)
    Returns: (total_cost, {dest_key: allocated_cost_float})
    """
    if not dest_plts:
        return 0, {}

    sorted_z = sorted(dest_plts, key=lambda z: math.atan2(z[1] - hub_lon, z[0] - hub_lat))

    total_cost = 0
    dest_cost  = {}

    for v in range(math.ceil(len(sorted_z) / stops_per_vehicle)):
        batch = sorted_z[v * stops_per_vehicle:(v + 1) * stops_per_vehicle]

        unvisited = list(batch)
        route = []
        cur_lat, cur_lon = hub_lat, hub_lon
        while unvisited:
            bi, bd = 0, float('inf')
            for i, (lat, lon, _, _) in enumerate(unvisited):
                d = _haversine_km(cur_lat, cur_lon, lat, lon)
                if d < bd:
                    bd, bi = d, i
            stop = unvisited.pop(bi)
            route.append(stop)
            cur_lat, cur_lon = stop[0], stop[1]

        km = 0.0
        cur_lat, cur_lon = hub_lat, hub_lon
        for lat, lon, _, _ in route:
            km += _haversine_km(cur_lat, cur_lon, lat, lon)
            cur_lat, cur_lon = lat, lon
        road_km = max(1, min(1000, round(km * 1.3)))

        route_plt = sum(plt for _, _, plt, _ in route)
        if route_plt <= 0:
            continue

        sel_vt, sel_cap = '11T', 16
        for max_p, vt in _HUB_PLT_CAP:
            if max_p >= route_plt:
                sel_vt, sel_cap = vt, max_p
                break

        up = (dist_rate_map.get(sel_vt) or {}).get(road_km)
        if up is None:
            continue

        route_cost = round(up * (route_plt / sel_cap))
        total_cost += route_cost

        for _, _, plt, dest in route:
            frac = plt / route_plt if route_plt > 0 else 1.0 / len(route)
            dest_cost[dest] = dest_cost.get(dest, 0) + route_cost * frac

    return total_cost, dest_cost


def get_hub_distance_cost(plt_dec, hub_center_code, destination, session,
                          stops_per_vehicle=1):
    """
    거점 변동용차 거리 기반 비용 계산.
    stops_per_vehicle: 루트당 도착지 수 (SystemConfig). PLT > 최대 적재 시 동시에 여러 차 투입.
    Returns (cost, memo) or (None, reason_str)
    """
    if not hub_center_code or not destination:
        return None, '거점센터 또는 도착지 미지정'

    center = session.query(OurCenter).filter_by(center_code=hub_center_code).first()
    if not center or not center.lat or not center.lon:
        return None, f'거점센터 좌표 없음({hub_center_code})'

    coord = session.query(DestinationCoord).filter_by(destination=destination).first()
    if not coord:
        return None, f'도착지 좌표 없음({destination})'

    straight_km = _haversine_km(center.lat, center.lon, coord.lat, coord.lon)
    road_km = max(1, round(straight_km * 1.3))
    road_km = min(road_km, 1000)

    # 루트 총 PLT = 이 점포 PLT × 루트 내 도착지 수
    route_plt = plt_dec * stops_per_vehicle
    vtype, max_plt = _hub_vehicle_type_for_plt(route_plt)

    row = session.query(VehicleDistanceRate).filter_by(
        vehicle_type=vtype, km=road_km
    ).first()
    if not row:
        return None, f'거리단가 없음({vtype},{road_km}km)'

    # 비례 배차: 루트 총 PLT / 차종 최대 PLT
    route_cost = round(row.unit_price * (route_plt / max_plt))
    store_cost = round(route_cost / stops_per_vehicle)

    memo = (
        f'변동용차({vtype}) {row.unit_price:,}원'
        f'×({round(route_plt,2)}÷{max_plt}PLT)÷{stops_per_vehicle}점포 [{road_km}km]'
    )
    return store_cost, memo


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


def get_joint_cost(plt_dec, main_center_code, hub_center_code, session,
                   destination=None, stops_per_vehicle=1):
    """
    공동배송 비용 = 이고비(PLT 비례) + 변동용차비(거리 기반 우선, 없으면 고정단가)
    PLT > 최대 적재 시 동시에 여러 차 투입 (왕복 반복 아님).
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
                ratio = plt_dec / max_plt
                cost = tr.unit_price * ratio  # 항상 비례 (경계 불연속 없음)
                if best is None or cost < best[0]:
                    best = (cost, tr.vehicle_type, tr.unit_price, max_plt, ratio)
            if best:
                transfer_cost = round(best[0])
                transfer_memo = f'이고({best[1]}) {best[2]:,}원×({round(plt_dec,2)}÷{best[3]}PLT)'

    # 변동용차비: ① 거리 기반(VehicleDistanceRate) 우선 ② 없으면 고정단가(HubVehicleRate)
    hub_cost = None
    hub_memo = ''
    if hub_center_code:
        # ① 거리 기반 (stops_per_vehicle 반영)
        dist_has_data = session.query(VehicleDistanceRate).first() is not None
        if dist_has_data and destination:
            hub_cost, hub_memo = get_hub_distance_cost(
                plt_dec, hub_center_code, destination, session,
                stops_per_vehicle=stops_per_vehicle
            )
            if hub_cost is None:
                hub_memo = ''

        # ② 고정단가 폴백
        if hub_cost is None:
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
                hub_memo = f'변동용차({hvr.vehicle_type}) {hvr.unit_price:,}원×({round(plt_dec,2)}÷{max_plt}PLT)'

    parts = [m for m in [transfer_memo, hub_memo] if m]
    total = (transfer_cost or 0) + (hub_cost or 0) if (transfer_cost is not None or hub_cost is not None) else None
    if total is None:
        return None, None, None, '공동배송 단가 없음'
    return total, transfer_cost, hub_cost, ' + '.join(parts)


def calculate_from_history(history_rows, customer_id, calc_name, main_center_code, session):
    """
    3단계 계산:
    Phase 1: 행별 PLT·도착지·이고비 계산, hub_cost는 보류
    Phase 2: 공동배송을 (날짜, 거점센터)별로 묶어 planRoutes → PLT 비례 배분
    Phase 3: delivery_cost·cost_per_box 확정
    """
    threshold = get_direct_plt_threshold(session)
    _spv_cfg = session.query(SystemConfig).filter_by(key='stops_per_vehicle').first()
    stops_per_vehicle = int(_spv_cfg.value) if _spv_cfg else 8

    caps = {c.vehicle_type: c.max_plt for c in session.query(VehicleCapacity).all()}
    dist_rate_map = {}
    for dr in session.query(VehicleDistanceRate).all():
        dist_rate_map.setdefault(dr.vehicle_type, {})[dr.km] = dr.unit_price

    groups = {}
    for row in history_rows:
        key = (row.store_code or row.store_name, row.shipping_date)
        groups.setdefault(key, []).append(row)

    pre, errors = [], []

    # ── Phase 1: 행별 처리 ─────────────────────────────────────────────────────
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

        if total_plt_dec >= threshold:
            # 직송
            vehicle_type, delivery_cost, truck_count = find_best_vehicle(
                total_plt_int, destination, main_center_code, session
            )
            if delivery_cost is None:
                memo = f'차량단가 없음 ({destination})' if destination else '도착지 미매핑'
                errors.append(f"직송 단가없음: {store_name or store_code} | {memo}")
                continue
            pre.append({
                'store_code': store_code, 'store_name': store_name, 'address': address,
                'destination': destination, 'shipping_date': ship_date,
                'total_box_qty': total_box, 'total_plt_decimal': round(total_plt_dec, 3),
                'total_plt_count': total_plt_int, 'delivery_mode': '직송',
                'vehicle_type': vehicle_type, 'delivery_cost': delivery_cost,
                'transfer_cost': None, 'hub_cost': None,
                'cost_per_box': round(delivery_cost / total_box, 1) if total_box > 0 else None,
                'memo': f'{truck_count}대 투입' if truck_count and truck_count > 1 else None,
                '_joint': False,
            })
        else:
            # 공동배송: 이고비만 계산, hub_cost는 Phase 2에서 산정
            hub_center_code = find_hub_center_code(store_code, customer_id, session)
            if not hub_center_code:
                errors.append(
                    f"공동배송: 거점센터 미지정 — {store_name or store_code} "
                    f"(점포마스터 → 센터명 입력 필요)"
                )
                continue

            transfer_cost = None
            transfer_memo = ''
            if main_center_code and hub_center_code and main_center_code != hub_center_code:
                trs = session.query(TransferRate).filter_by(
                    from_center_code=main_center_code, to_center_code=hub_center_code
                ).all()
                if trs:
                    best = None
                    for tr in trs:
                        mp   = caps.get(tr.vehicle_type, 1.0)
                        cost = tr.unit_price * (total_plt_dec / mp)
                        if best is None or cost < best[0]:
                            best = (cost, tr.vehicle_type, tr.unit_price, mp)
                    if best:
                        transfer_cost = round(best[0])
                        transfer_memo = (f'이고({best[1]}) {best[2]:,}원'
                                         f'×({round(total_plt_dec,2)}÷{best[3]}PLT)')

            pre.append({
                'store_code': store_code, 'store_name': store_name, 'address': address,
                'destination': destination, 'shipping_date': ship_date,
                'total_box_qty': total_box, 'total_plt_decimal': round(total_plt_dec, 3),
                'total_plt_count': total_plt_int, 'delivery_mode': '공동배송',
                'vehicle_type': None, 'delivery_cost': None,
                'transfer_cost': transfer_cost, 'hub_cost': None,
                'cost_per_box': None, 'memo': None,
                '_joint': True, '_hub': hub_center_code,
                '_plt': total_plt_dec, '_tmemo': transfer_memo,
            })

    # ── Phase 2: planRoutes 기반 변동용차비 배분 ────────────────────────────────
    # (날짜, 거점센터)별 그룹 → 루트 계산 → 행별 PLT 비례 배분
    joint_groups = {}
    for i, r in enumerate(pre):
        if r.get('_joint'):
            joint_groups.setdefault((r['shipping_date'], r['_hub']), []).append(i)

    for (ship_date, hub_code), idxs in joint_groups.items():
        center = session.query(OurCenter).filter_by(center_code=hub_code).first()

        if not center or not center.lat or not center.lon or not dist_rate_map:
            # 좌표 없음 또는 거리단가 없음 → 고정단가 폴백
            for i in idxs:
                plt_dec  = pre[i]['_plt']
                hub_cost = _fixed_hub_cost_calc(plt_dec, hub_code, caps, session)
                _commit_joint_row(pre[i], hub_cost)
            continue

        hub_lat = float(center.lat)
        hub_lon = float(center.lon)

        # 도착지별 PLT 집계
        dest_plt   = {}  # dest → 합산 PLT
        dest_idxs  = {}  # dest → [(pre 인덱스, plt_dec)]
        for i in idxs:
            dest    = pre[i]['destination']
            plt_dec = pre[i]['_plt']
            dest_plt[dest]  = dest_plt.get(dest, 0) + plt_dec
            dest_idxs.setdefault(dest, []).append((i, plt_dec))

        # 좌표 조회
        dest_coords = {}
        for dest in dest_plt:
            if dest:
                coord = session.query(DestinationCoord).filter_by(destination=dest).first()
                if coord:
                    dest_coords[dest] = (float(coord.lat), float(coord.lon))

        # 좌표 있는 도착지: planRoutes
        zones = [(lat, lon, dest_plt[dest], dest)
                 for dest, (lat, lon) in dest_coords.items()]
        _, dest_cost_f = _plan_routes_hub_cost(hub_lat, hub_lon, zones, stops_per_vehicle, dist_rate_map)

        for dest, (lat, lon) in dest_coords.items():
            total_dest_cost = dest_cost_f.get(dest, 0)
            total_dest_plt  = dest_plt[dest]
            for i, plt_dec in dest_idxs[dest]:
                frac     = plt_dec / total_dest_plt if total_dest_plt > 0 else 1.0
                hub_cost = round(total_dest_cost * frac)
                _commit_joint_row(pre[i], hub_cost)

        # 좌표 없는 도착지: 고정단가 폴백
        for dest in set(dest_plt) - set(dest_coords):
            for i, plt_dec in dest_idxs.get(dest, []):
                hub_cost = _fixed_hub_cost_calc(plt_dec, hub_code, caps, session)
                _commit_joint_row(pre[i], hub_cost)

    # ── Phase 3: 정리 ──────────────────────────────────────────────────────────
    results = []
    for r in pre:
        if r.get('_joint') and r['delivery_cost'] is None:
            errors.append(
                f"공동배송 단가없음: {r['store_name'] or r['store_code']} [{r.get('_hub')}]"
            )
            continue
        results.append({k: v for k, v in r.items() if not k.startswith('_')})

    return results, errors


def _commit_joint_row(row, hub_cost):
    """공동배송 행에 hub_cost 및 최종 delivery_cost·memo를 기록한다."""
    tc = row.get('transfer_cost') or 0
    hc = hub_cost or 0
    total = tc + hc
    if total == 0 and row.get('transfer_cost') is None:
        return  # 단가 없음 상태 유지
    row['hub_cost'] = hc or None
    row['delivery_cost'] = total
    boxes = row['total_box_qty']
    row['cost_per_box'] = round(total / boxes, 1) if boxes > 0 else None
    parts = [m for m in [row.get('_tmemo', ''), '변동용차 PLT비례배분' if hc else ''] if m]
    row['memo'] = ' + '.join(parts) if parts else None


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


def _norm_center_code(code):
    """DZM의 0-패딩 코드를 OurCenter/TransferRate 코드로 정규화. '0002000' → '2000'"""
    if not code:
        return code
    try:
        return str(int(code))
    except (ValueError, TypeError):
        return code  # '1100D' 같은 알파뉴메릭은 그대로


def compute_joint_breakdown_live(customer_id, main_center_code, stops_per_vehicle, session,
                                  threshold=None):
    """
    현재 stops_per_vehicle 기준으로 이고비/변동용차비 합계를 실시간 계산.
    threshold 지정 시 delivery_mode 대신 PLT < threshold 기준으로 공동배송 분류.
    Returns: (total_transfer_cost, total_hub_cost)
    """
    from models import CalculationResult, DeliveryZoneMapping

    # ─── 프리로드 ──────────────────────────────────────────────────────────────
    caps        = {c.vehicle_type: c.max_plt for c in session.query(VehicleCapacity).all()}
    all_centers = {c.center_code: c for c in session.query(OurCenter).all()}
    all_coords  = {dc.destination: dc for dc in session.query(DestinationCoord).all()}

    # 1) StoreMaster 기반 hub 코드 (center_name 있는 경우)
    center_by_name = {c.center_name: c.center_code for c in all_centers.values()}
    store_hub_map = {}
    for s in session.query(StoreMaster).filter_by(customer_id=customer_id).all():
        if s.store_code and s.center_name and s.center_name in center_by_name:
            store_hub_map[s.store_code] = center_by_name[s.center_name]

    # 2) DeliveryZoneMapping 기반 (sido+sigungu → hub_code)
    dzm_hub_map = {}  # (sido, sigungu) → normalized center_code
    for dz in session.query(DeliveryZoneMapping).all():
        norm = _norm_center_code(dz.center_code)
        if norm in all_centers:
            dzm_hub_map[(dz.sido, dz.sigungu)] = norm

    # 3) TransferRate 맵 (from_code, to_code 모두 정규화)
    transfer_rate_map = {}
    for tr in session.query(TransferRate).all():
        nf = _norm_center_code(tr.from_center_code)
        nt = _norm_center_code(tr.to_center_code)
        transfer_rate_map.setdefault((nf, nt), []).append(tr)

    # 가능한 메인 센터 코드 목록 (is_main_center + from_codes에 있는 것)
    main_codes_in_tr = {_norm_center_code(k[0]) for k in transfer_rate_map}
    all_main_codes   = list(main_codes_in_tr)

    # 4) 거리 요율 맵
    dist_rate_map = {}
    for dr in session.query(VehicleDistanceRate).all():
        dist_rate_map.setdefault(dr.vehicle_type, {})[dr.km] = dr.unit_price
    has_dist = bool(dist_rate_map)

    hub_fixed_map = {}
    for hvr in session.query(HubVehicleRate).all():
        hub_fixed_map.setdefault(hvr.center_code, []).append(hvr)

    # ─── 공동배송 행 조회 ────────────────────────────────────────────────────
    _base = session.query(CalculationResult).filter(
        CalculationResult.customer_id == customer_id,
        CalculationResult.total_plt_decimal.isnot(None),
    )
    if threshold is not None:
        joint_rows = _base.filter(CalculationResult.total_plt_decimal < threshold).all()
    else:
        joint_rows = _base.filter(CalculationResult.delivery_mode == '공동배송').all()

    total_transfer = 0
    total_hub = 0

    for row in joint_rows:
        plt_dec = row.total_plt_decimal or 0
        dest    = row.destination or ''

        # hub_code: StoreMaster 우선, 없으면 DZM
        hub_code = store_hub_map.get(row.store_code or '')
        if not hub_code and dest:
            parts = dest.split()
            if len(parts) >= 2:
                hub_code = dzm_hub_map.get((parts[0], parts[1]))

        if not hub_code:
            continue

        # ── 이고비 ─────────────────────────────────────────────────────────
        # 현재 main_center_code 우선, 없으면 TransferRate에 있는 가장 저렴한 경로
        def _best_transfer(from_code, to_code):
            trs = transfer_rate_map.get((_norm_center_code(from_code), _norm_center_code(to_code)), [])
            best = None
            for tr in trs:
                mp   = caps.get(tr.vehicle_type, 1.0)
                cost = tr.unit_price * (plt_dec / mp)
                if best is None or cost < best:
                    best = cost
            return best

        norm_hub = _norm_center_code(hub_code)
        norm_main = _norm_center_code(main_center_code) if main_center_code else None

        if norm_main and norm_main != norm_hub:
            best_tc = _best_transfer(norm_main, norm_hub)
        else:
            # main_center_code가 없거나 같은 센터면 모든 경로 중 최저 탐색
            best_tc = None
            for mc in all_main_codes:
                if mc != norm_hub:
                    c = _best_transfer(mc, norm_hub)
                    if c is not None and (best_tc is None or c < best_tc):
                        best_tc = c

        if best_tc is not None:
            total_transfer += round(best_tc)

        # ── 변동용차비 ──────────────────────────────────────────────────────
        hc_obj = all_centers.get(norm_hub)
        used_dist = False
        if hc_obj and hc_obj.lat and dest:
            coord = all_coords.get(dest)
            if coord:
                sk = _haversine_km(hc_obj.lat, hc_obj.lon, coord.lat, coord.lon)
                road_km = min(1000, max(1, round(sk * 1.3)))
                route_plt = plt_dec * stops_per_vehicle
                vtype, max_plt = _hub_vehicle_type_for_plt(route_plt)
                if has_dist:
                    up = (dist_rate_map.get(vtype) or {}).get(road_km)
                    if up:
                        total_hub += round(up * (route_plt / max_plt) / stops_per_vehicle)
                        used_dist = True
        # 고정단가 폴백: 거리요율 없거나 좌표 없을 때 (hub 코드만 있으면 적용)
        if not used_dist:
            hvrs = hub_fixed_map.get(norm_hub, [])
            best_hc = None
            for hvr in hvrs:
                mp   = caps.get(hvr.vehicle_type, 1.0)
                cost = hvr.unit_price * (plt_dec / mp)
                if best_hc is None or cost < best_hc:
                    best_hc = cost
            if best_hc is not None:
                total_hub += round(best_hc)

    return total_transfer, total_hub


def compute_hub_daily_avg_by_avgplt(customer_id, stops_per_vehicle, session):
    """
    planRoutes 기반 일평균 변동용차비 (지도 시뮬레이션과 동일 방식).
    거점별 zones을 각도-배치 → NN-TSP → 루트 누적km/합산PLT로 비례 비용 산출.
    JS getRouteCost: cost = round(unitPrice * (totalPlt / selCap))
    """
    from sqlalchemy import func as sqlfunc
    from models import CalculationResult, DeliveryZoneMapping

    joint_biz_days = session.query(
        sqlfunc.count(sqlfunc.distinct(CalculationResult.shipping_date))
    ).filter(
        CalculationResult.customer_id == customer_id,
        CalculationResult.delivery_mode == '공동배송',
        CalculationResult.shipping_date.isnot(None),
    ).scalar() or 1

    plt_rows = session.query(
        CalculationResult.store_code,
        CalculationResult.destination,
        sqlfunc.sum(CalculationResult.total_plt_decimal).label('plt_sum'),
    ).filter(
        CalculationResult.customer_id == customer_id,
        CalculationResult.delivery_mode == '공동배송',
        CalculationResult.destination.isnot(None),
        CalculationResult.total_plt_decimal.isnot(None),
    ).group_by(CalculationResult.store_code, CalculationResult.destination).all()

    if not plt_rows:
        return 0

    caps        = {c.vehicle_type: c.max_plt for c in session.query(VehicleCapacity).all()}
    all_centers = {c.center_code: c for c in session.query(OurCenter).all()}
    all_coords  = {dc.destination: dc for dc in session.query(DestinationCoord).all()}

    center_by_name = {c.center_name: c.center_code for c in all_centers.values()}
    store_hub_map  = {}
    for s in session.query(StoreMaster).filter_by(customer_id=customer_id).all():
        if s.store_code and s.center_name and s.center_name in center_by_name:
            store_hub_map[s.store_code] = center_by_name[s.center_name]

    dzm_hub_map = {}
    for dz in session.query(DeliveryZoneMapping).all():
        norm = _norm_center_code(dz.center_code)
        if norm in all_centers:
            dzm_hub_map[(dz.sido, dz.sigungu)] = norm

    dist_rate_map = {}
    for dr in session.query(VehicleDistanceRate).all():
        dist_rate_map.setdefault(dr.vehicle_type, {})[dr.km] = dr.unit_price
    has_dist = bool(dist_rate_map)

    hub_fixed_map = {}
    for hvr in session.query(HubVehicleRate).all():
        hub_fixed_map.setdefault(hvr.center_code, []).append(hvr)

    # 거점별 → 목적지별 avg_plt 집계 (지도와 동일: 목적지=zone 단위)
    hub_dest_plt = {}  # norm_hub → {destination: total_plt_sum}
    for row in plt_rows:
        dest = row.destination or ''
        hub_code = store_hub_map.get(row.store_code or '')
        if not hub_code and dest:
            parts = dest.split()
            if len(parts) >= 2:
                hub_code = dzm_hub_map.get((parts[0], parts[1]))
        if not hub_code:
            continue
        norm_hub = _norm_center_code(hub_code)
        hub_dest_plt.setdefault(norm_hub, {})
        hub_dest_plt[norm_hub][dest] = hub_dest_plt[norm_hub].get(dest, 0.0) + float(row.plt_sum or 0)

    total_hub = 0

    for norm_hub, dest_plt in hub_dest_plt.items():
        hc_obj = all_centers.get(norm_hub)

        # 좌표 있는 zones (지도: filter z.lat && z.lon)
        zones = []
        fallback_zones = []
        for dest, plt_sum in dest_plt.items():
            avg_plt = plt_sum / joint_biz_days
            coord   = all_coords.get(dest)
            if hc_obj and hc_obj.lat and coord:
                zones.append({'avg_plt': avg_plt, 'lat': coord.lat, 'lon': coord.lon, 'dest': dest})
            else:
                fallback_zones.append({'avg_plt': avg_plt, 'dest': dest})

        if zones and has_dist and hc_obj and hc_obj.lat:
            hub_lat = float(hc_obj.lat)
            hub_lon = float(hc_obj.lon)

            # planRoutes: 각도 기준 정렬 (JS: atan2(z.lon - hub.lon, z.lat - hub.lat))
            sorted_zones = sorted(
                zones,
                key=lambda z: math.atan2(z['lon'] - hub_lon, z['lat'] - hub_lat)
            )

            num_routes = math.ceil(len(sorted_zones) / stops_per_vehicle)
            for v in range(num_routes):
                batch = sorted_zones[v * stops_per_vehicle:(v + 1) * stops_per_vehicle]

                # NN-TSP (JS: nnRoute)
                unvisited = list(batch)
                route = []
                cur_lat, cur_lon = hub_lat, hub_lon
                while unvisited:
                    bi, bd = 0, float('inf')
                    for i, z in enumerate(unvisited):
                        d = _haversine_km(cur_lat, cur_lon, z['lat'], z['lon'])
                        if d < bd:
                            bd, bi = d, i
                    stop = unvisited.pop(bi)
                    route.append(stop)
                    cur_lat, cur_lon = stop['lat'], stop['lon']

                # routeTotalKm: 누적 직선거리 × 1.3, 정수 반올림 (roundtrip=false)
                km = 0.0
                cur_lat, cur_lon = hub_lat, hub_lon
                for stop in route:
                    km += _haversine_km(cur_lat, cur_lon, stop['lat'], stop['lon'])
                    cur_lat, cur_lon = stop['lat'], stop['lon']
                road_km = max(1, min(1000, round(km * 1.3)))

                # route PLT = 루트 내 모든 stops avg_plt 합산
                route_plt = sum(z['avg_plt'] for z in route)

                # 차종 선택: route_plt를 수용할 수 있는 최소 차종
                sel_vt, sel_cap = _HUB_PLT_CAP[-1][1], _HUB_PLT_CAP[-1][0]
                for max_p, vt in _HUB_PLT_CAP:
                    if max_p >= route_plt:
                        sel_vt, sel_cap = vt, max_p
                        break

                up = (dist_rate_map.get(sel_vt) or {}).get(road_km)
                if up:
                    # 비례 비용 (JS: round(unitPrice * (totalPlt / selCap)))
                    total_hub += round(up * (route_plt / sel_cap))

        # 좌표 없는 zones: 고정단가 폴백 (개별 처리)
        for fz in fallback_zones:
            avg_plt = fz['avg_plt']
            hvrs    = hub_fixed_map.get(norm_hub, [])
            best_hc = None
            for hvr in hvrs:
                mp   = caps.get(hvr.vehicle_type, 1.0)
                cost = hvr.unit_price * (avg_plt / mp)
                if best_hc is None or cost < best_hc:
                    best_hc = cost
            if best_hc is not None:
                total_hub += round(best_hc)

    return total_hub


def compute_joint_breakdown_detail(customer_id, main_center_code, stops_per_vehicle, session):
    """
    거점별 이고비/변동용차비 상세 내역.
    Returns: {'transfer': [...], 'hub_vehicle': [...]}
    각 항목: hub_code, hub_name, total_plt, total_trucks, total_cost (이고)
             hub_code, hub_name, total_plt, total_stores, total_routes, total_cost (변동용차)
    """
    import collections
    from models import CalculationResult, DeliveryZoneMapping

    # ── 프리로드 (compute_joint_breakdown_live와 동일) ─────────────────────────
    caps        = {c.vehicle_type: c.max_plt for c in session.query(VehicleCapacity).all()}
    all_centers = {c.center_code: c for c in session.query(OurCenter).all()}
    all_coords  = {dc.destination: dc for dc in session.query(DestinationCoord).all()}

    center_by_name = {c.center_name: c.center_code for c in all_centers.values()}
    store_hub_map  = {}
    for s in session.query(StoreMaster).filter_by(customer_id=customer_id).all():
        if s.store_code and s.center_name and s.center_name in center_by_name:
            store_hub_map[s.store_code] = center_by_name[s.center_name]

    dzm_hub_map = {}
    for dz in session.query(DeliveryZoneMapping).all():
        norm = _norm_center_code(dz.center_code)
        if norm in all_centers:
            dzm_hub_map[(dz.sido, dz.sigungu)] = norm

    transfer_rate_map = {}
    for tr in session.query(TransferRate).all():
        nf = _norm_center_code(tr.from_center_code)
        nt = _norm_center_code(tr.to_center_code)
        transfer_rate_map.setdefault((nf, nt), []).append(tr)

    main_codes_in_tr = {_norm_center_code(k[0]) for k in transfer_rate_map}

    dist_rate_map = {}
    for dr in session.query(VehicleDistanceRate).all():
        dist_rate_map.setdefault(dr.vehicle_type, {})[dr.km] = dr.unit_price
    has_dist = bool(dist_rate_map)

    hub_fixed_map = {}
    for hvr in session.query(HubVehicleRate).all():
        hub_fixed_map.setdefault(hvr.center_code, []).append(hvr)

    joint_rows = session.query(CalculationResult).filter(
        CalculationResult.customer_id == customer_id,
        CalculationResult.delivery_mode == '공동배송',
        CalculationResult.total_plt_decimal.isnot(None),
    ).all()

    # ── (거점, 배송일) 기준으로 그루핑 ────────────────────────────────────────
    day_groups = collections.defaultdict(list)  # (hub_code, date) → [row, ...]
    for row in joint_rows:
        hub_code = store_hub_map.get(row.store_code or '')
        if not hub_code and row.destination:
            parts = row.destination.split()
            if len(parts) >= 2:
                hub_code = dzm_hub_map.get((parts[0], parts[1]))
        if hub_code:
            day_groups[(hub_code, row.shipping_date)].append(row)

    # ── 거점별 집계 ──────────────────────────────────────────────────────────
    tr_hub  = collections.defaultdict(lambda: {'total_plt': 0.0, 'total_trucks': 0, 'total_cost': 0})
    hub_hub = collections.defaultdict(lambda: {'total_plt': 0.0, 'total_stores': 0,
                                                'total_routes': 0, 'total_trucks': 0, 'total_cost': 0})

    norm_main = _norm_center_code(main_center_code) if main_center_code else None

    for (hub_code, date), rows in day_groups.items():
        norm_hub  = _norm_center_code(hub_code)
        daily_plt = sum(r.total_plt_decimal or 0 for r in rows)

        # ── 이고비: 일별 PLT 합산 → 트럭 대수 ────────────────────────────
        def _best_tr_for_plt(from_c, to_c, plt):
            trs = transfer_rate_map.get((from_c, to_c), [])
            best_cost, best_mp, best_vt = None, 1.0, ''
            for tr in trs:
                mp = caps.get(tr.vehicle_type, 1.0)
                cost = tr.unit_price * (plt / mp)  # 항상 비례
                if best_cost is None or cost < best_cost:
                    best_cost, best_mp, best_vt = cost, mp, tr.vehicle_type
            return best_cost, best_mp, best_vt

        if norm_main and norm_main != norm_hub:
            daily_tc, mp, _ = _best_tr_for_plt(norm_main, norm_hub, daily_plt)
        else:
            daily_tc, mp = None, 1.0
            for mc in main_codes_in_tr:
                if mc != norm_hub:
                    c, m, _ = _best_tr_for_plt(mc, norm_hub, daily_plt)
                    if c is not None and (daily_tc is None or c < daily_tc):
                        daily_tc, mp = c, m

        if daily_tc is not None:
            daily_trucks = round(daily_plt / mp, 2) if mp > 0 else 0  # 비례 배차율
            tr_hub[hub_code]['total_plt']    += daily_plt
            tr_hub[hub_code]['total_trucks'] += daily_trucks
            tr_hub[hub_code]['total_cost']   += round(daily_tc)

        # ── 변동용차비: 일별 점포수 → 루트 수 → 비용 ─────────────────────
        daily_stores = len(rows)
        daily_routes = math.ceil(daily_stores / stops_per_vehicle)
        hc_obj = all_centers.get(norm_hub)
        daily_hub_cost = 0
        for row in rows:
            dest = row.destination or ''
            if hc_obj and hc_obj.lat and dest:
                coord = all_coords.get(dest)
                if coord:
                    sk = _haversine_km(hc_obj.lat, hc_obj.lon, coord.lat, coord.lon)
                    road_km = min(1000, max(1, round(sk * 1.3)))
                    plt_dec = row.total_plt_decimal or 0
                    route_plt = plt_dec * stops_per_vehicle
                    vtype, max_plt = _hub_vehicle_type_for_plt(route_plt)
                    if has_dist:
                        up = (dist_rate_map.get(vtype) or {}).get(road_km)
                        if up:
                            daily_hub_cost += round(up * (route_plt / max_plt) / stops_per_vehicle)
                            continue
            # 고정단가 폴백: 최저 비용 차량 선택
            hvrs = hub_fixed_map.get(norm_hub, [])
            best_hc2 = None
            for hvr in hvrs:
                mp2    = caps.get(hvr.vehicle_type, 1.0)
                plt_d2 = row.total_plt_decimal or 0
                cost   = hvr.unit_price * (plt_d2 / mp2)
                if best_hc2 is None or cost < best_hc2:
                    best_hc2 = cost
            if best_hc2 is not None:
                daily_hub_cost += round(best_hc2)

        hub_hub[hub_code]['total_plt']    += daily_plt
        hub_hub[hub_code]['total_stores'] += daily_stores
        hub_hub[hub_code]['total_routes'] += daily_routes
        hub_hub[hub_code]['total_trucks'] += daily_routes
        hub_hub[hub_code]['total_cost']   += daily_hub_cost

    def _cname(code):
        c = all_centers.get(_norm_center_code(code))
        return c.center_name if c else code

    transfer_list = sorted([
        {
            'hub_code':     k,
            'hub_name':     _cname(k),
            'total_plt':    round(v['total_plt'], 1),
            'total_trucks': v['total_trucks'],
            'total_cost':   v['total_cost'],
        }
        for k, v in tr_hub.items()
    ], key=lambda x: -x['total_cost'])

    hub_list = sorted([
        {
            'hub_code':     k,
            'hub_name':     _cname(k),
            'total_plt':    round(v['total_plt'], 1),
            'total_stores': v['total_stores'],
            'total_routes': v['total_routes'],
            'total_trucks': v['total_trucks'],
            'total_cost':   v['total_cost'],
        }
        for k, v in hub_hub.items()
    ], key=lambda x: -x['total_cost'])

    return {'transfer': transfer_list, 'hub_vehicle': hub_list}
