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
    JointDeliveryRate, ProductMaster, StoreMaster,
    ShippingHistory, CalculationResult
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

with app.app_context():
    if VehicleCapacity.query.count() == 0:
        for vt, mp, so in DEFAULT_CAPACITIES:
            db.session.add(VehicleCapacity(vehicle_type=vt, max_plt=mp, sort_order=so))
        db.session.commit()


# ─── 대시보드 ──────────────────────────────────────────────────────────────────

@app.route('/')
def dashboard():
    customers = Customer.query.order_by(Customer.created_at.desc()).all()
    vehicle_dest_count = db.session.query(VehicleRate.destination).distinct().count()
    recent_results = CalculationResult.query.order_by(
        CalculationResult.calc_date.desc()
    ).limit(10).all()
    return render_template('dashboard.html',
                           customers=customers,
                           vehicle_dest_count=vehicle_dest_count,
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
    customer = Customer.query.get_or_404(cid)
    batches = db.session.query(
        ShippingHistory.batch_id,
        db.func.count(ShippingHistory.id).label('cnt'),
        db.func.min(ShippingHistory.uploaded_at).label('uploaded_at')
    ).filter_by(customer_id=cid).group_by(ShippingHistory.batch_id).order_by(
        db.func.min(ShippingHistory.uploaded_at).desc()
    ).all()
    return render_template('calculation/index.html', customer=customer, batches=batches)


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
            '납품일자': 'shipping_date', '출고일': 'shipping_date',
            '주문번호': 'order_no',
            '오더유형': 'order_type',
            '채널': 'channel',
            '배송처코드': 'store_code', '점포코드': 'store_code',
            '배송처명': 'store_name', '점포명': 'store_name',
            '주소': 'address',
            '상품코드': 'product_code',
            '상품명': 'product_name',
            '출고수량(BOX)': 'box_qty', '수량(BOX)': 'box_qty', '수량': 'box_qty',
            '출고수량(PLT)': 'plt_qty_decimal', 'PLT수': 'plt_qty_decimal',
            'PLT 환산': 'plt_qty_int', 'PLT환산': 'plt_qty_int',
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

        batch_id = str(uuid.uuid4())[:8]
        added = 0
        for _, row in df.iterrows():
            if pd.isna(row.get('store_code')) and pd.isna(row.get('store_name')):
                continue

            def safe_str(col):
                v = row.get(col)
                return str(v).strip() if col in df.columns and not pd.isna(v) else None

            def safe_float(col):
                v = row.get(col)
                try:
                    return float(v) if col in df.columns and not pd.isna(v) else None
                except Exception:
                    return None

            def safe_int(col):
                v = safe_float(col)
                return int(math.ceil(v)) if v is not None else None

            shipping_date = None
            sd_val = row.get('shipping_date')
            if 'shipping_date' in df.columns and not pd.isna(sd_val):
                try:
                    shipping_date = pd.to_datetime(sd_val).date()
                except Exception:
                    pass

            db.session.add(ShippingHistory(
                customer_id=cid,
                batch_id=batch_id,
                shipping_date=shipping_date,
                order_no=safe_str('order_no'),
                order_type=safe_str('order_type'),
                channel=safe_str('channel'),
                store_code=safe_str('store_code'),
                store_name=safe_str('store_name'),
                address=safe_str('address'),
                product_code=safe_str('product_code'),
                product_name=safe_str('product_name'),
                box_qty=safe_float('box_qty'),
                plt_qty_decimal=safe_float('plt_qty_decimal'),
                plt_qty_int=safe_int('plt_qty_int'),
            ))
            added += 1
        db.session.commit()
        flash(f'{added}건 출고내역 업로드 완료 (배치: {batch_id})', 'success')
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


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
