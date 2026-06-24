"""
사용 가이드용 스크린샷 자동 캡쳐 스크립트.

사용법:
  1. Flask 서버를 먼저 실행: python app.py
  2. 이 스크립트를 별도 터미널에서 실행: python capture_screenshots.py
  3. 완료 후 /help 페이지를 새로고침하면 스크린샷이 표시됨.

필요 패키지: selenium (pip install selenium)
Chrome 브라우저가 설치되어 있어야 합니다.
"""

import os
import sys
import time

BASE_URL  = "http://127.0.0.1:5000"
OUT_DIR   = os.path.join(os.path.dirname(__file__), "static", "help", "screenshots")
WIDTH     = 1440
HEIGHT    = 900
WAIT_SEC  = 1.5   # 페이지 로드 대기 시간 (느린 PC에서는 늘릴 것)

# 첫 번째 고객사 ID — 결과 데이터가 있는 고객 ID로 변경하세요
SAMPLE_CUSTOMER_ID = 1

PAGES = [
    ("analytics",       f"{BASE_URL}/"),
    ("analytics_detail",f"{BASE_URL}/analytics/{SAMPLE_CUSTOMER_ID}"),
    ("map",             f"{BASE_URL}/map?customer_id={SAMPLE_CUSTOMER_ID}"),
    ("customers",       f"{BASE_URL}/customers"),
    ("calculate",       f"{BASE_URL}/customers/{SAMPLE_CUSTOMER_ID}/calculate"),
    ("results",         f"{BASE_URL}/customers/{SAMPLE_CUSTOMER_ID}/results"),
    ("vehicle",         f"{BASE_URL}/masters/vehicle"),
    ("transfer",        f"{BASE_URL}/masters/transfer"),
    ("hub_vehicle",     f"{BASE_URL}/masters/hub-vehicle"),
    ("distance_rate",   f"{BASE_URL}/masters/distance-rate"),
    ("centers",         f"{BASE_URL}/centers"),
    ("zone_mapping",    f"{BASE_URL}/masters/delivery-zone-mapping"),
    ("settings",        f"{BASE_URL}/settings"),
]


def main():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    os.makedirs(OUT_DIR, exist_ok=True)

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument(f"--window-size={WIDTH},{HEIGHT}")
    options.add_argument("--hide-scrollbars")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--lang=ko-KR")
    options.add_argument("--disable-dev-shm-usage")

    # webdriver-manager가 있으면 자동으로 chromedriver 경로 지정
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
        driver  = webdriver.Chrome(service=service, options=options)
    except Exception:
        # 없으면 PATH에서 chromedriver 탐색
        driver = webdriver.Chrome(options=options)

    driver.set_window_size(WIDTH, HEIGHT)

    try:
        for slug, url in PAGES:
            out_path = os.path.join(OUT_DIR, f"{slug}.png")
            print(f"  [{slug}] {url} ...", end=" ", flush=True)
            try:
                driver.get(url)
                time.sleep(WAIT_SEC)

                # 지도 페이지는 JS 렌더링 대기를 더 줌
                if slug == "map":
                    time.sleep(2.0)

                # 전체 페이지 높이로 뷰포트 확장 후 캡쳐
                total_h = driver.execute_script("return document.body.scrollHeight")
                capture_h = min(total_h, 2400)  # 너무 길면 2400px 제한
                driver.set_window_size(WIDTH, capture_h)
                time.sleep(0.3)

                driver.save_screenshot(out_path)
                driver.set_window_size(WIDTH, HEIGHT)  # 다음 페이지를 위해 복원
                print("OK")
            except Exception as e:
                print(f"SKIP ({e})")

    finally:
        driver.quit()

    print(f"\n완료! {OUT_DIR} 에 저장됨")
    print("Flask /help 페이지를 새로고침하면 스크린샷이 표시됩니다.")


if __name__ == "__main__":
    main()
