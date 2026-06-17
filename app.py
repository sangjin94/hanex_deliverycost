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


# ─── 대시보드 ──────────────────────────────────────────────────────────────────

@app.route('/')
def dashboard():
    customers = Customer.query.order_by(Customer.created_at.desc()).all()
    vehicle_dest_count = db.session.query(VehicleRate.destination).distinct().count()
    center_count = OurCenter.query.count()
    recent_results = CalculationResult.query.order_by(
        CalculationResult.calc_date.desc()
    ).limit(10).all()
    return render_template('dashboard.html',
                           customers=customers,
                           vehicle_dest_count=vehicle_dest_count,
                           center_count=center_count,
                           recent_results=recent_results)


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


# ─── 차량 단가 마스터 ─────────────────────────────────────────────────────────

@app.route('/masters/vehicle')
def vehicle_master():
    # 도착지 목록
    destinations = db.session.query(VehicleRate.destination).distinct().order_by(VehicleRate.destination).all()
    destinations = [d[0] for d in destinations]
    capacities = VehicleCapacity.query.order_by(VehicleCapacity.sort_order).all()
    vehicle_types = [c.vehicle_type for c in capacities]

    # 매트릭스 구성 {도착지: {차량종류: 단가}}
    all_rates = VehicleRate.query.all()
    matrix = {}
    for r in all_rates:
        if r.destination not in matrix:
            matrix[r.destination] = {}
        matrix[r.destination][r.vehicle_type] = r.unit_price

    surcharges = SurchargeRule.query.order_by(SurchargeRule.surcharge_type).all()
    return render_template('masters/vehicle.html',
                           destinations=destinations,
                           vehicle_types=vehicle_types,
                           matrix=matrix,
                           capacities=capacities,
                           surcharges=surcharges)


@app.route('/masters/vehicle/upload', methods=['POST'])
def vehicle_master_upload():
    """
    차량마스터 Excel 업로드.
    형식: 도착지 | 11톤 | 5톤장축 | 5톤 | 3.5톤 | 2.5톤 | 1.4톤 | 1톤 | 퀵
    오른쪽 섹션(톤수+적재량)도 파싱.
    """
    file = request.files.get('file')
    if not file or not file.filename:
        flash('파일을 선택해주세요.', 'danger')
        return redirect(url_for('vehicle_master'))
    try:
        df = pd.read_excel(file) if file.filename.endswith(('.xlsx', '.xls')) else pd.read_csv(file)
        df.columns = [str(c).strip() for c in df.columns]

        # 차량 종류 컬럼 감지 (도착지/출발지 컬럼 제외)
        dest_col = None
        for col in df.columns:
            if '도착' in col or '출발' in col or '지역' in col or '도시' in col:
                dest_col = col
                break
        if dest_col is None:
            dest_col = df.columns[0]

        vehicle_cols = []
        capacity_map = {}   # 차량종류 → 적재량
        known_vehicles = ['11톤', '5톤장축', '5톤', '3.5톤', '2.5톤', '1.4톤', '1톤', '퀵']

        # 적재량 컬럼 파싱 (톤수, 적재량 컬럼)
        if '톤수' in df.columns and '적재량' in df.columns:
            for _, row in df.iterrows():
                tv = row.get('톤수')
                av = row.get('적재량')
                if pd.notna(tv) and pd.notna(av):
                    capacity_map[str(tv).strip()] = float(av)

        for col in df.columns:
            clean = col.replace('.1', '').replace('.2', '').strip()
            if clean in known_vehicles and col not in [dest_col]:
                # 중복 컬럼(.1 접미사)은 부가요금 섹션 → 제외
                # 첫 번째 등장만 단가로 사용
                if clean not in [v for _, v in vehicle_cols]:
                    vehicle_cols.append((col, clean))

        if not vehicle_cols:
            flash('차량 종류 컬럼을 찾지 못했습니다. (11톤, 5톤, 1톤 등)', 'danger')
            return redirect(url_for('vehicle_master'))

        overwrite = request.form.get('overwrite') == 'yes'
        if overwrite:
            VehicleRate.query.delete()

        added = 0
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
                    destination=dest, vehicle_type=vtype
                ).first()
                if existing:
                    existing.unit_price = price
                else:
                    db.session.add(VehicleRate(
                        destination=dest, vehicle_type=vtype, unit_price=price
                    ))
                    added += 1

        # 적재량 업데이트
        for vtype, max_plt in capacity_map.items():
            cap = VehicleCapacity.query.filter_by(vehicle_type=vtype).first()
            if cap:
                cap.max_plt = max_plt
            else:
                so = next((i for i, (t, _, _) in enumerate(
                    [('11톤', 18, 0), ('5톤장축', 12, 1), ('5톤', 10, 2),
                     ('3.5톤', 4, 3), ('2.5톤', 3, 4), ('1.4톤', 2, 5), ('1톤', 1, 6), ('퀵', 0.5, 7)]
                ) if t == vtype), 9)
                db.session.add(VehicleCapacity(vehicle_type=vtype, max_plt=max_plt, sort_order=so))

        db.session.commit()
        dest_count = db.session.query(VehicleRate.destination).distinct().count()
        flash(f'차량 단가 업로드 완료 — {dest_count}개 도착지, {added}건 추가', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'업로드 오류: {e}', 'danger')
    return redirect(url_for('vehicle_master'))


