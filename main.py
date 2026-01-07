import sqlite3
import secrets
import json
import requests
import time
import re
import os
import sys
import asyncio
from datetime import datetime
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from curl_cffi import requests as c_requests 
import uvicorn
import os
from dotenv import load_dotenv


load_dotenv()
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
SECRET_KEY = os.getenv("SECRET_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")


ADMIN_IDS = os.getenv("ADMIN_IDS", "").split(",")

# ==========================================
# 🚀 SYSTEM SETUP
# ==========================================
app = FastAPI(title="TrueMoney Redeem Pro")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
templates = Jinja2Templates(directory="templates")

DB_NAME = "truemoney_pro.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # Table Users
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  discord_id TEXT UNIQUE,
                  username TEXT,
                  avatar_url TEXT,
                  api_key TEXT UNIQUE,
                  webhook_url TEXT,
                  line_token TEXT,
                  total_earned REAL DEFAULT 0,
                  is_banned INTEGER DEFAULT 0, 
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Migration
    try: c.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
    except: pass 
    try: c.execute("ALTER TABLE users ADD COLUMN line_token TEXT")
    except: pass

    # Table Transactions
    c.execute('''CREATE TABLE IF NOT EXISTS transactions
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  voucher_code TEXT,
                  phone_number TEXT,
                  amount REAL,
                  status TEXT,
                  message TEXT,
                  timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

init_db()

# --- Helper Functions ---
def get_current_user(request: Request):
    user_data = request.session.get("user")
    if not user_data: return None
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE discord_id = ?", (user_data['id'],))
    user = c.fetchone()
    conn.close()
    return dict(user) if user else None

def is_admin(user_discord_id: str):
    return user_discord_id in ADMIN_IDS

def send_discord_webhook(webhook_url, amount, phone, sender, balance):
    if not webhook_url: return
    embed = {
        "title": "💰 เงินเข้าจ้า!! (Money In)",
        "color": 5763719,
        "fields": [
            {"name": "💵 จำนวน", "value": f"`{amount} THB`", "inline": True},
            {"name": "📱 เบอร์", "value": f"`{phone}`", "inline": True},
            {"name": "👤 ผู้ส่ง", "value": f"`{sender}`", "inline": True},
            {"name": "🏦 ยอดสะสม", "value": f"`{balance} THB`", "inline": False}
        ],
        "footer": {"text": "TrueMoney Redeem System"},
        "timestamp": datetime.utcnow().isoformat()
    }
    try: requests.post(webhook_url, json={"embeds": [embed]}, timeout=3)
    except: pass

def send_line_push(user_id, amount, phone, sender):
    """ส่งแจ้งเตือนผ่าน LINE Messaging API"""
    if not user_id or not LINE_CHANNEL_ACCESS_TOKEN: return
    
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    msg_text = f"💰 เงินเข้า: {amount} บาท\n📱 เบอร์: {phone}\n👤 จาก: {sender}"
    payload = {
        "to": user_id,
        "messages": [{"type": "text", "text": msg_text}]
    }
    try: requests.post(url, headers=headers, json=payload, timeout=5)
    except Exception as e: print(f"LINE Push Error: {e}")

# ==========================================
# 🤖 LINE WEBHOOK (ตอบกลับ User ID)
# ==========================================
@app.post("/line/webhook")
async def line_webhook(request: Request):
    try:
        body = await request.json()
        events = body.get("events", [])
        for event in events:
            if event.get("type") == "message":
                reply_token = event.get("replyToken")
                user_id = event.get("source", {}).get("userId")
                if reply_token and user_id:
                    reply_url = "https://api.line.me/v2/bot/message/reply"
                    headers = {
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
                    }
                    payload = {
                        "replyToken": reply_token,
                        "messages": [{
                            "type": "text", 
                            "text": f"🆔 User ID ของคุณคือ:\n{user_id}\n(นำไปใส่ในเว็บได้เลยครับ)"
                        }]
                    }
                    requests.post(reply_url, headers=headers, json=payload)
        return "OK"
    except Exception as e:
        print(f"Webhook Error: {e}")
        return "Error"

# ==========================================
# 🔐 AUTHENTICATION
# ==========================================
@app.get("/login")
def login():
    return RedirectResponse(
        f"https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&redirect_uri={DISCORD_REDIRECT_URI}&response_type=code&scope=identify"
    )

@app.get("/callback")
def callback(code: str, request: Request):
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    token_res = requests.post("https://discord.com/api/oauth2/token", data=data, headers=headers)
    token_json = token_res.json()

    if "access_token" not in token_json:
        return JSONResponse(content={"error": "Login Failed", "details": token_json}, status_code=400)

    access_token = token_json["access_token"]
    user_res = requests.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {access_token}"})
    user_data = user_res.json()

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    avatar = f"https://cdn.discordapp.com/avatars/{user_data['id']}/{user_data['avatar']}.png"
    
    c.execute("SELECT * FROM users WHERE discord_id = ?", (user_data['id'],))
    existing = c.fetchone()
    
    if not existing:
        new_key = secrets.token_hex(16)
        c.execute("INSERT INTO users (discord_id, username, avatar_url, api_key, is_banned) VALUES (?, ?, ?, ?, 0)",
                  (user_data['id'], user_data['username'], avatar, new_key))
    else:
        c.execute("UPDATE users SET username=?, avatar_url=? WHERE discord_id=?", 
                  (user_data['username'], avatar, user_data['id']))
    
    conn.commit()
    conn.close()
    request.session["user"] = user_data
    return RedirectResponse("/dashboard")

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")

# ==========================================
# 🖥️ DASHBOARD & ACTIONS
# ==========================================
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if get_current_user(request): return RedirectResponse("/dashboard")
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = get_current_user(request)
    if not user: return RedirectResponse("/")
    if user['is_banned']:
        request.session.clear()
        return HTMLResponse("<h1>🚫 BANNED</h1>", status_code=403)
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT 20", (user['id'],))
    txs = [dict(row) for row in c.fetchall()] 
    c.execute("SELECT COUNT(*), SUM(amount) FROM transactions WHERE user_id = ? AND status='SUCCESS'", (user['id'],))
    stats = c.fetchone()
    conn.close()

    return templates.TemplateResponse("dashboard.html", {
        "request": request, "user": user, "txs": txs,
        "total_tx": stats[0] or 0, "total_amount": stats[1] or 0,
        "base_url": str(request.base_url).rstrip("/"),
        "is_admin": is_admin(user['discord_id'])
    })

@app.post("/reset_key")
def reset_key_route(request: Request):
    user = get_current_user(request)
    if not user: return RedirectResponse("/")
    new_key = secrets.token_hex(16)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET api_key = ? WHERE id = ?", (new_key, user['id']))
    conn.commit()
    conn.close()
    return RedirectResponse("/dashboard", status_code=303)

@app.post("/update_notify")
async def update_notify(request: Request):
    user = get_current_user(request)
    if not user: return RedirectResponse("/")
    form = await request.form()
    webhook_url = form.get("webhook_url", "").strip()
    line_token = form.get("line_token", "").strip()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET webhook_url = ?, line_token = ? WHERE id = ?", (webhook_url, line_token, user['id']))
    conn.commit()
    conn.close()
    return RedirectResponse("/dashboard", status_code=303)

# 🧪 TEST NOTIFICATION (UPDATED: Show Errors)
@app.post("/test_notify")
async def test_notify(request: Request):
    user = get_current_user(request)
    if not user: return JSONResponse({"status": "error", "message": "Unauthorized"}, 401)
    
    form = await request.form()
    webhook_url = form.get("webhook_url", "").strip()
    line_user_id = form.get("line_token", "").strip()
    
    triggered = []
    errors = []
    
    # Test Discord
    if webhook_url:
        try:
            embed = {"title": "🔔 Test Discord", "description": "OK!", "color": 5763719}
            r = requests.post(webhook_url, json={"embeds": [embed]}, timeout=5)
            if r.status_code in [200, 204]: triggered.append("Discord")
            else: errors.append(f"Discord: {r.status_code}")
        except Exception as e: errors.append(f"Discord: {str(e)}")

    # Test LINE Messaging API
    if line_user_id:
        if not LINE_CHANNEL_ACCESS_TOKEN or "วาง_" in LINE_CHANNEL_ACCESS_TOKEN:
             errors.append("LINE: ยังไม่ได้ใส่ Access Token ในไฟล์ main.py")
        else:
            try:
                url = "https://api.line.me/v2/bot/message/push"
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
                }
                payload = {
                    "to": line_user_id,
                    "messages": [{"type": "text", "text": "🔔 ทดสอบ: LINE Messaging API ใช้งานได้แล้ว!"}]
                }
                res = requests.post(url, headers=headers, json=payload, timeout=5)
                
                if res.status_code == 200:
                    triggered.append("LINE")
                else:
                    try: detail = res.json().get('message', res.text)
                    except: detail = res.text
                    errors.append(f"LINE Error ({res.status_code}): {detail}")
            except Exception as e:
                errors.append(f"LINE Connection: {str(e)}")

    if triggered:
        msg = f"ส่งสำเร็จ: {', '.join(triggered)}"
        if errors: msg += f" | ปัญหา: {'; '.join(errors)}"
        return JSONResponse({"status": "success", "message": msg})
    else:
        msg = "; ".join(errors) if errors else "กรุณากรอกข้อมูลก่อนทดสอบ"
        return JSONResponse({"status": "error", "message": msg})

# ==========================================
# 💰 API REDEEM
# ==========================================
@app.get("/{api_key}/redeem")
def api_redeem(api_key: str, link: str = "", phone: str = ""):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, webhook_url, total_earned, is_banned, line_token FROM users WHERE api_key = ?", (api_key,))
    user = c.fetchone()
    
    if not user:
        conn.close()
        return JSONResponse({"status": "error", "message": "API Key Invalid"}, status_code=401)
    if user['is_banned']:
        conn.close()
        return JSONResponse({"status": "error", "message": "BANNED"}, status_code=403)
    
    match = re.search(r'v=([a-zA-Z0-9]+)', link)
    voucher = match.group(1) if match else re.sub(r'[^a-zA-Z0-9]', '', link)
    
    url = f"https://gift.truemoney.com/campaign/vouchers/{voucher}/redeem"
    payload = {"mobile": phone, "voucher_hash": voucher}
    headers = {"Content-Type": "application/json"}
    
    status_text = "FAILED"
    amount = 0.0
    msg = "Unknown Error"
    
    try:
        res = c_requests.post(url, json=payload, headers=headers, impersonate="chrome120", timeout=15)
        data = res.json()
        
        if res.status_code == 200 and data.get("status", {}).get("code") == "SUCCESS":
            status_text = "SUCCESS"
            amount = float(data["data"]["my_ticket"]["amount_baht"])
            sender = data["data"]["owner_profile"]["full_name"]
            msg = f"ได้รับ {amount} บาท จาก {sender}"
            
            new_total = user['total_earned'] + amount
            c.execute("UPDATE users SET total_earned = ? WHERE id = ?", (new_total, user['id']))
            
            send_discord_webhook(user['webhook_url'], amount, phone, sender, new_total)
            send_line_push(user['line_token'], amount, phone, sender)
            
        else:
            msg = data.get("status", {}).get("message", "Redeem Failed")

    except Exception as e:
        msg = str(e)

    c.execute("INSERT INTO transactions (user_id, voucher_code, phone_number, amount, status, message) VALUES (?, ?, ?, ?, ?, ?)",
              (user['id'], voucher, phone, amount, status_text, msg))
    conn.commit()
    conn.close()
    return {"status": status_text, "amount": amount, "message": msg}

# ==========================================
# 🛡️ ADMIN PANEL
# ==========================================
@app.get("/admin", response_class=HTMLResponse)
def admin_panel(request: Request):
    user = get_current_user(request)
    if not user or not is_admin(user['discord_id']): return RedirectResponse("/")
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users ORDER BY created_at DESC")
    all_users = c.fetchall()
    c.execute("SELECT SUM(amount) FROM transactions WHERE status='SUCCESS'")
    total_sys = c.fetchone()[0] or 0
    conn.close()
    return templates.TemplateResponse("admin.html", {
        "request": request, "user": user, 
        "users_list": all_users, "total_system_money": total_sys
    })

@app.post("/admin/toggle_ban")
async def admin_toggle_ban(request: Request, user_id: int = Form(...)):
    user = get_current_user(request)
    if not user or not is_admin(user['discord_id']): return JSONResponse({"error": "Unauthorized"}, 403)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT is_banned, discord_id FROM users WHERE id = ?", (user_id,))
    target = c.fetchone()
    if target:
        if target[1] in ADMIN_IDS: return JSONResponse({"error": "Cannot ban admin"}, 400)
        new_status = 0 if target[0] == 1 else 1
        c.execute("UPDATE users SET is_banned = ? WHERE id = ?", (new_status, user_id))
        conn.commit()
    conn.close()
    return RedirectResponse("/admin", status_code=303)
@app.post("/admin/reset_key")
async def admin_reset_key(request: Request, user_id: int = Form(...)):
    user = get_current_user(request)
    if not user or not is_admin(user['discord_id']): return JSONResponse({"error": "Unauthorized"}, 403)
    
    new_key = secrets.token_hex(16)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET api_key = ? WHERE id = ?", (new_key, user_id))
    conn.commit()
    conn.close()
    
    return RedirectResponse("/admin", status_code=303)
@app.post("/admin/shutdown")
async def admin_shutdown(request: Request):
    user = get_current_user(request)
    if not user or not is_admin(user['discord_id']): return JSONResponse({"error": "Unauthorized"}, 403)
    async def shutdown_task():
        await asyncio.sleep(1)
        os._exit(0)
    asyncio.create_task(shutdown_task())
    return {"status": "Server shutting down..."}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3000)