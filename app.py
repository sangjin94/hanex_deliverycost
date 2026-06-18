import os
import uuid
import math
import pandas as pd
from io import BytesIO
from datetime import datetime
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file
)
from models import (
    db, Customer, VehicleRate, VehicleCapacity, SurchargeRule,
    JointDeliveryRate, SystemConfig, ProductMaster, StoreMaster,
    ShippingHistory, CalculationResult, OurCenter,
    TransferRate, HubVehicleRate
)
from calculator import (
    calculate_from_history, summarize_results,
    extract_sido_sigungu, make_destination_key, normalize_sido
)

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///delivery_pricing.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'hanex-delivery-2024'
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
db.init_app(app)

with app.app_context():
    db.create_all()
    # 기존 DB에 is_main_center 컬럼 추가 (없는 경우에만)
    with db.engine.connect() as _conn:
        try:
            _conn.execute(db.text("ALTER TABLE our_center ADD COLUMN is_main_center BOOLEAN DEFAULT 0"))
            _conn.commit()
        except Exception:
            pass

# 기본 차량 적재량 (없으면 자동 삽입)
DEFAULT_CAPACITIES = [
    ('11톤',   18, 0),
    ('5톤장축', 12, 1),
    ('5톤',    10, 2),
    ('3.5톤',   4, 3),
    ('2.5톤',   3, 4),
    ('1.4톤',   2, 5),
    ('1톤',     1, 6),
    ('퀵',    0.5, 7),
]

DEFAULT_CONFIGS = [
    ('direct_plt_threshold', '3', '직송 전환 기준 PLT 수 (이 값 이상이면 직송)'),
]

with app.app_context():
    if VehicleCapacity.query.count() == 0:
        for vt, mp, so in DEFAULT_CAPACITIES:
            db.session.add(VehicleCapacity(vehicle_type=vt, max_plt=mp, sort_order=so))
    for key, val, desc in DEFAULT_CONFIGS:
        if not SystemConfig.query.filter_by(key=key).first():
            db.session.add(SystemConfig(key=key, value=val, description=desc))
    db.session.commit()


# ─── 화주 현황 ─────────────────────────────────────────────────────────────────

@app.route('/')
def analytics():
    from sqlalchemy import func as sqlfunc

    customers = Customer.query.order_by(Customer.name).all()
    cust_map  = {c.id: c.name for c in customers}

    rows = db.session.query(
        CalculationResult.customer_id,
        sqlfunc.count(CalculationResult.id).label('cnt'),
        sqlfunc.sum(CalculationResult.total_box_qty).label('boxes'),
        sqlfunc.sum(CalculationResult.total_plt_decimal).label('plt'),
        sqlfunc.count(sqlfunc.distinct(CalculationResult.shipping_date)).label('days'),
        sqlfunc.avg(CalculationResult.cost_per_box).label('avg_cpb'),
        sqlfunc.sum(db.case((CalculationResult.delivery_mode == '직송', 1), else_=0)).label('direct_cnt'),
        sqlfunc.max(CalculationResult.calc_date).label('last_calc'),
    ).filter(CalculationResult.cost_per_box.isnot(None)).group_by(
        CalculationResult.customer_id
    ).all()

    customer_stats = sorted([{
        'id':             r.customer_id,
        'name':           cust_map.get(r.customer_id, f'고객#{r.customer_id}'),
        'cnt':            r.cnt,
        'boxes':          int(r.boxes or 0),
        'avg_cpb':        round(float(r.avg_cpb or 0), 1),
        'direct_pct':     round(int(r.direct_cnt or 0) / r.cnt * 100) if r.cnt else 0,
        'last_calc':      r.last_calc,
        'daily_avg_boxes': round(int(r.boxes or 0) / max(1, int(r.days or 1))),
        'daily_avg_plt':  round(float(r.plt or 0) / max(1, int(r.days or 1)), 1),
    } for r in rows], key=lambda x: x['name'])

    return render_template('analytics.html',
        customers=customers,
        customer_stats=customer_stats,
    )


@app.route('/analytics/<int:customer_id>')
def analytics_detail(customer_id):
    from sqlalchemy import func as sqlfunc
    import json

    customer = Customer.query.get_or_404(customer_id)
    f = CalculationResult.customer_id == customer_id

    total_results = CalculationResult.query.filter(f).count()

    overall_avg_cpb = round(float(db.session.query(
        sqlfunc.avg(CalculationResult.cost_per_box)
    ).filter(f, CalculationResult.cost_per_box.isnot(None)).scalar() or 0), 1)

    mode_rows = db.session.query(
        CalculationResult.delivery_mode,
        sqlfunc.count(CalculationResult.id).label('cnt'),
        sqlfunc.sum(CalculationResult.total_box_qty).label('boxes'),
    ).filter(f).group_by(CalculationResult.delivery_mode).all()
    direct_cnt   = next((r.cnt for r in mode_rows if r.delivery_mode == '직송'), 0)
    joint_cnt    = next((r.cnt for r in mode_rows if r.delivery_mode == '공동배송'), 0)
    direct_boxes = int(next((r.boxes for r in mode_rows if r.delivery_mode == '직송'), 0) or 0)
    joint_boxes  = int(next((r.boxes for r in mode_rows if r.delivery_mode == '공동배송'), 0) or 0)
    direct_pct   = round(direct_cnt / (direct_cnt + joint_cnt) * 100) if (direct_cnt + joint_cnt) else 0

    total_boxes = int(db.session.query(
        sqlfunc.sum(CalculationResult.total_box_qty)
    ).filter(f).scalar() or 0)

    total_plt = round(float(db.session.query(
        sqlfunc.sum(CalculationResult.total_plt_decimal)
    ).filter(f).scalar() or 0), 1)

    daily_rows = db.session.query(
        CalculationResult.shipping_date,
        sqlfunc.sum(CalculationResult.total_box_qty).label('boxes'),
        sqlfunc.sum(CalculationResult.total_plt_decimal).label('plt'),
    ).filter(f, CalculationResult.shipping_date.isnot(None)).group_by(
        CalculationResult.shipping_date
    ).order_by(CalculationResult.shipping_date).all()

    weekday_rows = db.session.query(
        db.func.strftime('%w', CalculationResult.shipping_date).label('wd'),
        sqlfunc.sum(CalculationResult.total_box_qty).label('boxes'),
        sqlfunc.sum(CalculationResult.total_plt_decimal).label('plt'),
    ).filter(f, CalculationResult.shipping_date.isnot(None)).group_by('wd').all()
    wd_boxes = [0] * 7
    wd_plt   = [0.0] * 7
    for r in weekday_rows:
        idx = int(r.wd)
        wd_boxes[idx] = round(float(r.boxes or 0))
        wd_plt[idx]   = round(float(r.plt or 0), 1)

    # 요일별 출현 날짜 수 (하루 평균 산출용)
    wd_daycount_rows = db.session.query(
        db.func.strftime('%w', CalculationResult.shipping_date).label('wd'),
        sqlfunc.count(db.func.distinct(CalculationResult.shipping_date)).label('day_cnt'),
    ).filter(f, CalculationResult.shipping_date.isnot(None)).group_by('wd').all()
    wd_count = [1] * 7
    for r in wd_daycount_rows:
        wd_count[int(r.wd)] = max(1, int(r.day_cnt or 1))

    monthly_rows = db.session.query(
        db.func.strftime('%Y-%m', CalculationResult.shipping_date).label('ym'),
        sqlfunc.sum(CalculationResult.total_box_qty).label('boxes'),
        sqlfunc.sum(CalculationResult.total_plt_decimal).label('plt'),
    ).filter(f, CalculationResult.shipping_date.isnot(None)).group_by('ym').order_by('ym').all()

    VT_ORDER = ['11톤', '5톤장축', '5톤', '3.5톤', '2.5톤', '1.4톤', '1톤', '퀵']
    veh_raw = db.session.query(
        CalculationResult.vehicle_type,
        sqlfunc.count(CalculationResult.id).label('cnt')
    ).filter(f, CalculationResult.vehicle_type.isnot(None)).group_by(
        CalculationResult.vehicle_type
    ).all()
    veh_dict   = {r.vehicle_type: r.cnt for r in veh_raw}
    veh_sorted = [(vt, veh_dict[vt]) for vt in VT_ORDER if vt in veh_dict]
    veh_sorted += [(vt, cnt) for vt, cnt in veh_dict.items() if vt not in VT_ORDER]

    total_distinct_days = len(daily_rows)
    daily_avg_boxes = round(total_boxes / total_distinct_days) if total_distinct_days > 0 else 0
    daily_avg_plt   = round(total_plt   / total_distinct_days, 1) if total_distinct_days > 0 else 0

    MON_FIRST = [1, 2, 3, 4, 5, 6, 0]
    chart_daily    = {
        'labels': [str(r.shipping_date) for r in daily_rows],
        'boxes':  [round(float(r.boxes or 0)) for r in daily_rows],
        'plt':    [round(float(r.plt or 0), 1) for r in daily_rows],
    }
    chart_weekday  = {
        'labels':    ['월', '화', '수', '목', '금', '토', '일'],
        'boxes':     [wd_boxes[i] for i in MON_FIRST],
        'plt':       [wd_plt[i]   for i in MON_FIRST],
        'avg_boxes': [round(wd_boxes[i] / wd_count[i]) for i in MON_FIRST],
        'avg_plt':   [round(wd_plt[i]   / wd_count[i], 1) for i in MON_FIRST],
    }
    chart_monthly  = {
        'labels': [r.ym for r in monthly_rows],
        'boxes':  [round(float(r.boxes or 0)) for r in monthly_rows],
        'plt':    [round(float(r.plt or 0), 1) for r in monthly_rows],
    }
    chart_mode = {
        'labels': ['직송', '공동배송'],
        'cnt':    [direct_cnt, joint_cnt],
        'boxes':  [direct_boxes, joint_boxes],
    }
    chart_veh = {
        'labels': [vt for vt, _ in veh_sorted],
        'data':   [cnt for _, cnt in veh_sorted],
    }

    return render_template('analytics_detail.html',
        customer=customer,
        total_results=total_results,
        total_boxes=total_boxes,
        total_plt=total_plt,
        daily_avg_boxes=daily_avg_boxes,
        daily_avg_plt=daily_avg_plt,
        overall_avg_cpb=overall_avg_cpb,
        direct_cnt=direct_cnt,
        joint_cnt=joint_cnt,
        direct_boxes=direct_boxes,
        joint_boxes=joint_boxes,
        direct_pct=direct_pct,
        chart_daily=json.dumps(chart_daily, ensure_ascii=False),
        chart_weekday=json.dumps(chart_weekday, ensure_ascii=False),
        chart_monthly=json.dumps(chart_monthly, ensure_ascii=False),
        chart_mode=json.dumps(chart_mode, ensure_ascii=False),
        chart_veh=json.dumps(chart_veh, ensure_ascii=False),
    )


