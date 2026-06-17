from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class Customer(db.Model):
    __tablename__ = 'customers'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    memo = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.now)

    products = db.relationship('ProductMaster', backref='customer', lazy=True, cascade='all, delete-orphan')
    stores = db.relationship('StoreMaster', backref='customer', lazy=True, cascade='all, delete-orphan')
    histories = db.relationship('ShippingHistory', backref='customer', lazy=True, cascade='all, delete-orphan')
    results = db.relationship('CalculationResult', backref='customer', lazy=True, cascade='all, delete-orphan')


class VehicleRate(db.Model):
    """차량 단가: 도착지(시군구) × 차량종류 → 직송 단가"""
    __tablename__ = 'vehicle_rate'
    id = db.Column(db.Integer, primary_key=True)
    destination = db.Column(db.String(100), nullable=False)   # 예: 강원도 강릉시
    vehicle_type = db.Column(db.String(20), nullable=False)   # 11톤, 5톤장축, 5톤, 3.5톤, 2.5톤, 1.4톤, 1톤, 퀵
    unit_price = db.Column(db.Integer, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        db.UniqueConstraint('destination', 'vehicle_type', name='uq_dest_vehicle'),
    )


class VehicleCapacity(db.Model):
    """차량별 최대 PLT 적재량"""
    __tablename__ = 'vehicle_capacity'
    id = db.Column(db.Integer, primary_key=True)
    vehicle_type = db.Column(db.String(20), nullable=False, unique=True)
    max_plt = db.Column(db.Float, nullable=False)
    sort_order = db.Column(db.Integer, default=0)   # 낮을수록 큰 차량 (비용 기준 정렬용)


class SurchargeRule(db.Model):
    """부가 요금 규칙: 대기비, 경유비, 수작업, 회송비"""
    __tablename__ = 'surcharge_rule'
    id = db.Column(db.Integer, primary_key=True)
    surcharge_type = db.Column(db.String(30), nullable=False)  # 대기비, 경유비, 수작업, 회송비
    vehicle_type = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Integer, nullable=False)


class JointDeliveryRate(db.Model):
    """공동배송 단가: 지역(운영센터/구역) 기반"""
    __tablename__ = 'joint_delivery_rate'
    id = db.Column(db.Integer, primary_key=True)
    region_name = db.Column(db.String(100), nullable=False, unique=True)  # 운영센터명 또는 지역구분
    price_per_box = db.Column(db.Float)    # 박스당 단가
    price_per_kg = db.Column(db.Float)     # 중량당 단가
    memo = db.Column(db.String(200))
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class ProductMaster(db.Model):
    """고객사 상품 마스터"""
    __tablename__ = 'product_master'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    product_code = db.Column(db.String(50), nullable=False)
    product_name = db.Column(db.String(300), nullable=False)
    box_in_count = db.Column(db.Integer)           # BOX입수 (박스 안 낱개 수)
    box_width = db.Column(db.Float)
    box_depth = db.Column(db.Float)
    box_height = db.Column(db.Float)
    box_weight_kg = db.Column(db.Float)
    plt_per_box = db.Column(db.Integer)            # PLT입수 (PLT당 BOX 수)
    uploaded_at = db.Column(db.DateTime, default=datetime.now)

    __table_args__ = (
        db.UniqueConstraint('customer_id', 'product_code', name='uq_customer_product'),
    )


class StoreMaster(db.Model):
    """고객사 배송처(점포) 마스터"""
    __tablename__ = 'store_master'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    store_code = db.Column(db.String(50), nullable=False)
    store_name = db.Column(db.String(300), nullable=False)
    address = db.Column(db.String(500))
    sido = db.Column(db.String(50))               # 시도 (경기도, 강원도 등)
    sigungu = db.Column(db.String(50))            # 시군구
    destination = db.Column(db.String(100))       # 차량마스터 도착지 (매핑 결과)
    center_name = db.Column(db.String(100))       # 운영센터 (공동배송망)
    delivery_mode = db.Column(db.String(10))      # 직송 / 공동배송 / 혼합
    uploaded_at = db.Column(db.DateTime, default=datetime.now)

    __table_args__ = (
        db.UniqueConstraint('customer_id', 'store_code', name='uq_customer_store'),
    )


class ShippingHistory(db.Model):
    """출고 내역"""
    __tablename__ = 'shipping_history'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    batch_id = db.Column(db.String(50))
    shipping_date = db.Column(db.Date)
    order_no = db.Column(db.String(50))
    order_type = db.Column(db.String(20))         # B2B, B2C 등
    channel = db.Column(db.String(50))            # 채널 (할인점, 편의점 등)
    store_code = db.Column(db.String(50))
    store_name = db.Column(db.String(300))
    address = db.Column(db.String(500))
    product_code = db.Column(db.String(50))
    product_name = db.Column(db.String(300))
    box_qty = db.Column(db.Float)                 # 출고수량(BOX)
    plt_qty_decimal = db.Column(db.Float)         # 출고수량(PLT) 소수점
    plt_qty_int = db.Column(db.Integer)           # PLT 환산 (올림)
    uploaded_at = db.Column(db.DateTime, default=datetime.now)


class CalculationResult(db.Model):
    """단가 산정 결과"""
    __tablename__ = 'calculation_results'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    calc_name = db.Column(db.String(200))
    calc_date = db.Column(db.DateTime, default=datetime.now)
    batch_id = db.Column(db.String(50))
    shipping_date = db.Column(db.Date)
    store_code = db.Column(db.String(50))
    store_name = db.Column(db.String(300))
    address = db.Column(db.String(500))
    destination = db.Column(db.String(100))       # 차량마스터 도착지
    region = db.Column(db.String(50))
    delivery_mode = db.Column(db.String(20))      # 직송 / 공동배송
    total_box_qty = db.Column(db.Float)
    total_plt_decimal = db.Column(db.Float)       # 합산 PLT (소수)
    total_plt_count = db.Column(db.Integer)       # 합산 PLT (올림)
    vehicle_type = db.Column(db.String(20))
    delivery_cost = db.Column(db.Integer)
    cost_per_box = db.Column(db.Float)
    memo = db.Column(db.String(500))