@app.route('/masters/vehicle/add', methods=['POST'])
def vehicle_master_add():
    try:
        destination = request.form['destination'].strip()
        vehicle_type = request.form['vehicle_type'].strip()
        unit_price = int(request.form['unit_price'].replace(',', ''))
        existing = VehicleRate.query.filter_by(destination=destination, vehicle_type=vehicle_type).first()
        if existing:
            existing.unit_price = unit_price
            flash(f'[{destination} / {vehicle_type}] 단가가 수정되었습니다.', 'success')
        else:
            db.session.add(VehicleRate(destination=destination, vehicle_type=vehicle_type, unit_price=unit_price))
            flash(f'[{destination} / {vehicle_type}] 단가가 추가되었습니다.', 'success')
        db.session.commit()
    except Exception as e:
        flash(f'오류: {e}', 'danger')
    return redirect(url_for('vehicle_master'))


@app.route('/masters/vehicle/<int:vid>/delete', methods=['POST'])
def vehicle_master_delete(vid):
    v = VehicleRate.query.get_or_404(vid)
    db.session.delete(v)
    db.session.commit()
    flash('삭제되었습니다.', 'warning')
    return redirect(url_for('vehicle_master'))


@app.route('/masters/vehicle/clear', methods=['POST'])
def vehicle_master_clear():
    VehicleRate.query.delete()
    db.session.commit()
    flash('차량 단가가 초기화되었습니다.', 'warning')
    return redirect(url_for('vehicle_master'))


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
    return redirect(url_for('vehicle_master'))