@app.route('/analytics/<int:customer_id>/delete', methods=['POST'])
def analytics_delete(customer_id):
    if request.form.get('confirm_text') != '삭제':
        flash('삭제 확인 텍스트가 올바르지 않습니다.', 'danger')
        return redirect(url_for('analytics'))
    CalculationResult.query.filter_by(customer_id=customer_id).delete()
    db.session.commit()
    flash('산정 이력이 삭제되었습니다.', 'success')
    return redirect(url_for('analytics'))


# ─── 고객사 ────────────────────────────────────────────────────────────────────

@app.route('/customers')
def customer_list():
    customers = Customer.query.order_by(Customer.created_at.desc()).all()
    return render_template('customers/list.html', customers=customers)


@app.route('/customers/new', methods=['GET', 'POST'])
def customer_new():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        memo = request.form.get('memo', '').strip()
        if not name:
            flash('고객사명을 입력해주세요.', 'danger')
            return render_template('customers/form.html')
        c = Customer(name=name, memo=memo)
        db.session.add(c)
        db.session.commit()
        flash(f'고객사 [{name}]이(가) 등록되었습니다.', 'success')
        return redirect(url_for('customer_detail', cid=c.id))
    return render_template('customers/form.html')


@app.route('/customers/<int:cid>')
def customer_detail(cid):
    customer = Customer.query.get_or_404(cid)
    product_count = ProductMaster.query.filter_by(customer_id=cid).count()
    store_count = StoreMaster.query.filter_by(customer_id=cid).count()
    history_count = ShippingHistory.query.filter_by(customer_id=cid).count()
    result_count = CalculationResult.query.filter_by(customer_id=cid).count()
    return render_template('customers/detail.html',
                           customer=customer,
                           product_count=product_count,
                           store_count=store_count,
                           history_count=history_count,
                           result_count=result_count)


@app.route('/customers/<int:cid>/delete', methods=['POST'])
def customer_delete(cid):
    customer = Customer.query.get_or_404(cid)
    name = customer.name
    db.session.delete(customer)
    db.session.commit()
    flash(f'고객사 [{name}]이(가) 삭제되었습니다.', 'warning')
    return redirect(url_for('customer_list'))


# ─── 차량 단가 마스터 (센터별 매트릭스) ───────────────────────────────────────

@app.route('/masters/vehicle')
def vehicle_master():
    centers    = OurCenter.query.filter_by(is_main_center=True).order_by(OurCenter.sort_order).all()
    center_map = {c.center_code: c.center_name for c in centers}
    selected   = request.args.get('center', centers[0].center_code if centers else '')

    capacities   = VehicleCapacity.query.order_by(VehicleCapacity.sort_order).all()
    vehicle_types = [c.vehicle_type for c in capacities]

    # 선택된 센터의 매트릭스
    rates = VehicleRate.query.filter_by(center_code=selected).order_by(VehicleRate.destination).all()
    destinations = sorted(set(r.destination for r in rates))
    matrix = {}
    for r in rates:
        matrix.setdefault(r.destination, {})[r.vehicle_type] = r.unit_price

    # 센터별 도착지 수
    from sqlalchemy import func as sqlfunc
    stats_q = db.session.query(
        VehicleRate.center_code,
        sqlfunc.count(sqlfunc.distinct(VehicleRate.destination))
    ).group_by(VehicleRate.center_code).all()
    center_stats = {code: cnt for code, cnt in stats_q}

    return render_template('masters/vehicle.html',
                           centers=centers, center_map=center_map, selected=selected,
                           destinations=destinations, vehicle_types=vehicle_types,
                           matrix=matrix, capacities=capacities,
                           center_stats=center_stats)


@app.route('/masters/vehicle/save-matrix', methods=['POST'])
def vehicle_matrix_save():
    center_code = request.form.get('center_code', '').strip()
    dests  = request.form.getlist('dest')
    vts    = request.form.getlist('vt')
    prices = request.form.getlist('price')
    if not center_code:
        flash('센터를 선택해주세요.', 'danger')
        return redirect(url_for('vehicle_master'))
    saved = deleted = 0
    for dest, vt, p_str in zip(dests, vts, prices):
        p_str = p_str.replace(',', '').strip()
        existing = VehicleRate.query.filter_by(
            center_code=center_code, destination=dest, vehicle_type=vt
        ).first()
        if p_str and int(p_str) > 0:
            price = int(p_str)
            if existing:
                existing.unit_price = price
            else:
                db.session.add(VehicleRate(
                    center_code=center_code, destination=dest,
                    vehicle_type=vt, unit_price=price
                ))
            saved += 1
        elif existing:
            db.session.delete(existing)
            deleted += 1
    db.session.commit()
    flash(f'직송 단가 저장 — {saved}건 저장, {deleted}건 삭제', 'success')
    return redirect(url_for('vehicle_master', center=center_code))


