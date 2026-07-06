import sys
sys.stdout.reconfigure(encoding='utf-8')
import pandas as pd, math, uuid
from app import app, db
from models import ShippingHistory, Customer, VehicleRate
from calculator import calculate_from_history, summarize_results

with app.app_context():
    print("=== DB 상태 ===")
    print("VehicleRate:", VehicleRate.query.count(), "건")
    dest_count = db.session.query(VehicleRate.destination).distinct().count()
    print("도착지:", dest_count, "개")

    cust = Customer.query.first()
    if not cust:
        print("ERROR: 고객사 없음")
        exit(1)
    cid = cust.id
    print("고객사:", cust.name)

    # 기존 테스트 데이터 삭제
    ShippingHistory.query.filter_by(customer_id=cid, batch_id='test001').delete()
    db.session.commit()

    # 실제 Excel 업로드
    xl = pd.ExcelFile(r'C:\Users\HanEx\Desktop\배송비 단가 산정 만들기.xlsx')
    df = xl.parse('출고내역')
    df.columns = [str(c).strip() for c in df.columns]
    col_map = {
        '납품일자': 'shipping_date', '배송처코드': 'store_code',
        '배송처명': 'store_name', '주소': 'address',
        '상품코드': 'product_code', '상품명': 'product_name',
        '출고수량(BOX)': 'box_qty', '출고수량(PLT)': 'plt_qty_decimal',
        'PLT 환산': 'plt_qty_int',
        '주문번호': 'order_no', '오더유형': 'order_type', '채널': 'channel',
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    added, upload_errors = 0, []
    for _, row in df.iterrows():
        try:
            sc = row.get('store_code')
            sn = row.get('store_name')
            if pd.isna(sc) and pd.isna(sn):
                continue
            ship_date = None
            sd = row.get('shipping_date')
            if sd is not None and not (sd != sd):
                ship_date = pd.to_datetime(sd).date()

            plt_dec = row.get('plt_qty_decimal')
            plt_dec = float(plt_dec) if plt_dec is not None and not (plt_dec != plt_dec) else 0.0

            plt_int_val = row.get('plt_qty_int')
            plt_int = int(math.ceil(float(plt_int_val))) if plt_int_val is not None and not (plt_int_val != plt_int_val) else None

            box = row.get('box_qty')
            box = float(box) if box is not None and not (box != box) else None

            addr = row.get('address')
            addr = str(addr).strip() if addr is not None and not (addr != addr) else None

            def safe(col):
                v = row.get(col)
                return str(v).strip() if col in df.columns and v is not None and not (v != v) else None

            db.session.add(ShippingHistory(
                customer_id=cid, batch_id='test001',
                shipping_date=ship_date,
                store_code=str(sc).strip() if sc is not None and not (sc != sc) else None,
                store_name=str(sn).strip() if sn is not None and not (sn != sn) else None,
                address=addr,
                product_code=safe('product_code'),
                box_qty=box, plt_qty_decimal=plt_dec, plt_qty_int=plt_int,
            ))
            added += 1
        except Exception as e:
            upload_errors.append(str(e))

    db.session.commit()
    print(f"\n=== 업로드 결과 ===")
    print(f"추가: {added}건, 오류: {len(upload_errors)}")
    if upload_errors:
        print("오류 샘플:", upload_errors[:3])

    # 계산 실행
    rows = ShippingHistory.query.filter_by(customer_id=cid, batch_id='test001').all()
    print(f"\n=== 계산 실행 ({len(rows)}건) ===")
    results, calc_errors = calculate_from_history(rows, cid, '테스트', db.session)

    print(f"결과: {len(results)}건")
    print(f"계산 오류: {len(calc_errors)}건")
    if calc_errors:
        print("오류 샘플:")
        for e in calc_errors[:5]:
            print(" -", e)

    valid = [r for r in results if r['delivery_cost']]
    no_cost = [r for r in results if not r['delivery_cost']]
    print(f"단가 산출: {len(valid)}건 성공, {len(no_cost)}건 실패")

    if valid:
        summary = summarize_results(results)
        print(f"직송: {summary['direct_count']}건, 공동배송: {summary['joint_count']}건")
        print(f"평균 박스당 단가: {summary['avg_cost_per_box']}원")

    if no_cost:
        print("\n단가 미산출 샘플:")
        for r in no_cost[:3]:
            print(f"  {r['store_name']} | 도착지={r['destination']} | PLT={r['total_plt_count']} | {r['memo']}")
