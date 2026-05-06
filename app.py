import requests
import time
import threading
import json
import uuid
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
import random
import logging
import os

app = Flask(__name__)
app.secret_key = 'webook_bot_super_secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ------------------- إعدادات أساسية -------------------
BASE_CONFIG = {
    "WORKSPACE_KEY": "66e63c10464382fb1f049832",
    "EVENT_SLUG": "nassr-vs-hilal",
    "CHART_KEY": "38bd4175-2082-4161-8c13-b396b98d477c",
    "CHECKOUT_URL": "https://webook.com/ar/checkout"
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://webook.com/",
    "Origin": "https://webook.com",
    "Content-Type": "application/json"
}

# ------------------- الحالة العامة -------------------
accounts = []          # كل حساب: dict يحتوي على email, password, access_token, refresh_token, token_expiry, captcha, signature, proxy, status, held_seats, hold_expiry, hold_token
proxies_list = []
bot_running = False
bot_mode = "scanner"   # scanner / checker
active_threads = []
sound_alerts = True
max_per_account = 2

# ------------------- دوال تسجيل الدخول وإدارة التوكن -------------------
def login_to_webook(email, password, captcha="", signature="", proxy=None):
    """
    تسجيل الدخول الفعلي إلى Webook.
    إذا تم توفير captcha و signature يتم إرسالهما، وإلا يرسل request بدونهما.
    تعيد (access_token, refresh_token, expiry_timestamp) أو None في حالة الفشل.
    """
    url = "https://api.webook.com/api/v2/login"
    payload = {
        "email": email,
        "password": password,
        "app_source": "rs",
        "login_with": "email",
        "lang": "ar"
    }
    if captcha:
        payload["captcha"] = captcha
    if signature:
        payload["signature"] = signature
    
    headers = {
        "Content-Type": "application/json",
        "authorization": "Bearer ",
        "token": "e9aac1f2f0b6c07d6be070ed14829de684264278359148d6a582ca65a50934d2",  # token ثابت كما في HAR
        "origin": "https://webook.com"
    }
    proxy_dict = {"http": proxy, "https": proxy} if proxy else None
    try:
        resp = requests.post(url, json=payload, headers=headers, proxies=proxy_dict, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            access_token = data.get('access_token')
            refresh_token = data.get('refresh_token')
            # عادةً الـ JWT له صلاحية ساعة؛ نحدد وقت انتهاء افتراضي بعد ساعة
            expires_in = data.get('expires_in', 3600)
            expiry = time.time() + expires_in
            return access_token, refresh_token, expiry
        else:
            socketio.emit('log', {'message': f"فشل تسجيل دخول {email}: {resp.status_code} {resp.text}", 'type': 'error'})
            return None, None, None
    except Exception as e:
        socketio.emit('log', {'message': f"خطأ في تسجيل دخول {email}: {str(e)}", 'type': 'error'})
        return None, None, None

def refresh_access_token(refresh_token, proxy=None):
    """تحديث التوكن باستخدام refresh_token (يفترض وجود endpoint refresh)"""
    # قد لا يكون متاحاً، لكن نضيفه كإمكانية
    url = "https://api.webook.com/api/v2/refresh"
    payload = {"refresh_token": refresh_token}
    headers = {"Content-Type": "application/json"}
    proxy_dict = {"http": proxy, "https": proxy} if proxy else None
    try:
        resp = requests.post(url, json=payload, headers=headers, proxies=proxy_dict, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data.get('access_token'), data.get('refresh_token'), time.time() + data.get('expires_in', 3600)
    except:
        pass
    return None, None, None

def ensure_valid_token(account):
    """تتحقق من صلاحية التوكن وتحدثه إذا لزم الأمر"""
    if account['token_expiry'] and time.time() >= account['token_expiry'] - 60:
        new_access, new_refresh, new_expiry = refresh_access_token(account['refresh_token'], account.get('proxy'))
        if new_access:
            account['access_token'] = new_access
            account['refresh_token'] = new_refresh or account['refresh_token']
            account['token_expiry'] = new_expiry
            socketio.emit('log', {'message': f"تم تحديث توكن {account['email']}", 'type': 'info'})
        else:
            # فشل التحديث، نعيد تسجيل الدخول
            access, refresh, expiry = login_to_webook(account['email'], account['password'], account.get('captcha',''), account.get('signature',''), account.get('proxy'))
            if access:
                account['access_token'] = access
                account['refresh_token'] = refresh
                account['token_expiry'] = expiry
                socketio.emit('log', {'message': f"تم إعادة تسجيل دخول {account['email']}", 'type': 'success'})
            else:
                account['status'] = "auth_failed"
                socketio.emit('log', {'message': f"فشل تجديد توكن {account['email']}", 'type': 'error'})
                return False
    return True

# ------------------- دوال SeatCloud API -------------------
def fetch_chart_data(workspace_key, chart_key, account, proxy=None):
    """جلب خريطة المقاعد باستخدام توكن الحساب"""
    url = f"https://api.seatcloud.com/api/v2/{workspace_key}/map/{chart_key}/data"
    headers = {
        "Authorization": f"Bearer {account['access_token']}",
        "Accept": "application/json",
        "User-Agent": HEADERS["User-Agent"]
    }
    proxy_dict = {"http": proxy, "https": proxy} if proxy else None
    try:
        resp = requests.get(url, headers=headers, proxies=proxy_dict, timeout=5)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 401:
            # التوكن منتهي – نحاول تحديثه
            ensure_valid_token(account)
            # إعادة المحاولة مرة واحدة
            resp = requests.get(url, headers=headers, proxies=proxy_dict, timeout=5)
            if resp.status_code == 200:
                return resp.json()
        return None
    except:
        return None

def check_held_items(workspace_key, event_slug, account):
    """التحقق من المقاعد الممسوكة لهذا الحساب (باستخدام hold_token و التوكن)"""
    url = f"https://api.seatcloud.com/api/v2/{workspace_key}/event/{event_slug}/items/held"
    params = {"hold_token": account.get('hold_token', '')}
    headers = {"Authorization": f"Bearer {account['access_token']}", "Accept": "application/json"}
    proxy_dict = {"http": account.get('proxy'), "https": account.get('proxy')} if account.get('proxy') else None
    try:
        resp = requests.get(url, headers=headers, params=params, proxies=proxy_dict, timeout=5)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 429:
            return "rate_limit"
        else:
            return None
    except:
        return None

def hold_seats(workspace_key, event_slug, seat_ids, account):
    """
    إمساك مقاعد محددة.
    يفترض أن الـ endpoint الصحيح هو POST إلى /items/hold مع hold_token و قائمة المقاعد.
    نستخدم التوكن من الحساب.
    """
    url = f"https://api.seatcloud.com/api/v2/{workspace_key}/event/{event_slug}/items/hold"
    payload = {
        "hold_token": account.get('hold_token', str(uuid.uuid4())),
        "seats": [{"seat_id": sid} for sid in seat_ids]   # قد تحتاج للتعديل حسب الـ API الحقيقي
    }
    headers = {
        "Authorization": f"Bearer {account['access_token']}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    proxy_dict = {"http": account.get('proxy'), "https": account.get('proxy')} if account.get('proxy') else None
    try:
        resp = requests.post(url, json=payload, headers=headers, proxies=proxy_dict, timeout=5)
        if resp.status_code in [200, 201]:
            # حفظ hold_token الذي ربما يعاد من السيرفر
            data = resp.json()
            if 'hold_token' in data:
                account['hold_token'] = data['hold_token']
            return True
        else:
            socketio.emit('log', {'message': f"فشل إمساك المقاعد {seat_ids} كود {resp.status_code}", 'type': 'error'})
            return False
    except Exception as e:
        socketio.emit('log', {'message': f"خطأ في طلب الإمساك: {str(e)}", 'type': 'error'})
        return False

# ------------------- دوال السكانر والفاحص -------------------
def extract_available_seats(chart_data, max_seats=2):
    """
    استخراج قائمة المقاعد المتاحة من بيانات الخريطة.
    هذه دالة نموذجية – يجب تعديلها حسب بنية JSON الفعلية.
    نفترض أن chart_data يحتوي على 'sections' أو 'areas' وفي داخلها 'seats' مع خاصية 'available'.
    """
    available = []
    try:
        # مثال: إذا كانت البيانات على شكل قائمة مقاعد
        if isinstance(chart_data, dict):
            # قد تحتوي على مفتاح 'seats' أو 'items'
            seats = chart_data.get('seats', [])
            for seat in seats:
                if seat.get('available', False) and not seat.get('held', False):
                    available.append(seat['id'])
                    if len(available) >= max_seats:
                        break
        else:
            # محاكاة لإظهار الفكرة
            available = [f"seat_{i}" for i in range(random.randint(0,5))]
    except:
        pass
    return available

def run_scanner_for_account(account, stop_event):
    """وضع السكانر – يحاول مسك أي مقاعد متاحة فور ظهورها"""
    email = account['email']
    socketio.emit('log', {'message': f"[{email}] بدأ وضع SCANNER", 'type': 'info'})
    
    while not stop_event.is_set() and bot_running:
        if not ensure_valid_token(account):
            time.sleep(10)
            continue
        
        chart = fetch_chart_data(BASE_CONFIG['WORKSPACE_KEY'], BASE_CONFIG['CHART_KEY'], account, account.get('proxy'))
        if not chart:
            time.sleep(0.5)
            continue
        
        available_seats = extract_available_seats(chart, max_per_account)
        if available_seats:
            socketio.emit('log', {'message': f"[{email}] تم العثور على {len(available_seats)} مقعد متاح: {available_seats}", 'type': 'info'})
            success = hold_seats(BASE_CONFIG['WORKSPACE_KEY'], BASE_CONFIG['EVENT_SLUG'], available_seats[:max_per_account], account)
            if success:
                account['held_seats'] = available_seats[:max_per_account]
                account['hold_expiry'] = time.time() + 600  # 10 دقائق افتراضية
                account['status'] = "hold_success"
                socketio.emit('log', {'message': f"[{email}] ✅ نجاح مسك المقاعد {account['held_seats']}!", 'type': 'success'})
                if sound_alerts:
                    socketio.emit('play_sound')
                socketio.emit('update_accounts', accounts)
                # نوقف السكنر لهذا الحساب بعد النجاح (أو يمكن يستمر لمسك المزيد)
                break
        time.sleep(0.3)  # أقل من نصف ثانية

def run_checker_for_account(account, stop_event):
    """وضع الفاحص – يرصد عدد المقاعد المتاحة فقط ولا يحجز"""
    email = account['email']
    while not stop_event.is_set() and bot_running:
        if not ensure_valid_token(account):
            time.sleep(5)
            continue
        chart = fetch_chart_data(BASE_CONFIG['WORKSPACE_KEY'], BASE_CONFIG['CHART_KEY'], account, account.get('proxy'))
        if chart:
            available = len(extract_available_seats(chart, 999))
            socketio.emit('availability', {'email': email, 'available': available})
        time.sleep(0.5)

# ------------------- واجهات Flask -------------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/config', methods=['POST'])
def save_config():
    global max_per_account, sound_alerts, bot_mode
    data = request.json
    max_per_account = int(data.get('max_per_account', 2))
    sound_alerts = data.get('sound_alerts', True)
    bot_mode = data.get('mode', 'scanner')
    return jsonify({"status": "ok"})

@app.route('/api/accounts', methods=['POST'])
def add_accounts():
    global accounts
    new_accounts = request.json.get('accounts', [])
    proxies = request.json.get('proxies', [])
    for idx, acc_data in enumerate(new_accounts):
        email = acc_data['email']
        password = acc_data['password']
        captcha = acc_data.get('captcha', '')
        signature = acc_data.get('signature', '')
        proxy = proxies[idx % len(proxies)] if proxies else None
        
        # محاولة تسجيل الدخول
        access_token, refresh_token, expiry = login_to_webook(email, password, captcha, signature, proxy)
        if access_token:
            # ننشئ hold_token جديد للحساب (يمكن أن يكون أي UUID)
            hold_token = str(uuid.uuid4())
            accounts.append({
                "email": email,
                "password": password,  # تُحفظ فقط للتوكن، في الإنتاج يفضل تخزين آمن
                "captcha": captcha,
                "signature": signature,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_expiry": expiry,
                "hold_token": hold_token,
                "proxy": proxy,
                "status": "ready",
                "held_seats": [],
                "hold_expiry": 0
            })
            socketio.emit('log', {'message': f"✅ تم إضافة الحساب {email}", 'type': 'success'})
        else:
            socketio.emit('log', {'message': f"❌ فشل تسجيل دخول {email}", 'type': 'error'})
    socketio.emit('update_accounts', accounts)
    return jsonify({"status": "ok", "accounts": accounts})

@app.route('/api/start', methods=['POST'])
def start_bot():
    global bot_running, active_threads, bot_mode
    if bot_running:
        return jsonify({"status": "already running"})
    if not accounts:
        return jsonify({"status": "error", "message": "لا توجد حسابات"})
    bot_running = True
    active_threads = []
    for acc in accounts:
        stop_event = threading.Event()
        if bot_mode == "scanner":
            t = threading.Thread(target=run_scanner_for_account, args=(acc, stop_event))
        else:
            t = threading.Thread(target=run_checker_for_account, args=(acc, stop_event))
        t.daemon = True
        t.start()
        active_threads.append((t, stop_event))
    socketio.emit('log', {'message': f"🚀 تم تشغيل البوت بوضع {bot_mode.upper()}", 'type': 'info'})
    return jsonify({"status": "started"})

@app.route('/api/stop', methods=['POST'])
def stop_bot():
    global bot_running
    bot_running = False
    for _, stop_event in active_threads:
        stop_event.set()
    socketio.emit('log', {'message': "⏹️ تم إيقاف البوت", 'type': 'warning'})
    return jsonify({"status": "stopped"})

@socketio.on('connect')
def handle_connect():
    emit('update_accounts', accounts)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