@app.route('/masters/vehicle/template')
def vehicle_master_template():
    data = {
        '도착지': ['강원도 강릉시', '강원도 춘천시', '경기도 가평군', '경기도 고양시'],
        '11톤':   [334000, 291000, 269000, 258000],
        '5톤장축': [289000, 251000, 235000, 220000],
        '5톤':    [253000, 219000, 205000, 196000],
        '3.5톤':  [220000, 194000, 183000, 174000],
        '2.5톤':  [201000, 181000, 172000, 165000],
        '1.4톤':  [162000, 145000, 139000, 132000],
        '1톤':    [154000, 136000, 130000, 125000],
        '퀵':     [149000, 129000, 124000, 118000],
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
    return render_template('calculation/index.html', customer=customer, batches=batches, threshold=threshold)


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
            '출고수량(PLT)': 'plt_qty_decimal', 'PLT수': 'plt_qty_decimal', 'PLT량': 'plt_qty_decimal',
            'PLT 환산': 'plt_qty_int', 'PLT환산': 'plt_qty_int', 'PLT(올림)': 'plt_qty_int',
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

        # 필수 컬럼 확인 및 사용자에게 피드백
        has_store = 'store_code' in df.columns or 'store_name' in df.columns
        has_box = 'box_qty' in df.columns
        has_plt = 'plt_qty_decimal' in df.columns or 'plt_qty_int' in df.columns

        if not has_store:
            orig_cols = list(df.columns)
            flash(f'배송처 컬럼을 찾을 수 없습니다. 파일의 컬럼: {orig_cols[:10]}', 'danger')
            return redirect(url_for('calculate_page', cid=cid))
        if not has_box:
            flash('박스 수량 컬럼(출고수량(BOX))을 찾을 수 없습니다. 템플릿을 확인해주세요.', 'warning')
        if not has_plt:
            flash('PLT 수량 컬럼이 없습니다. 상품마스터(PLT입수)로 자동 계산합니다.', 'info')

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
            plt_int_raw = safe_float(row, 'plt_qty_int')
            plt_int = int(math.ceil(plt_int_raw)) if plt_int_raw is not None else None

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
                plt_qty_int=plt_int,
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
        'PLT 환산': [1, 1],
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
    batch_id = request.form.get('batch_id')
    calc_name = request.form.get('calc_name', f'{customer.name} 단가산정 {datetime.now().strftime("%Y-%m-%d")}')

    query = ShippingHistory.query.filter_by(customer_id=cid)
    if batch_id:
        query = query.filter_by(batch_id=batch_id)
    history_rows = query.all()

    if not history_rows:
        flash('계산할 출고내역이 없습니다.', 'danger')
        return redirect(url_for('calculate_page', cid=cid))

    if not VehicleRate.query.count():
        flash('차량 단가 마스터가 없습니다. 먼저 차량 마스터를 등록해주세요.', 'danger')
        return redirect(url_for('calculate_page', cid=cid))

    results, errors = calculate_from_history(history_rows, cid, calc_name, db.session)

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
    ('ICN', '인천센터',      '인천광역시 서구 완정로 78',                   37.5308, 126.6989, True),
    ('KMP', '김포센터',      '경기도 김포시 고촌읍 아라육로 76번길 29',      37.5969, 126.7188, True),
    ('SWN', '수원센터',      '경기도 수원시 권선구 경수대로 1009',           37.2430, 126.9878, True),
    ('YGN', '용인센터',      '경기도 용인시 처인구 남사읍 완장리 296-7',     37.1610, 127.1860, True),
    ('ASN', '안성센터',      '경기도 안성시 공도읍 심교리 1-2',              37.0270, 127.2440, True),
    ('PLN', '평택센터',      '경기도 평택시 진위면 진위로 180',              37.0440, 127.0820, True),
    ('CHN', '천안센터',      '충청남도 천안시 서북구 직산읍 판정로 120번길 108', 36.8912, 127.1337, True),
    ('OJ',  '오창센터',      '충청북도 청주시 청원구 오창읍 과학산업1로 10', 36.7271, 127.4600, True),
    ('SEJ', '세종센터',      '세종특별자치시 연기면 봉기리 산 1-2',          36.5895, 127.2400, True),
    ('DJN', '대전센터',      '대전광역시 유성구 관평동 949-2',               36.4350, 127.4040, True),
    ('KSN', '광주(경기)센터', '경기도 광주시 초월읍 서하리 376',              37.3823, 127.3660, True),
    ('IHC', '이천센터',      '경기도 이천시 호법면 중산리 175-1',            37.2020, 127.4290, True),
    ('WJU', '원주센터',      '강원도 원주시 지정면 판대리 210-2',            37.3854, 127.8680, True),
    ('GWN', '강원센터',      '강원도 춘천시 동내면 고은리 12-14',            37.8225, 127.6838, True),
    ('BSN', '부산센터',      '부산광역시 강서구 화전산단1로 78번길 20',      35.1310, 128.9680, True),
    ('KYN', '경남센터',      '경상남도 함안군 칠서면 용성리 1085-3',         35.3760, 128.5390, True),
    ('GSN', '구미(경북)센터', '경상북도 구미시 산동면 이메리 71-2',           36.1942, 128.4640, True),
]