@app.route('/masters/vehicle/upload', methods=['POST'])
def vehicle_master_upload():
    center_code = request.form.get('center_code', '').strip()
    if not center_code:
        flash('출발 센터를 선택해주세요.', 'danger')
        return redirect(url_for('vehicle_master'))
    file = request.files.get('file')
    if not file or not file.filename:
        flash('파일을 선택해주세요.', 'danger')
        return redirect(url_for('vehicle_master', center=center_code))
    try:
        df = pd.read_excel(file) if file.filename.endswith(('.xlsx', '.xls')) else pd.read_csv(file)
        df.columns = [str(c).strip() for c in df.columns]

        dest_col = df.columns[0]
        for col in df.columns:
            if any(kw in col for kw in ['도착', '출발', '지역', '도시']):
                dest_col = col
                break

        known_vehicles = ['11톤', '5톤장축', '5톤', '3.5톤', '2.5톤', '1.4톤', '1톤', '퀵']
        vehicle_cols = []
        for col in df.columns:
            clean = col.replace('.1', '').replace('.2', '').strip()
            if clean in known_vehicles and clean not in [v for _, v in vehicle_cols]:
                vehicle_cols.append((col, clean))

        if not vehicle_cols:
            flash('차량 종류 컬럼을 찾지 못했습니다. (11톤, 5톤, 1톤 등)', 'danger')
            return redirect(url_for('vehicle_master', center=center_code))

        overwrite = request.form.get('overwrite') == 'yes'
        if overwrite:
            VehicleRate.query.filter_by(center_code=center_code).delete()

        added = updated = 0
        for _, row in df.iterrows():
            dest_val = row.get(dest_col)
            if pd.isna(dest_val) or not str(dest_val).strip():
                continue
            dest = str(dest_val).strip()
            for raw_col, vtype in vehicle_cols:
                price_val = row.get(raw_col)
                if pd.isna(price_val):
                    continue
                try:
                    price = int(str(price_val).replace(',', '').replace(' ', ''))
                except ValueError:
                    continue
                existing = VehicleRate.query.filter_by(
                    center_code=center_code, destination=dest, vehicle_type=vtype
                ).first()
                if existing:
                    existing.unit_price = price
                    updated += 1
                else:
                    db.session.add(VehicleRate(
                        center_code=center_code, destination=dest,
                        vehicle_type=vtype, unit_price=price
                    ))
                    added += 1

        db.session.commit()
        center_name = OurCenter.query.filter_by(center_code=center_code).first()
        center_name = center_name.center_name if center_name else center_code
        flash(f'[{center_name}] 차량 단가 업로드 — {added}건 추가, {updated}건 수정', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'업로드 오류: {e}', 'danger')
    return redirect(url_for('vehicle_master', center=center_code))


@app.route('/masters/vehicle/add', methods=['POST'])
def vehicle_master_add():
    center_code  = request.form.get('center_code', '').strip()
    destination  = request.form.get('destination', '').strip()
    vehicle_type = request.form.get('vehicle_type', '').strip()
    price_str    = request.form.get('unit_price', '').replace(',', '').strip()
    if not all([center_code, destination, vehicle_type, price_str]):
        flash('모든 항목을 입력해주세요.', 'danger')
        return redirect(url_for('vehicle_master', center=center_code))
    try:
        price = int(price_str)
        existing = VehicleRate.query.filter_by(
            center_code=center_code, destination=destination, vehicle_type=vehicle_type
        ).first()
        if existing:
            existing.unit_price = price
            flash(f'단가 수정 완료', 'success')
        else:
            db.session.add(VehicleRate(
                center_code=center_code, destination=destination,
                vehicle_type=vehicle_type, unit_price=price
            ))
            flash(f'단가 추가 완료', 'success')
        db.session.commit()
    except Exception as e:
        flash(f'오류: {e}', 'danger')
    return redirect(url_for('vehicle_master', center=center_code))


@app.route('/masters/vehicle/<int:vid>/delete', methods=['POST'])
def vehicle_master_delete(vid):
    v = VehicleRate.query.get_or_404(vid)
    center_code = v.center_code
    db.session.delete(v)
    db.session.commit()
    flash('삭제되었습니다.', 'warning')
    return redirect(url_for('vehicle_master', center=center_code))


@app.route('/masters/vehicle/clear', methods=['POST'])
def vehicle_master_clear():
    center_code = request.form.get('center_code', '').strip()
    if center_code:
        cnt = VehicleRate.query.filter_by(center_code=center_code).delete()
        db.session.commit()
        flash(f'[{center_code}] 차량 단가 {cnt}건 초기화', 'warning')
    else:
        VehicleRate.query.delete()
        db.session.commit()
        flash('전체 차량 단가가 초기화되었습니다.', 'warning')
    return redirect(url_for('vehicle_master', center=center_code))


@app.route('/masters/vehicle/capacity/update', methods=['POST'])
def vehicle_capacity_update():
    caps = VehicleCapacity.query.all()
    for cap in caps:
        val = request.form.get(f'cap_{cap.id}')
        if val:
            try:
                cap.max_plt = float(val)
            except ValueError:
                pass
    db.session.commit()
    flash('차량 적재량이 업데이트되었습니다.', 'success')
    center = request.form.get('center_code', '')
    return redirect(url_for('vehicle_master', center=center))


