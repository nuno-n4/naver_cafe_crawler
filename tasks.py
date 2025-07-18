import os
import time
import re
import requests
import random
from celery import Celery, Task
from flask_socketio import SocketIO
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from datetime import datetime

# Celery 앱 설정 (app.py와 동일하게)
celery_app = Celery('tasks', broker='redis://localhost:6379/0', backend='redis://localhost:6379/0')

# SocketIO 클라이언트 설정 (서버로 메시지를 보내기 위함)
socketio_client = SocketIO(message_queue='redis://')

def sanitize_filename(filename):
    """파일 이름으로 사용할 수 없는 문자를 제거합니다."""
    return re.sub(r'[\/*?:"<>|]', "", filename).strip()

class ScrapeTask(Task):
    """진행 상황을 업데이트 할 수 있는 커스텀 Task 클래스"""
    def send_progress(self, message):
        """웹페이지로 진행 상황 메시지를 전송하는 함수"""
        socketio_client.emit('progress_update', {'message': message})
        print(message) # Celery 작업자 터미널에도 로그 출력
        socketio_client.sleep(0.1)

def _perform_login(driver, task_instance):
    """사용자 수동 로그인을 처리하는 함수"""
    task_instance.send_progress("네이버 로그인을 위해 브라우저를 엽니다.")
    task_instance.send_progress("60초 내에 직접 로그인해주세요. 완료하면 자동으로 진행됩니다.")
    driver.get('https://nid.naver.com/nidlogin.login')
    try:
        # 로그인 성공 시 네이버 메인 페이지의 특정 요소가 나타나는 것을 기다림
        WebDriverWait(driver, 60).until(
            EC.presence_of_element_located((By.ID, "account"))
        )
        task_instance.send_progress("로그인 성공을 확인했습니다.")
        return True
    except TimeoutException:
        task_instance.send_progress("[오류] 60초 내에 로그인이 확인되지 않았습니다.")
        return False

def _scrape_article_detail(driver, task_instance, article_url, download_dir):
    """게시글 상세 페이지에서 내용과 이미지를 스크랩하는 함수"""
    try:
        driver.get(article_url)
        
        # cafe_main iframe으로 전환
        WebDriverWait(driver, 10).until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "cafe_main")))

        title_element = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "h3.title_text"))
        )
        title = title_element.text
        sanitized_title = sanitize_filename(title)
        task_instance.send_progress(f"게시글 스크랩 중: {title}")

        content = driver.find_element(By.CSS_SELECTOR, 'div.se-main-container').text

        # 게시글 내용 저장
        post_dir = os.path.join(download_dir, sanitized_title)
        os.makedirs(post_dir, exist_ok=True)
        with open(os.path.join(post_dir, 'content.txt'), 'w', encoding='utf-8') as f:
            f.write(content)

        # 이미지 다운로드
        images = driver.find_elements(By.CSS_SELECTOR, 'img.se-image-resource')
        task_instance.send_progress(f"{len(images)}개의 이미지 발견.")
        for i, img in enumerate(images):
            img_url = img.get_attribute('src')
            if img_url and not img_url.startswith('data:'): # base64 인코딩된 이미지는 제외
                try:
                    img_data = requests.get(img_url, headers={'Referer': driver.current_url}).content
                    with open(os.path.join(post_dir, f'image_{i+1}.jpg'), 'wb') as f:
                        f.write(img_data)
                except Exception as e:
                    task_instance.send_progress(f"[경고] 이미지 다운로드 실패: {img_url}, 오류: {e}")
        
        driver.switch_to.default_content()
        return True

    except Exception as e:
        task_instance.send_progress(f"[오류] 게시글 상세 내용 스크랩 실패: {article_url}, 오류: {e}")
        driver.switch_to.default_content() # 오류 발생 시에도 iframe에서 빠져나오도록 보장
        return False


@celery_app.task(bind=True, base=ScrapeTask)
def run_cafe_scrape(self, cafe_id, start_date_str, end_date_str):
    """메인 크롤링 로직을 담고 있는 Celery 작업"""
    self.send_progress("크롤링 초기화 중...")
    
    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    
    download_dir = os.path.join(os.getcwd(), "downloads", sanitize_filename(cafe_id))
    os.makedirs(download_dir, exist_ok=True)

    service = Service(ChromeDriverManager().install())
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(service=service, options=options)
    driver.implicitly_wait(5)

    try:
        if not _perform_login(driver, self):
            raise Exception("로그인 실패")

        self.send_progress(f"카페(ID: {cafe_id}) 크롤링을 시작합니다.")
        self.send_progress(f"추출 기간: {start_date_str} ~ {end_date_str}")

        page = 1
        total_posts_scraped = 0
        stop_scraping = False

        while not stop_scraping:
            self.send_progress(f"--- {page} 페이지 크롤링 시작 ---")
            board_url = f"https://cafe.naver.com/ca-fe/cafes/{cafe_id}/menus/0?page={page}"
            driver.get(board_url)
            
            WebDriverWait(driver, 10).until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "cafe_main")))

            articles = driver.find_elements(By.css_selector, 'div.article-board[class*="type-L"] > table > tbody > tr')
            if not articles:
                self.send_progress("게시글이 더 이상 없거나, 페이지 구조가 변경되었습니다. 크롤링을 종료합니다.")
                break

            article_links = []
            for article in articles:
                try:
                    date_element = article.find_element(By.css_selector, 'td.td_date')
                    post_date_str = date_element.text
                    
                    if ':' in post_date_str:
                        post_date = datetime.now().date()
                    else:
                        post_date = datetime.strptime(post_date_str, '%Y.%m.%d.').date()

                    if post_date > end_date:
                        continue
                    if post_date < start_date:
                        stop_scraping = True
                        break
                    
                    link_element = article.find_element(By.css_selector, 'a.article')
                    article_links.append(link_element.get_attribute('href'))

                except NoSuchElementException:
                    self.send_progress("[정보] 날짜 정보가 없는 행(공지 등)을 건너뜁니다.")
                    continue
            
            if stop_scraping:
                self.send_progress(f"게시글 날짜가 시작일({start_date_str})보다 이전이므로 종료합니다.")
                break
            
            self.send_progress(f"{len(article_links)}개의 게시글을 발견하여 스크랩을 시작합니다.")
            for link in article_links:
                if _scrape_article_detail(driver, self, link, download_dir):
                    total_posts_scraped += 1
                time.sleep(random.uniform(1, 3)) # 다음 게시글 스크랩 전 지연

            page += 1
            driver.switch_to.default_content()

    except Exception as e:
        self.send_progress(f"[치명적 오류] 크롤링 중단: {e}")
    finally:
        driver.quit()
        self.send_progress(f"크롤링 완료! 총 {total_posts_scraped}개의 게시글을 스크랩했습니다. 🎉")

    return f"총 {total_posts_scraped}개 게시글 처리 완료."