def _seed_centers():
    if OurCenter.query.count() == 0:
        for code, name, addr, lat, lon, is_hub in INITIAL_CENTERS:
            db.session.add(OurCenter(
                center_code=code, center_name=name, address=addr,
                lat=lat, lon=lon, is_direct_hub=is_hub,
                sort_order=INITIAL_CENTERS.index((code, name, addr, lat, lon, is_hub))
            ))
        db.session.commit()


# ── 이고비용 기본 데이터 (메인센터 → 거점센터, PLT당) ────────────────────────
# 거리 기준: 근거리(~100km) 20,000 / 중거리(~200km) 35,000 / 원거리(200km+) 60,000
INITIAL_TRANSFER_RATES = [
    # (from, to, plt단가, box단가, 메모)
    ('ICN', 'KMP',  15000, 130, '인천→김포 (근거리)'),
    ('ICN', 'SWN',  18000, 160, '인천→수원'),
    ('ICN', 'YGN',  22000, 190, '인천→용인'),
    ('ICN', 'IHC',  25000, 220, '인천→이천'),
    ('ICN', 'KSN',  25000, 220, '인천→광주(경기)'),
    ('ICN', 'ASN',  28000, 240, '인천→안성'),
    ('ICN', 'PLN',  28000, 240, '인천→평택'),
    ('ICN', 'WJU',  38000, 330, '인천→원주 (강원권)'),
    ('ICN', 'GWN',  38000, 330, '인천→강원(춘천)'),
    ('ICN', 'CHN',  32000, 280, '인천→천안 (충청권)'),
    ('ICN', 'OJ',   35000, 300, '인천→오창'),
    ('ICN', 'SEJ',  38000, 330, '인천→세종'),
    ('ICN', 'DJN',  40000, 350, '인천→대전'),
    ('ICN', 'BSN',  65000, 560, '인천→부산 (영남권)'),
    ('ICN', 'KYN',  65000, 560, '인천→경남'),
    ('ICN', 'GSN',  58000, 500, '인천→구미(경북)'),
    # 이천 기준 (수도권 동부 메인)
    ('IHC', 'WJU',  25000, 220, '이천→원주'),
    ('IHC', 'GWN',  35000, 300, '이천→강원(춘천)'),
    ('IHC', 'OJ',   30000, 260, '이천→오창'),
    ('IHC', 'DJN',  35000, 300, '이천→대전'),
    # 천안 기준 (충청 메인)
    ('CHN', 'OJ',   18000, 160, '천안→오창'),
    ('CHN', 'SEJ',  20000, 175, '천안→세종'),
    ('CHN', 'DJN',  22000, 190, '천안→대전'),
    # 대전 기준 (충청 거점→영남)
    ('DJN', 'BSN',  40000, 350, '대전→부산'),
    ('DJN', 'KYN',  38000, 330, '대전→경남'),
    ('DJN', 'GSN',  30000, 260, '대전→구미(경북)'),
]