@app.route('/masters/vehicle/template')
def vehicle_master_template():
    data = {
        '도착지':  ['강원도 강릉시', '강원도 춘천시', '경기도 가평군', '경기도 고양시'],
        '11톤':    [334000, 291000, 269000, 258000],
        '5톤장축': [289000, 251000, 235000, 220000],
        '5톤':     [253000, 219000, 205000, 196000],
        '3.5톤':   [220000, 194000, 183000, 174000],
        '2.5톤':   [201000, 181000, 172000, 165000],
        '1.4톤':   [162000, 145000, 139000, 132000],
        '1톤':     [154000, 136000, 130000, 125000],
        '퀵':      [149000, 129000, 124000, 118000],
    }
    df = pd.DataFrame(data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='차량단가마스터')
    output.seek(0)
    return send_file(output, download_name='차량단가마스터_템플릿.xlsx',
                     as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ─── 상품 마스터 ──────────────────────────────────────────────────────────────

@app.route('/customers/<int:cid>/products')
def product_master(cid):
    customer = Customer.query.get_or_404(cid)
    products = ProductMaster.query.filter_by(customer_id=cid).order_by(ProductMaster.product_code).all()
    return render_template('masters/product.html', customer=customer, products=products)


@app.route('/customers/<int:cid>/products/upload', methods=['POST'])
def product_upload(cid):
    Customer.query.get_or_404(cid)
    file = request.files.get('file')
    if not file or not file.filename:
        flash('파일을 선택해주세요.', 'danger')
        return redirect(url_for('product_master', cid=cid))
    try:
        df = pd.read_excel(file) if file.filename.endswith(('.xlsx', '.xls')) else pd.read_csv(file)
        df.columns = [str(c).strip() for c in df.columns]

        # 컬럼 매핑 (실제 Excel 헤더명 → 내부 컬럼명)
        col_map = {
            '고객사상품코드': 'product_code', '상품코드': 'product_code',
            '상품명': 'product_name',
            'BOX입수': 'box_in_count',
            '박스가로': 'box_width', '박스세로': 'box_depth', '박스높이': 'box_height',
            '박스중량': 'box_weight_kg',
            'PLT입수': 'plt_per_box',
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

        if 'product_code' not in df.columns:
            flash('상품코드 컬럼을 찾지 못했습니다. (고객사상품코드 또는 상품코드)', 'danger')
            return redirect(url_for('product_master', cid=cid))

        overwrite = request.form.get('overwrite') == 'yes'
        if overwrite:
            ProductMaster.query.filter_by(customer_id=cid).delete()

        added, updated = 0, 0
        for _, row in df.iterrows():
            code_val = row.get('product_code')
            if pd.isna(code_val):
                continue
            code = str(code_val).strip()

            def get_float(col):
                v = row.get(col)
                return float(v) if col in df.columns and not pd.isna(v) else None

            def get_int(col):
                v = row.get(col)
                return int(float(v)) if col in df.columns and not pd.isna(v) else None

            existing = ProductMaster.query.filter_by(customer_id=cid, product_code=code).first()
            kwargs = dict(
                product_name=str(row.get('product_name', code)).strip(),
                box_in_count=get_int('box_in_count'),
                box_width=get_float('box_width'),
                box_depth=get_float('box_depth'),
                box_height=get_float('box_height'),
                box_weight_kg=get_float('box_weight_kg'),
                plt_per_box=get_int('plt_per_box'),
            )
            if existing and not overwrite:
                for k, v in kwargs.items():
                    if v is not None:
                        setattr(existing, k, v)
                updated += 1
            else:
                db.session.add(ProductMaster(customer_id=cid, product_code=code, **kwargs))
                added += 1
        db.session.commit()
        flash(f'상품마스터: {added}건 추가, {updated}건 업데이트', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'업로드 오류: {e}', 'danger')
    return redirect(url_for('product_master', cid=cid))


@app.route('/customers/<int:cid>/products/template')
def product_template(cid):
    df = pd.DataFrame({
        '고객사상품코드': ['P001', 'P002'],
        '상품명': ['상품A', '상품B'],
        'BOX입수': [6, 4],
        '박스가로': [27.0, 30.0],
        '박스세로': [20.3, 22.0],
        '박스높이': [8.9, 10.0],
        '박스중량': [2.9, 4.5],
        'PLT입수': [300, 200],
    })
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='상품마스터')
    output.seek(0)
    return send_file(output, download_name='상품마스터_템플릿.xlsx', as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ─── 점포 마스터 ──────────────────────────────────────────────────────────────

@app.route('/customers/<int:cid>/stores')
def store_master(cid):
    customer = Customer.query.get_or_404(cid)
    stores = StoreMaster.query.filter_by(customer_id=cid).order_by(StoreMaster.sido, StoreMaster.store_code).all()
    unmapped = sum(1 for s in stores if not s.destination)
    return render_template('masters/store.html', customer=customer, stores=stores, unmapped=unmapped)


@app.route('/customers/<int:cid>/stores/upload', methods=['POST'])
def store_upload(cid):
    Customer.query.get_or_404(cid)
    file = request.files.get('file')
    if not file or not file.filename:
        flash('파일을 선택해주세요.', 'danger')
        return redirect(url_for('store_master', cid=cid))
    try:
        df = pd.read_excel(file) if file.filename.endswith(('.xlsx', '.xls')) else pd.read_csv(file)
        df.columns = [str(c).strip() for c in df.columns]

        col_map = {
            '배송처코드': 'store_code', '점포코드': 'store_code',
            '배송처명': 'store_name', '점포명': 'store_name', '센터': 'store_name',
            '주소': 'address',
            '시도': 'sido', '시군구': 'sigungu',
            '운영센터': 'center_name',
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

        # 배송처코드가 없으면 점포명으로 대체
        if 'store_code' not in df.columns and 'store_name' in df.columns:
            df['store_code'] = df['store_name']

        if 'store_code' not in df.columns:
            flash('배송처코드 또는 점포코드 컬럼이 필요합니다.', 'danger')
            return redirect(url_for('store_master', cid=cid))

        overwrite = request.form.get('overwrite') == 'yes'
        if overwrite:
            StoreMaster.query.filter_by(customer_id=cid).delete()

        added, updated = 0, 0
        for _, row in df.iterrows():
            code_val = row.get('store_code')
            if pd.isna(code_val):
                continue
            code = str(code_val).strip()

            address = str(row.get('address', '')).strip() if 'address' in df.columns and not pd.isna(row.get('address', float('nan'))) else None
            sido_val = str(row.get('sido', '')).strip() if 'sido' in df.columns and not pd.isna(row.get('sido', float('nan'))) else None
            sigungu_val = str(row.get('sigungu', '')).strip() if 'sigungu' in df.columns and not pd.isna(row.get('sigungu', float('nan'))) else None

            # 주소에서 시도+시군구 추출
            if address and (not sido_val or not sigungu_val):
                parsed_sido, parsed_sgg = extract_sido_sigungu(address)
                sido_val = sido_val or (normalize_sido(parsed_sido) if parsed_sido else None)
                sigungu_val = sigungu_val or parsed_sgg

            # 도착지 자동 매핑
            destination = None
            if sido_val and sigungu_val:
                dest_key = make_destination_key(normalize_sido(sido_val), sigungu_val)
                rate = VehicleRate.query.filter_by(destination=dest_key).first()
                if rate:
                    destination = dest_key
                else:
                    # 부분 매핑
                    rate = VehicleRate.query.filter(
                        VehicleRate.destination.like(f'%{sigungu_val}%')
                    ).first()
                    if rate:
                        destination = rate.destination

            existing = StoreMaster.query.filter_by(customer_id=cid, store_code=code).first()
            kwargs = dict(
                store_name=str(row.get('store_name', code)).strip() if 'store_name' in df.columns and not pd.isna(row.get('store_name', float('nan'))) else code,
                address=address,
                sido=normalize_sido(sido_val) if sido_val else None,
                sigungu=sigungu_val,
                destination=destination,
                center_name=str(row.get('center_name', '')).strip() if 'center_name' in df.columns and not pd.isna(row.get('center_name', float('nan'))) else None,
            )
            if existing and not overwrite:
                for k, v in kwargs.items():
                    if v is not None:
                        setattr(existing, k, v)
                updated += 1
            else:
                db.session.add(StoreMaster(customer_id=cid, store_code=code, **kwargs))
                added += 1
        db.session.commit()
        total = StoreMaster.query.filter_by(customer_id=cid).count()
        mapped = StoreMaster.query.filter(
            StoreMaster.customer_id == cid,
            StoreMaster.destination.isnot(None)
        ).count()
        flash(f'점포마스터: {added}건 추가, {updated}건 업데이트 | 도착지 자동매핑: {mapped}/{total}개', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'업로드 오류: {e}', 'danger')
    return redirect(url_for('store_master', cid=cid))


@app.route('/customers/<int:cid>/stores/template')
def store_template(cid):
    df = pd.DataFrame({
        '배송처코드': ['4630267', '1234567'],
        '배송처명': ['㈜이마트 보정몰센터', '㈜롯데마트 강릉점'],
        '주소': ['경기 용인시 기흥구 용구대로 2467', '강원도 강릉시 하슬라로 123'],
        '운영센터': ['이천센터', '이천센터'],
    })
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='점포마스터')
    output.seek(0)
    return send_file(output, download_name='점포마스터_템플릿.xlsx', as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ─── 출고내역 업로드 ──────────────────────────────────────────────────────────

@app.route('/customers/<int:cid>/calculate', methods=['GET'])
def calculate_page(cid):
    from calculator import get_direct_plt_threshold
    customer = Customer.query.get_or_404(cid)
    batches = db.session.query(
        ShippingHistory.batch_id,
        db.func.count(ShippingHistory.id).label('cnt'),
        db.func.min(ShippingHistory.uploaded_at).label('uploaded_at')
    ).filter_by(customer_id=cid).group_by(ShippingHistory.batch_id).order_by(
        db.func.min(ShippingHistory.uploaded_at).desc()
    ).all()
    threshold = get_direct_plt_threshold(db.session)
    centers = OurCenter.query.order_by(OurCenter.sort_order).all()
    return render_template('calculation/index.html',
                           customer=customer, batches=batches,
                           threshold=threshold, centers=centers)


@app.route('/customers/<int:cid>/history/<batch_id>/delete', methods=['POST'])
def history_delete(cid, batch_id):
    ShippingHistory.query.filter_by(customer_id=cid, batch_id=batch_id).delete()
    db.session.commit()
    flash('출고내역 배치가 삭제되었습니다.', 'warning')
    return redirect(url_for('calculate_page', cid=cid))


@app.route('/customers/<int:cid>/history/upload', methods=['POST'])
def history_upload(cid):
    Customer.query.get_or_404(cid)
    file = request.files.get('file')
    if not file or not file.filename:
        flash('파일을 선택해주세요.', 'danger')
        return redirect(url_for('calculate_page', cid=cid))
    try:
        df = pd.read_excel(file) if file.filename.endswith(('.xlsx', '.xls')) else pd.read_csv(file)
        df.columns = [str(c).strip() for c in df.columns]

        col_map = {
            '납품일자': 'shipping_date', '출고일': 'shipping_date', '일자': 'shipping_date',
            '주문번호': 'order_no', '오더유형': 'order_type', '채널': 'channel',
            '배송처코드': 'store_code', '점포코드': 'store_code', '거래처코드': 'store_code',
            '배송처명': 'store_name', '점포명': 'store_name', '거래처명': 'store_name',
            '주소': 'address', '배송주소': 'address',
            '상품코드': 'product_code', '품목코드': 'product_code',
            '상품명': 'product_name', '품목명': 'product_name',
            '출고수량(BOX)': 'box_qty', '출고수량': 'box_qty', '수량(BOX)': 'box_qty', '수량': 'box_qty', '박스수': 'box_qty',
            '출고수량(PLT)': 'plt_qty_decimal',  # PLT 컬럼명 고정: 반드시 이 이름만 인식
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

        # 필수 컬럼 확인 및 사용자에게 피드백
        has_store = 'store_code' in df.columns or 'store_name' in df.columns
        has_box = 'box_qty' in df.columns
        has_plt = 'plt_qty_decimal' in df.columns

        if not has_store:
            orig_cols = list(df.columns)
            flash(f'배송처 컬럼을 찾을 수 없습니다. 파일의 컬럼: {orig_cols[:10]}', 'danger')
            return redirect(url_for('calculate_page', cid=cid))
        if not has_box:
            flash('박스 수량 컬럼(출고수량(BOX))을 찾을 수 없습니다. 템플릿을 확인해주세요.', 'warning')
        if not has_plt:
            flash('출고수량(PLT) 컬럼이 없습니다. 상품마스터(PLT입수)로 자동 계산합니다.', 'info')

        # 헬퍼 함수 (루프 밖에 정의)
        def safe_str(row, col):
            v = row.get(col)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return None
            s = str(v).strip()
            return s if s and s.lower() != 'nan' else None

        def safe_float(row, col):
            v = row.get(col)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return None
            try:
                return float(v)
            except Exception:
                return None

        batch_id = str(uuid.uuid4())[:8]
        added = 0
        for _, row in df.iterrows():
            sc = safe_str(row, 'store_code')
            sn = safe_str(row, 'store_name')
            if not sc and not sn:
                continue

            ship_date = None
            if 'shipping_date' in df.columns:
                sd = row.get('shipping_date')
                if sd is not None and not (isinstance(sd, float) and math.isnan(sd)):
                    try:
                        ship_date = pd.to_datetime(sd).date()
                    except Exception:
                        pass

            plt_dec = safe_float(row, 'plt_qty_decimal')

            db.session.add(ShippingHistory(
                customer_id=cid, batch_id=batch_id,
                shipping_date=ship_date,
                order_no=safe_str(row, 'order_no'),
                order_type=safe_str(row, 'order_type'),
                channel=safe_str(row, 'channel'),
                store_code=sc, store_name=sn,
                address=safe_str(row, 'address'),
                product_code=safe_str(row, 'product_code'),
                product_name=safe_str(row, 'product_name'),
                box_qty=safe_float(row, 'box_qty'),
                plt_qty_decimal=plt_dec,
                plt_qty_int=None,
            ))
            added += 1
        db.session.commit()
        flash(f'✅ {added:,}건 출고내역 업로드 완료', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'업로드 오류: {e}', 'danger')
    return redirect(url_for('calculate_page', cid=cid))


@app.route('/customers/<int:cid>/history/template')
def history_template(cid):
    df = pd.DataFrame({
        '납품일자': ['2026-05-01', '2026-05-01'],
        '주문번호': ['F6486', 'F6487'],
        '오더유형': ['B2B', 'B2B'],
        '채널': ['할인점', '할인점'],
        '배송처코드': ['4630267', '4630267'],
        '배송처명': ['㈜이마트 보정몰센터', '㈜이마트 보정몰센터'],
        '주소': ['경기 용인시 기흥구 용구대로 2467', '경기 용인시 기흥구 용구대로 2467'],
        '상품코드': ['PG1011142056', 'PG1074021083'],
        '상품명': ['상품A', '상품B'],
        '출고수량(BOX)': [10, 5],
        '출고수량(PLT)': [0.5, 0.25],
    })
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='출고내역')
    output.seek(0)
    return send_file(output, download_name='출고내역_템플릿.xlsx', as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ─── 단가 산정 실행 ───────────────────────────────────────────────────────────

@app.route('/customers/<int:cid>/calculate/run', methods=['POST'])
def calculate_run(cid):
    customer = Customer.query.get_or_404(cid)
    batch_id          = request.form.get('batch_id')
    main_center_code  = request.form.get('main_center_code', '').strip()
    calc_name = request.form.get('calc_name', f'{customer.name} 단가산정 {datetime.now().strftime("%Y-%m-%d")}')

    if not main_center_code:
        flash('메인 센터를 선택해주세요.', 'danger')
        return redirect(url_for('calculate_page', cid=cid))

    query = ShippingHistory.query.filter_by(customer_id=cid)
    if batch_id:
        query = query.filter_by(batch_id=batch_id)
    history_rows = query.all()

    if not history_rows:
        flash('계산할 출고내역이 없습니다.', 'danger')
        return redirect(url_for('calculate_page', cid=cid))

    if not VehicleRate.query.filter_by(center_code=main_center_code).count():
        flash('선택한 센터의 차량 단가 마스터가 없습니다. 차량 단가를 먼저 등록해주세요.', 'danger')
        return redirect(url_for('calculate_page', cid=cid))

    results, errors = calculate_from_history(history_rows, cid, calc_name, main_center_code, db.session)

    result_batch = str(uuid.uuid4())[:8]
    for r in results:
        db.session.add(CalculationResult(
            customer_id=cid,
            calc_name=calc_name,
            batch_id=result_batch,
            shipping_date=r['shipping_date'],
            store_code=r['store_code'],
            store_name=r['store_name'],
            address=r['address'],
            destination=r['destination'],
            delivery_mode=r['delivery_mode'],
            total_box_qty=r['total_box_qty'],
            total_plt_decimal=r['total_plt_decimal'],
            total_plt_count=r['total_plt_count'],
            vehicle_type=r['vehicle_type'],
            delivery_cost=r['delivery_cost'],
            cost_per_box=r['cost_per_box'],
            memo=r['memo'],
        ))
    db.session.commit()

    summary = summarize_results(results)

    if errors:
        flash(f'{len(errors)}건 처리 이슈 (단가 미산출)', 'warning')

    return render_template('calculation/result.html',
                           customer=customer,
                           results=results,
                           summary=summary,
                           errors=errors,
                           calc_name=calc_name,
                           result_batch=result_batch)


# ─── 결과 조회 & 내보내기 ────────────────────────────────────────────────────────

@app.route('/customers/<int:cid>/results')
def result_list(cid):
    customer = Customer.query.get_or_404(cid)
    results = CalculationResult.query.filter_by(customer_id=cid).order_by(
        CalculationResult.calc_date.desc()
    ).all()
    batches = {}
    for r in results:
        bid = r.batch_id
        if bid not in batches:
            batches[bid] = {'name': r.calc_name, 'date': r.calc_date, 'rows': []}
        batches[bid]['rows'].append(r)
    return render_template('customers/results.html', customer=customer, batches=batches)


@app.route('/customers/<int:cid>/results/<batch_id>/export')
def result_export(cid, batch_id):
    customer = Customer.query.get_or_404(cid)
    results = CalculationResult.query.filter_by(customer_id=cid, batch_id=batch_id).all()
    if not results:
        flash('결과가 없습니다.', 'danger')
        return redirect(url_for('result_list', cid=cid))

    rows = [{
        '납품일자': r.shipping_date,
        '배송처코드': r.store_code,
        '배송처명': r.store_name,
        '주소': r.address,
        '도착지': r.destination,
        '배송모드': r.delivery_mode,
        '박스수': r.total_box_qty,
        'PLT수(소수)': r.total_plt_decimal,
        'PLT수(올림)': r.total_plt_count,
        '차량종류': r.vehicle_type,
        '배송비(원)': r.delivery_cost,
        '박스당배송비(원)': r.cost_per_box,
        '비고': r.memo,
    } for r in results]

    df = pd.DataFrame(rows)

    # 요약 행
    valid = [r for r in results if r.delivery_cost]
    total_cost = sum(r.delivery_cost for r in valid)
    total_boxes = sum(r.total_box_qty or 0 for r in valid)
    avg_cpb = round(total_cost / total_boxes, 1) if total_boxes > 0 else None
    df = pd.concat([df, pd.DataFrame([{
        '납품일자': '합계/평균', '배송처코드': '', '배송처명': '',
        '주소': '', '도착지': '', '배송모드': '',
        '박스수': total_boxes,
        'PLT수(소수)': sum(r.total_plt_decimal or 0 for r in valid),
        'PLT수(올림)': sum(r.total_plt_count or 0 for r in valid),
        '차량종류': '', '배송비(원)': total_cost, '박스당배송비(원)': avg_cpb, '비고': '',
    }])], ignore_index=True)

    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='단가산정결과')
    output.seek(0)
    filename = f'{customer.name}_배송단가_{datetime.now().strftime("%Y%m%d")}.xlsx'
    return send_file(output, download_name=filename, as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/customers/<int:cid>/results/<batch_id>/delete', methods=['POST'])
def result_delete(cid, batch_id):
    CalculationResult.query.filter_by(customer_id=cid, batch_id=batch_id).delete()
    db.session.commit()
    flash('결과가 삭제되었습니다.', 'warning')
    return redirect(url_for('result_list', cid=cid))


# ─── 시스템 설정 ──────────────────────────────────────────────────────────────

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    from calculator import get_direct_plt_threshold
    if request.method == 'POST':
        threshold = request.form.get('direct_plt_threshold', '').strip()
        try:
            val = float(threshold)
            cfg = SystemConfig.query.filter_by(key='direct_plt_threshold').first()
            if cfg:
                cfg.value = str(val)
            else:
                db.session.add(SystemConfig(
                    key='direct_plt_threshold', value=str(val),
                    description='직송 전환 기준 PLT 수'
                ))
            db.session.commit()
            flash(f'직송 기준이 PLT {val}개 이상으로 변경되었습니다.', 'success')
        except ValueError:
            flash('숫자를 입력해주세요.', 'danger')
        return redirect(url_for('settings'))

    current_threshold = get_direct_plt_threshold(db.session)
    return render_template('settings.html', threshold=current_threshold)


# ─── 공동배송 단가 관리 ────────────────────────────────────────────────────────

@app.route('/masters/joint')
def joint_master():
    from calculator import get_direct_plt_threshold
    rates = JointDeliveryRate.query.order_by(JointDeliveryRate.destination).all()
    destinations = db.session.query(VehicleRate.destination).distinct().order_by(VehicleRate.destination).all()
    destinations = ['기본'] + [d[0] for d in destinations]
    threshold = get_direct_plt_threshold(db.session)
    return render_template('masters/joint.html', rates=rates, destinations=destinations, threshold=threshold)


@app.route('/masters/joint/add', methods=['POST'])
def joint_rate_add():
    destination = request.form.get('destination', '').strip()
    price_str = request.form.get('price_per_box', '').replace(',', '').strip()
    memo = request.form.get('memo', '').strip()
    if not destination or not price_str:
        flash('도착지와 단가를 입력해주세요.', 'danger')
        return redirect(url_for('joint_master'))
    try:
        price = float(price_str)
        existing = JointDeliveryRate.query.filter_by(destination=destination).first()
        if existing:
            existing.price_per_box = price
            existing.memo = memo
            flash(f'[{destination}] 공동배송 단가가 업데이트되었습니다.', 'success')
        else:
            db.session.add(JointDeliveryRate(destination=destination, price_per_box=price, memo=memo))
            flash(f'[{destination}] 공동배송 단가가 추가되었습니다.', 'success')
        db.session.commit()
    except Exception as e:
        flash(f'오류: {e}', 'danger')
    return redirect(url_for('joint_master'))


@app.route('/masters/joint/<int:rid>/delete', methods=['POST'])
def joint_rate_delete(rid):
    r = JointDeliveryRate.query.get_or_404(rid)
    name = r.destination
    db.session.delete(r)
    db.session.commit()
    flash(f'[{name}] 단가가 삭제되었습니다.', 'warning')
    return redirect(url_for('joint_master'))


@app.route('/masters/joint/upload', methods=['POST'])
def joint_rate_upload():
    file = request.files.get('file')
    if not file or not file.filename:
        flash('파일을 선택해주세요.', 'danger')
        return redirect(url_for('joint_master'))
    try:
        df = pd.read_excel(file) if file.filename.endswith(('.xlsx', '.xls')) else pd.read_csv(file)
        df.columns = [str(c).strip() for c in df.columns]
        col_map = {'도착지': 'destination', '박스당단가': 'price_per_box', '단가': 'price_per_box', '메모': 'memo'}
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        if 'destination' not in df.columns or 'price_per_box' not in df.columns:
            flash('필수 컬럼: 도착지, 박스당단가', 'danger')
            return redirect(url_for('joint_master'))
        added, updated = 0, 0
        for _, row in df.iterrows():
            if pd.isna(row.get('destination')):
                continue
            dest = str(row['destination']).strip()
            price = float(row['price_per_box'])
            memo = str(row.get('memo', '')).strip() if 'memo' in df.columns and not pd.isna(row.get('memo')) else None
            existing = JointDeliveryRate.query.filter_by(destination=dest).first()
            if existing:
                existing.price_per_box = price
                existing.memo = memo
                updated += 1
            else:
                db.session.add(JointDeliveryRate(destination=dest, price_per_box=price, memo=memo))
                added += 1
        db.session.commit()
        flash(f'공동배송 단가: {added}건 추가, {updated}건 업데이트', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'업로드 오류: {e}', 'danger')
    return redirect(url_for('joint_master'))


# ─── 자사 센터 관리 ────────────────────────────────────────────────────────────

INITIAL_CENTERS = [
    # (코드, 명칭, 주소, 위도, 경도, 직송허브여부, 정렬순서)
    ('3300',    '인천센터',       '인천광역시 서구 북항로245번길 13-22',           37.50399, 126.62355, True,  0),
    ('3100',    '인천대포',       '인천광역시 서구 북항로 178번길 68-47',          37.51327, 126.63373, True,  1),
    ('3500',    '화성센터본점',   '경기도 화성시 양감면 초록로 103',               37.07786, 126.95455, True,  2),
    ('2000',    '이천센터',       '경기도 이천시 대월면 대월로 331',               37.18854, 127.49480, True,  3),
    ('2100',    '이천R2',         '경기도 이천시 대월면 대월로 331',               37.18854, 127.49480, True,  4),
    ('2200',    '이천대포',       '경기도 이천시 대월면 대월로932번길 63',         37.23642, 127.50253, True,  5),
    ('1100D',   '대월센터',       '경기도 이천시 대월면 대월로 627-61',            37.21011, 127.49390, True,  6),
    ('200',     '남이천1센터',    '경기도 이천시 모가면 공원로 112',               37.18209, 127.45063, True,  7),
    ('100',     '남이천2센터',    '경기도 이천시 모가면 공원로 134',               37.18194, 127.45288, True,  8),
    ('1100',    '동이천센터',     '경기도 이천시 부발읍 황무로 2037-57',           37.24314, 127.51581, True,  9),
    ('2700',    '북이천센터',     '경기도 이천시 부발읍 중부대로 2051번길 101',    37.29595, 127.52966, True, 10),
    ('5400',    '백암센터',       '경기도 용인시 처인구 백암면 덕평로 120',        37.18022, 127.37364, True, 11),
    ('7000',    '광주오포센터',   '경기도 광주시 오포로 614',                      37.35163, 127.21486, True, 12),
    ('9900',    '진천센터',       '충청북도 진천군 초평면 용정길 29-7',            36.84157, 127.51628, True, 13),
    ('0090000', '남청주센터',     '청주시 서원구 남이면 저산척북로 203-30',        36.56295, 127.38343, True, 14),
    ('5000',    '대구센터',       '경상북도 경산시 압량읍 가야로 20',              35.85336, 128.76886, True, 15),
    ('6000',    '김해센터',       '경상남도 김해시 상동면 상동로 680-70',          35.31140, 128.93961, True, 16),
]


def _seed_centers():
    if OurCenter.query.count() == 0:
        for row in INITIAL_CENTERS:
            code, name, addr, lat, lon, is_hub, sort = row
            db.session.add(OurCenter(
                center_code=code, center_name=name, address=addr,
                lat=lat, lon=lon, is_direct_hub=is_hub, sort_order=sort
            ))
        db.session.commit()


# ── 이고비용 기본 데이터 ──────────────────────────────────────────────────────
# 차량 1대 전체 운행 단가 (원). 실제 이고비 = unit_price × (실제PLT / 차량최대PLT)
# 거리 등급: 근(~70km) / 중(70~200km) / 원(200km+)
_TR_PRICES = {
    '근': {'11톤': 350000, '5톤장축': 250000, '5톤': 200000, '3.5톤': 120000, '2.5톤': 95000},
    '중': {'11톤': 550000, '5톤장축': 400000, '5톤': 320000, '3.5톤': 195000, '2.5톤': 160000},
    '원': {'11톤': 850000, '5톤장축': 630000, '5톤': 500000, '3.5톤': 310000, '2.5톤': 255000},
}
_TRANSFER_ROUTES = [
    # (출발, 도착, 거리등급)  —  실제 운영 노선 기준
    # 인천센터(3300) 출발
    ('3300', '3100',    '근'), ('3300', '3500',    '근'), ('3300', '2000',    '중'),
    ('3300', '7000',    '중'), ('3300', '5400',    '중'), ('3300', '9900',    '중'),
    ('3300', '0090000', '중'), ('3300', '5000',    '원'), ('3300', '6000',    '원'),
    # 이천센터(2000) 출발
    ('2000', '3300',    '중'), ('2000', '3500',    '중'), ('2000', '7000',    '근'),
    ('2000', '5400',    '근'), ('2000', '9900',    '중'), ('2000', '0090000', '중'),
    ('2000', '5000',    '원'), ('2000', '6000',    '원'),
    # 이천 내부 (대포·서브센터)
    ('2000', '2200',    '근'), ('2000', '1100D',   '근'), ('2000', '200',     '근'),
    ('2000', '100',     '근'), ('2000', '1100',    '근'), ('2000', '2700',    '근'),
    ('2000', '2100',    '근'),
    # 화성센터(3500) 출발
    ('3500', '3300',    '근'), ('3500', '2000',    '중'), ('3500', '0090000', '중'),
    # 광주오포(7000) 출발
    ('7000', '2000',    '근'), ('7000', '5400',    '근'),
    # 진천(9900), 남청주(0090000) 출발
    ('9900',    '0090000', '근'), ('9900', '5000', '중'), ('9900', '6000', '원'),
    ('0090000', '5000',    '중'), ('0090000', '6000', '원'),
    # 김해(6000) ↔ 대구(5000)
    ('6000', '5000', '중'), ('5000', '6000', '중'),
]

INITIAL_TRANSFER_RATES = []
for _from, _to, _tier in _TRANSFER_ROUTES:
    for _vt, _price in _TR_PRICES[_tier].items():
        INITIAL_TRANSFER_RATES.append((_from, _to, _vt, _price))


# ── 거점 변동용차 기본 데이터 ─────────────────────────────────────────────────
# 차량 1대 전체 운행 단가. 실제 비용 = unit_price × (실적PLT / 차량최대PLT)
# (센터코드, 차량종류, 기본단가) — 배송지구 구분 없이 거점센터 단위로 관리
INITIAL_HUB_VEHICLE_RATES = [
    # (센터코드, 차량종류, 1회단가) -- 기본값, 실제 운용 후 수정 필요
    ('3300', '1톤',    90000),  ('3300', '2.5톤', 165000), ('3300', '5톤',    310000),
    ('3100', '1톤',    90000),  ('3100', '2.5톤', 165000), ('3100', '5톤',    310000),
    ('3500', '1톤',    95000),  ('3500', '2.5톤', 175000), ('3500', '5톤',    330000),
    ('2000', '1톤',    85000),  ('2000', '2.5톤', 155000), ('2000', '5톤',    290000),
    ('2100', '1톤',    85000),  ('2100', '2.5톤', 155000), ('2100', '5톤',    290000),
    ('2200', '1톤',    85000),  ('2200', '2.5톤', 155000), ('2200', '5톤',    290000),
    ('1100D','1톤',    85000),  ('1100D','2.5톤', 155000), ('1100D','5톤',    290000),
    ('200',  '1톤',    80000),  ('200',  '2.5톤', 148000), ('200',  '5톤',    278000),
    ('100',  '1톤',    80000),  ('100',  '2.5톤', 148000), ('100',  '5톤',    278000),
    ('1100', '1톤',    85000),  ('1100', '2.5톤', 155000), ('1100', '5톤',    290000),
    ('2700', '1톤',    90000),  ('2700', '2.5톤', 165000), ('2700', '5톤',    310000),
    ('5400', '1톤',    90000),  ('5400', '2.5톤', 165000), ('5400', '5톤',    310000),
    ('7000', '1톤',    85000),  ('7000', '2.5톤', 155000), ('7000', '5톤',    290000),
    ('9900', '1톤',    80000),  ('9900', '2.5톤', 148000), ('9900', '5톤',    278000),
    ('0090000','1톤',  80000),  ('0090000','2.5톤',148000),('0090000','5톤',  278000),
    ('5000', '1톤',    85000),  ('5000', '2.5톤', 155000), ('5000', '5톤',    290000),
    ('6000', '1톤',    85000),  ('6000', '2.5톤', 155000), ('6000', '5톤',    290000),
]


def _seed_transfer_and_hub():
    if TransferRate.query.count() == 0:
        for from_c, to_c, vt, price in INITIAL_TRANSFER_RATES:
            db.session.add(TransferRate(
                from_center_code=from_c, to_center_code=to_c,
                vehicle_type=vt, unit_price=price
            ))
    if HubVehicleRate.query.count() == 0:
        for code, vtype, price in INITIAL_HUB_VEHICLE_RATES:
            db.session.add(HubVehicleRate(
                center_code=code, vehicle_type=vtype, unit_price=price
            ))
    db.session.commit()


with app.app_context():
    _seed_centers()
    db.create_all()
    _seed_transfer_and_hub()


@app.route('/centers')
def center_list():
    centers = OurCenter.query.order_by(OurCenter.sort_order, OurCenter.center_name).all()
    return render_template('centers/list.html', centers=centers)


@app.route('/centers/add', methods=['POST'])
def center_add():
    code = request.form.get('center_code', '').strip().upper()
    name = request.form.get('center_name', '').strip()
    addr = request.form.get('address', '').strip()
    lat_s = request.form.get('lat', '').strip()
    lon_s = request.form.get('lon', '').strip()
    is_hub = request.form.get('is_direct_hub') == 'on'
    memo = request.form.get('memo', '').strip()
    if not code or not name:
        flash('센터코드와 센터명은 필수입니다.', 'danger')
        return redirect(url_for('center_list'))
    try:
        lat = float(lat_s) if lat_s else None
        lon = float(lon_s) if lon_s else None
        existing = OurCenter.query.filter_by(center_code=code).first()
        if existing:
            existing.center_name = name
            existing.address = addr
            existing.lat = lat
            existing.lon = lon
            existing.is_direct_hub = is_hub
            existing.memo = memo
            flash(f'[{code}] {name} 업데이트되었습니다.', 'success')
        else:
            max_order = db.session.query(db.func.max(OurCenter.sort_order)).scalar() or 0
            db.session.add(OurCenter(
                center_code=code, center_name=name, address=addr,
                lat=lat, lon=lon, is_direct_hub=is_hub, memo=memo,
                sort_order=max_order + 1
            ))
            flash(f'[{code}] {name} 추가되었습니다.', 'success')
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        flash(f'오류: {e}', 'danger')
    return redirect(url_for('center_list'))


@app.route('/centers/<int:ctr_id>/delete', methods=['POST'])
def center_delete(ctr_id):
    center = OurCenter.query.get_or_404(ctr_id)
    name = center.center_name
    db.session.delete(center)
    db.session.commit()
    flash(f'[{name}] 센터가 삭제되었습니다.', 'warning')
    return redirect(url_for('center_list'))


@app.route('/centers/<int:ctr_id>/toggle-hub', methods=['POST'])
def center_toggle_hub(ctr_id):
    center = OurCenter.query.get_or_404(ctr_id)
    center.is_direct_hub = not center.is_direct_hub
    db.session.commit()
    status = '직송허브' if center.is_direct_hub else '거점전용'
    flash(f'[{center.center_name}] → {status}로 변경되었습니다.', 'success')
    return redirect(url_for('center_list'))


@app.route('/centers/<int:ctr_id>/toggle-main', methods=['POST'])
def center_toggle_main(ctr_id):
    center = OurCenter.query.get_or_404(ctr_id)
    center.is_main_center = not center.is_main_center
    db.session.commit()
    status = '메인센터' if center.is_main_center else '일반센터'
    flash(f'[{center.center_name}] → {status}로 변경되었습니다.', 'success')
    return redirect(url_for('center_list'))


# ─── 이고비용 마스터 (매트릭스 뷰) ─────────────────────────────────────────────

@app.route('/masters/transfer')
def transfer_master():
    rates = TransferRate.query.all()
    # matrix[from_code][to_code][vt] = unit_price
    matrix = {}
    for r in rates:
        matrix.setdefault(r.from_center_code, {}).setdefault(r.to_center_code, {})[r.vehicle_type] = r.unit_price
    main_centers = OurCenter.query.filter_by(is_main_center=True).order_by(OurCenter.sort_order).all()
    all_centers  = OurCenter.query.order_by(OurCenter.sort_order).all()
    vehicle_types = [c.vehicle_type for c in VehicleCapacity.query.order_by(VehicleCapacity.sort_order).all()]
    return render_template('masters/transfer.html',
                           main_centers=main_centers, centers=all_centers,
                           vehicle_types=vehicle_types, matrix=matrix)


@app.route('/masters/transfer/save-matrix', methods=['POST'])
def transfer_matrix_save():
    from_code = request.form.get('from_code', '').strip()
    tos    = request.form.getlist('to')
    vts    = request.form.getlist('vt')
    prices = request.form.getlist('price')
    if not from_code:
        flash('오류: 출발 센터 미지정', 'danger')
        return redirect(url_for('transfer_master'))
    saved = deleted = 0
    for to, vt, p_str in zip(tos, vts, prices):
        p_str = p_str.replace(',', '').strip()
        existing = TransferRate.query.filter_by(
            from_center_code=from_code, to_center_code=to, vehicle_type=vt
        ).first()
        if p_str and int(p_str) > 0:
            price = int(p_str)
            if existing:
                existing.unit_price = price
            else:
                db.session.add(TransferRate(
                    from_center_code=from_code, to_center_code=to,
                    vehicle_type=vt, unit_price=price
                ))
            saved += 1
        elif existing:
            db.session.delete(existing)
            deleted += 1
    db.session.commit()
    flash(f'이고비용 저장 완료 — {saved}건 저장, {deleted}건 삭제', 'success')
    return redirect(url_for('transfer_master'))


# ─── 거점 변동용차 비용 마스터 ────────────────────────────────────────────────

@app.route('/masters/hub-vehicle')
def hub_vehicle_master():
    centers       = OurCenter.query.order_by(OurCenter.sort_order).all()
    capacities    = VehicleCapacity.query.order_by(VehicleCapacity.sort_order).all()
    vehicle_types = [c.vehicle_type for c in capacities]

    rates = HubVehicleRate.query.all()
    # matrix[center_code][vt] = unit_price
    matrix = {}
    for r in rates:
        matrix.setdefault(r.center_code, {})[r.vehicle_type] = r.unit_price

    return render_template('masters/hub_vehicle.html',
                           centers=centers, vehicle_types=vehicle_types,
                           capacities=capacities, matrix=matrix)


@app.route('/masters/hub-vehicle/save', methods=['POST'])
def hub_vehicle_save():
    center_code = request.form.get('center_code', '').strip()
    vts    = request.form.getlist('vt')
    prices = request.form.getlist('price')
    if not center_code:
        flash('오류: 센터 미지정', 'danger')
        return redirect(url_for('hub_vehicle_master'))
    saved = deleted = 0
    for vt, p_str in zip(vts, prices):
        p_str = p_str.replace(',', '').strip()
        existing = HubVehicleRate.query.filter_by(
            center_code=center_code, vehicle_type=vt
        ).first()
        if p_str and int(p_str) > 0:
            price = int(p_str)
            if existing:
                existing.unit_price = price
            else:
                db.session.add(HubVehicleRate(
                    center_code=center_code, vehicle_type=vt, unit_price=price
                ))
            saved += 1
        elif existing:
            db.session.delete(existing)
            deleted += 1
    db.session.commit()
    flash(f'변동용차 단가 저장 완료 — {saved}건 저장, {deleted}건 삭제', 'success')
    return redirect(url_for('hub_vehicle_master'))


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
