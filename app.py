import os
import re
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from flask_socketio import SocketIO
from celery import Celery
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# .env 파일 로드
load_dotenv()

# Flask 앱 설정
app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)

# Celery 설정
app.config['CELERY_BROKER_URL'] = 'redis://localhost:6379/0'
app.config['CELERY_RESULT_BACKEND'] = 'redis://localhost:6379/0'

# Celery 인스턴스 생성
celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
celery.conf.update(app.config)

# SocketIO 설정
socketio = SocketIO(app, message_queue='redis://')

# --- 라우트(URL) 정의 ---

@app.route('/', methods=['GET', 'POST'])
def index():
    """메인 페이지: 로그인 완료 시 카페 선택 화면, 아니면 로그인 페이지로 리디렉션"""
    if request.method == 'POST':
        cafe_id = request.form.get('cafe_id')
        cafe_name = request.form.get('cafe_name')
        if not cafe_id or not cafe_name:
            return "오류: 카페 ID와 이름이 필요합니다.", 400
        session['selected_cafe_id'] = cafe_id
        session['selected_cafe_name'] = cafe_name
        return redirect(url_for('scrape_page'))

    # 세션에 카페 목록이 있으면, 해당 목록으로 페이지 렌더링
    if 'my_cafes' in session:
        return render_template('index.html', my_cafes=session['my_cafes'])
    
    # 세션에 카페 목록이 없으면, 로그인 페이지로 안내
    return redirect(url_for('login'))

@app.route('/login')
def login():
    """네이버 로그인을 유도하는 페이지"""
    return render_template('login.html')

@app.route('/fetch_cafes')
def fetch_cafes():
    """Selenium으로 네이버 로그인 후, 가입된 카페 목록을 가져오는 경로"""
    # 이미 카페 목록이 세션에 있으면, 다시 로그인하지 않고 메인 페이지로 리디렉션
    if 'my_cafes' in session:
        return redirect(url_for('index'))

    options = webdriver.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.binary_location = "/usr/bin/google-chrome-stable"
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.implicitly_wait(5)

    try:
        # 1. 네이버 로그인 페이지로 이동
        driver.get('https://nid.naver.com/nidlogin.login')
        
        # .env 파일에서 ID와 비밀번호 가져오기
        naver_id = os.getenv('NAVER_ID')
        naver_pw = os.getenv('NAVER_PW')

        if not naver_id or not naver_pw:
            raise ValueError("NAVER_ID 또는 NAVER_PW가 .env 파일에 설정되어 있지 않습니다.")

        # ID 입력 필드 찾기 및 입력
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, "id")))
        driver.find_element(By.ID, "id").send_keys(naver_id)

        # 비밀번호 입력 필드 찾기 및 입력
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, "pw")))
        driver.find_element(By.ID, "pw").send_keys(naver_pw)

        # 로그인 버튼 클릭
        WebDriverWait(driver, 30).until(EC.element_to_be_clickable((By.ID, "log.login")))
        driver.find_element(By.ID, "log.login").click()

        # 3. 로그인 성공 후 '내 카페 목록' 페이지로 이동 (로그인 성공 여부 확인)
        # 로그인 성공 후 URL이 변경되거나 특정 요소가 나타날 때까지 기다립니다.
        # 여기서는 '내 카페' 페이지로 리디렉션되는 것을 기다립니다.
        WebDriverWait(driver, 30).until(EC.url_contains("cafe.naver.com/MyCafeIntro.nhn"))
        
        # 3. 로그인 성공 후 '내 카페 목록' 페이지로 이동
        driver.get('https://cafe.naver.com/MyCafeIntro.nhn')
        
        # 4. 'cafe_main' iframe으로 전환
        WebDriverWait(driver, 10).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "cafe_main"))
        )
        
        # 5. 카페 목록 파싱
        cafe_elements = driver.find_elements(By.CSS_SELECTOR, 'ul.my-cafe-list a')
        my_cafes = []
        for cafe in cafe_elements:
            href = cafe.get_attribute('href')
            name = cafe.find_element(By.CSS_SELECTOR, 'span.cafe-name').text
            # href에서 cafeId 추출 (e.g., '...&clubid=12345' -> '12345')
            match = re.search(r'clubid=(\d+)', href)
            if match:
                cafe_id = match.group(1)
                my_cafes.append({'id': cafe_id, 'name': name})
        
        # 6. 파싱된 카페 목록을 세션에 저장
        session['my_cafes'] = my_cafes
        
    except TimeoutException:
        return "오류: 60초 내에 로그인이 확인되지 않았습니다. 다시 시도해주세요.", 408
    except Exception as e:
        return f"오류가 발생했습니다: {e}", 500
    finally:
        driver.quit()
        
    # 7. 메인 페이지로 리디렉션
    return redirect(url_for('index'))


@app.route('/scrape')
def scrape_page():
    """크롤링 설정 및 실행 페이지"""
    cafe_id = session.get('selected_cafe_id')
    cafe_name = session.get('selected_cafe_name')
    
    if not cafe_id or not cafe_name:
        return redirect(url_for('index'))
        
    return render_template('scrape.html', cafe_id=cafe_id, cafe_name=cafe_name)

# --- Socket.IO 이벤트 핸들러 ---

@socketio.on('connect')
def handle_connect():
    """사용자가 웹페이지에 접속했을 때 호출"""
    print('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    """사용자 접속이 끊겼을 때 호출"""
    print('Client disconnected')

@socketio.on('start_scrape_task')
def handle_start_scrape(data):
    """'추출 버튼' 클릭 시 웹페이지로부터 호출"""
    cafe_id = data.get('cafe_id')
    start_date = data.get('start_date')
    end_date = data.get('end_date')

    if not cafe_id:
        socketio.emit('task_response', {'status': '오류: 카페 ID가 없습니다.'})
        return

    # Celery를 통해 백그라운드에서 크롤링 작업 시작
    # .delay()는 작업을 즉시 백그라운드에 보내고 다음 코드를 실행합니다.
    from tasks import run_cafe_scrape # 순환 참조 방지를 위해 여기서 import
    task = run_cafe_scrape.delay(cafe_id, start_date, end_date)
    
    socketio.emit('task_response', {
        'status': f'크롤링 작업을 시작합니다 (작업 ID: {task.id}).',
        'task_id': task.id
    })


if __name__ == '__main__':
    # Flask 앱을 SocketIO 서버로 실행
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True, port=5001)