# ── 거점 변동용차 기본 데이터 ─────────────────────────────────────────────────
# 차량: 1톤 / 2.5톤 / 5톤 (변동용차 주력 사이즈)
# 지구별 1회 왕복 단가 (배송 건수·거리 고려한 적정 금액)
INITIAL_HUB_VEHICLE_RATES = [
    # (센터코드, 배송지구, 차량종류, 단가)
    # ── 강원센터 (춘천 기준) ──
    ('GWN', '춘천시',  '1톤',   85000),
    ('GWN', '춘천시',  '2.5톤', 160000),
    ('GWN', '춘천시',  '5톤',   300000),
    ('GWN', '화천군',  '1톤',   110000),
    ('GWN', '화천군',  '2.5톤', 200000),
    ('GWN', '화천군',  '5톤',   380000),
    ('GWN', '양구군',  '1톤',   120000),
    ('GWN', '양구군',  '2.5톤', 210000),
    ('GWN', '인제군',  '1톤',   140000),
    ('GWN', '인제군',  '2.5톤', 240000),
    ('GWN', '홍천군',  '1톤',   100000),
    ('GWN', '홍천군',  '2.5톤', 185000),
    ('GWN', '속초시',  '1톤',   160000),
    ('GWN', '속초시',  '2.5톤', 290000),
    ('GWN', '속초시',  '5톤',   550000),
    ('GWN', '강릉시',  '1톤',   200000),
    ('GWN', '강릉시',  '2.5톤', 360000),
    ('GWN', '강릉시',  '5톤',   680000),
    ('GWN', '고성군',  '1톤',   180000),
    ('GWN', '고성군',  '2.5톤', 320000),
    # ── 원주센터 ──
    ('WJU', '원주시',  '1톤',   80000),
    ('WJU', '원주시',  '2.5톤', 150000),
    ('WJU', '원주시',  '5톤',   280000),
    ('WJU', '횡성군',  '1톤',   110000),
    ('WJU', '횡성군',  '2.5톤', 200000),
    ('WJU', '영월군',  '1톤',   140000),
    ('WJU', '영월군',  '2.5톤', 250000),
    ('WJU', '평창군',  '1톤',   130000),
    ('WJU', '평창군',  '2.5톤', 230000),
    ('WJU', '정선군',  '1톤',   160000),
    ('WJU', '정선군',  '2.5톤', 280000),
    ('WJU', '태백시',  '1톤',   200000),
    ('WJU', '태백시',  '2.5톤', 350000),
    ('WJU', '삼척시',  '1톤',   230000),
    ('WJU', '삼척시',  '2.5톤', 400000),
    # ── 부산센터 ──
    ('BSN', '부산 동부',  '1톤',   90000),
    ('BSN', '부산 동부',  '2.5톤', 170000),
    ('BSN', '부산 동부',  '5톤',   320000),
    ('BSN', '부산 서부',  '1톤',   85000),
    ('BSN', '부산 서부',  '2.5톤', 160000),
    ('BSN', '부산 서부',  '5톤',   300000),
    ('BSN', '부산 북부',  '1톤',   95000),
    ('BSN', '부산 북부',  '2.5톤', 175000),
    ('BSN', '김해시',     '1톤',   100000),
    ('BSN', '김해시',     '2.5톤', 185000),
    ('BSN', '김해시',     '5톤',   350000),
    ('BSN', '양산시',     '1톤',   110000),
    ('BSN', '양산시',     '2.5톤', 200000),
    # ── 경남센터 (함안) ──
    ('KYN', '함안군',  '1톤',   80000),
    ('KYN', '함안군',  '2.5톤', 150000),
    ('KYN', '창원시',  '1톤',   100000),
    ('KYN', '창원시',  '2.5톤', 185000),
    ('KYN', '창원시',  '5톤',   350000),
    ('KYN', '진주시',  '1톤',   120000),
    ('KYN', '진주시',  '2.5톤', 220000),
    ('KYN', '진주시',  '5톤',   420000),
    ('KYN', '고성군',  '1톤',   130000),
    ('KYN', '고성군',  '2.5톤', 230000),
    ('KYN', '통영시',  '1톤',   150000),
    ('KYN', '통영시',  '2.5톤', 270000),
    ('KYN', '거제시',  '1톤',   170000),
    ('KYN', '거제시',  '2.5톤', 300000),
    # ── 구미센터 (경북) ──
    ('GSN', '구미시',  '1톤',   80000),
    ('GSN', '구미시',  '2.5톤', 150000),
    ('GSN', '구미시',  '5톤',   280000),
    ('GSN', '칠곡군',  '1톤',   90000),
    ('GSN', '칠곡군',  '2.5톤', 165000),
    ('GSN', '성주군',  '1톤',   100000),
    ('GSN', '성주군',  '2.5톤', 185000),
    ('GSN', '김천시',  '1톤',   110000),
    ('GSN', '김천시',  '2.5톤', 200000),
    ('GSN', '상주시',  '1톤',   130000),
    ('GSN', '상주시',  '2.5톤', 230000),
    ('GSN', '안동시',  '1톤',   170000),
    ('GSN', '안동시',  '2.5톤', 300000),
    ('GSN', '포항시',  '1톤',   180000),
    ('GSN', '포항시',  '2.5톤', 320000),
    ('GSN', '포항시',  '5톤',   600000),
    # ── 대전센터 (충청 거점) ──
    ('DJN', '대전시',  '1톤',   80000),
    ('DJN', '대전시',  '2.5톤', 150000),
    ('DJN', '대전시',  '5톤',   280000),
    ('DJN', '논산시',  '1톤',   100000),
    ('DJN', '논산시',  '2.5톤', 185000),
    ('DJN', '공주시',  '1톤',   110000),
    ('DJN', '공주시',  '2.5톤', 200000),
    ('DJN', '계룡시',  '1톤',   90000),
    ('DJN', '계룡시',  '2.5톤', 165000),
    # ── 오창센터 (청주 거점) ──
    ('OJ',  '청주시',  '1톤',   80000),
    ('OJ',  '청주시',  '2.5톤', 150000),
    ('OJ',  '청주시',  '5톤',   280000),
    ('OJ',  '증평군',  '1톤',   95000),
    ('OJ',  '증평군',  '2.5톤', 175000),
    ('OJ',  '진천군',  '1톤',   100000),
    ('OJ',  '진천군',  '2.5톤', 185000),
    ('OJ',  '음성군',  '1톤',   105000),
    ('OJ',  '음성군',  '2.5톤', 190000),
    ('OJ',  '충주시',  '1톤',   130000),
    ('OJ',  '충주시',  '2.5톤', 230000),
]


def _seed_transfer_and_hub():
    if TransferRate.query.count() == 0:
        for from_c, to_c, plt_cost, box_cost, memo in INITIAL_TRANSFER_RATES:
            db.session.add(TransferRate(
                from_center_code=from_c, to_center_code=to_c,
                cost_per_plt=plt_cost, cost_per_box=box_cost, memo=memo
            ))
    if HubVehicleRate.query.count() == 0:
        for code, zone, vtype, price in INITIAL_HUB_VEHICLE_RATES:
            db.session.add(HubVehicleRate(
                center_code=code, delivery_zone=zone,
                vehicle_type=vtype, unit_price=price
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


# ─── 이고비용 마스터 ────────────────────────────────────────────────────────────

@app.route('/masters/transfer')
def transfer_master():
    rates = TransferRate.query.order_by(
        TransferRate.from_center_code, TransferRate.to_center_code
    ).all()
    centers = OurCenter.query.order_by(OurCenter.sort_order).all()
    center_map = {c.center_code: c.center_name for c in centers}
    return render_template('masters/transfer.html', rates=rates, centers=centers, center_map=center_map)


@app.route('/masters/transfer/add', methods=['POST'])
def transfer_rate_add():
    from_c  = request.form.get('from_center_code', '').strip()
    to_c    = request.form.get('to_center_code', '').strip()
    plt_s   = request.form.get('cost_per_plt', '').replace(',', '').strip()
    box_s   = request.form.get('cost_per_box', '').replace(',', '').strip()
    memo    = request.form.get('memo', '').strip()
    if not from_c or not to_c or not plt_s:
        flash('출발·도착 센터와 PLT당 이고비는 필수입니다.', 'danger')
        return redirect(url_for('transfer_master'))
    if from_c == to_c:
        flash('출발과 도착 센터가 같을 수 없습니다.', 'danger')
        return redirect(url_for('transfer_master'))
    try:
        plt_cost = int(plt_s)
        box_cost = int(box_s) if box_s else None
        existing = TransferRate.query.filter_by(from_center_code=from_c, to_center_code=to_c).first()
        if existing:
            existing.cost_per_plt = plt_cost
            existing.cost_per_box = box_cost
            existing.memo = memo
            flash(f'이고비 [{from_c}→{to_c}] 업데이트', 'success')
        else:
            db.session.add(TransferRate(
                from_center_code=from_c, to_center_code=to_c,
                cost_per_plt=plt_cost, cost_per_box=box_cost, memo=memo
            ))
            flash(f'이고비 [{from_c}→{to_c}] 추가', 'success')
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        flash(f'오류: {e}', 'danger')
    return redirect(url_for('transfer_master'))


@app.route('/masters/transfer/<int:rid>/delete', methods=['POST'])
def transfer_rate_delete(rid):
    r = TransferRate.query.get_or_404(rid)
    label = f'{r.from_center_code}→{r.to_center_code}'
    db.session.delete(r)
    db.session.commit()
    flash(f'[{label}] 이고비 삭제', 'warning')
    return redirect(url_for('transfer_master'))


# ─── 거점 변동용차 비용 마스터 ──────────────────────────────────────────────────

@app.route('/masters/hub-vehicle')
def hub_vehicle_master():
    rates = HubVehicleRate.query.order_by(
        HubVehicleRate.center_code, HubVehicleRate.delivery_zone, HubVehicleRate.vehicle_type
    ).all()
    centers = OurCenter.query.order_by(OurCenter.sort_order).all()
    center_map = {c.center_code: c.center_name for c in centers}

    # 거점별 그룹핑
    by_center = {}
    for r in rates:
        by_center.setdefault(r.center_code, []).append(r)

    # 차량 종류 목록
    vehicle_types = [c.vehicle_type for c in VehicleCapacity.query.order_by(VehicleCapacity.sort_order).all()]

    return render_template('masters/hub_vehicle.html',
                           by_center=by_center, centers=centers,
                           center_map=center_map, vehicle_types=vehicle_types)


@app.route('/masters/hub-vehicle/add', methods=['POST'])
def hub_vehicle_rate_add():
    code   = request.form.get('center_code', '').strip()
    zone   = request.form.get('delivery_zone', '').strip()
    vtype  = request.form.get('vehicle_type', '').strip()
    price_s = request.form.get('unit_price', '').replace(',', '').strip()
    memo   = request.form.get('memo', '').strip()
    if not code or not zone or not vtype or not price_s:
        flash('센터·배송지구·차량종류·단가는 필수입니다.', 'danger')
        return redirect(url_for('hub_vehicle_master'))
    try:
        price = int(price_s)
        existing = HubVehicleRate.query.filter_by(
            center_code=code, delivery_zone=zone, vehicle_type=vtype
        ).first()
        if existing:
            existing.unit_price = price
            existing.memo = memo
            flash(f'[{code}] {zone} {vtype} 단가 업데이트', 'success')
        else:
            db.session.add(HubVehicleRate(
                center_code=code, delivery_zone=zone,
                vehicle_type=vtype, unit_price=price, memo=memo
            ))
            flash(f'[{code}] {zone} {vtype} 단가 추가', 'success')
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        flash(f'오류: {e}', 'danger')
    return redirect(url_for('hub_vehicle_master'))


@app.route('/masters/hub-vehicle/<int:rid>/delete', methods=['POST'])
def hub_vehicle_rate_delete(rid):
    r = HubVehicleRate.query.get_or_404(rid)
    label = f'{r.center_code} {r.delivery_zone} {r.vehicle_type}'
    db.session.delete(r)
    db.session.commit()
    flash(f'[{label}] 삭제', 'warning')
    return redirect(url_for('hub_vehicle_master'))


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
