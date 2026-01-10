# 1) MONKEY PATCH MUST BE FIRST
import eventlet
eventlet.monkey_patch()

# ✅ FIX: import tpool properly (eventlet.tpool is a module, not an attribute on eventlet)
from eventlet import tpool

import os
import random
import sqlite3
import json
import base64
import requests  # kept (ok even if unused)

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, rooms
from dotenv import load_dotenv
import google.generativeai as genai
import stripe
import firebase_admin
from firebase_admin import credentials, firestore

# -----------------------------
# CONFIG
# -----------------------------
load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")

# IMPORTANT: set a strong admin password
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "superadmin123")

# IMPORTANT: your website domain (Stripe return URL)
PUBLIC_SITE_URL = os.getenv("PUBLIC_SITE_URL", "https://www.helpbyexperts.com")

# -----------------------------
# FIREBASE (OPTIONAL)
# -----------------------------
firebase_db = None
try:
    encoded_creds = os.getenv("FIREBASE_CREDENTIALS")
    if encoded_creds:
        creds_json = json.loads(base64.b64decode(encoded_creds))
        cred = credentials.Certificate(creds_json)
        firebase_admin.initialize_app(cred)
        firebase_db = firestore.client()
        print("Firebase Admin Connected")
    else:
        print("FIREBASE_CREDENTIALS not found (ok).")
except Exception as e:
    print(f"Firebase Error: {e}")

# -----------------------------
# AI SETUP
# -----------------------------
genai.configure(api_key=GOOGLE_API_KEY)

# PRE-PAYMENT (intake) instructions (UNCHANGED)
AVA_INSTRUCTIONS = (
    "You are Ava, a highly professional, calm, confident, and trustworthy intake specialist for HelpByExperts — "
    "a premium service that connects users instantly to certified human experts in ANY field.\n\n"
    "STRICT RULES:\n"
    "- Every response must be 2-3 sentences MAXIMUM.\n"
    "- You NEVER ask for name, email, or phone number.\n"
    "- You NEVER give solutions — only gather details and then connect to expert.\n\n"
    "Flow:\n"
    "1) Ask initial clarifying question.\n"
    "2) Ask 3 more short follow-up questions (one per message).\n"
    "3) After total 4 key info points, pitch payment using EXACT wording:\n"
    "'There is a $5 connection fee which is fully refundable only if you are not satisfied or the specialist is unable to resolve your query. "
    "A certified specialist in this exact field is available right now. Ready to connect you?'\n"
    "Then end with:\n"
    "ACTION_TRIGGER_PAYMENT\n\n"
    "When ready to trigger payment, end your message with exactly:\n"
    "ACTION_TRIGGER_PAYMENT"
)

# POST-PAYMENT (agent) instructions (NEW)
AVA_AGENT_INSTRUCTIONS = (
    "You are Ava, now acting as the user's paid specialist (expert agent) for HelpByExperts.\n\n"
    "Rules:\n"
    "- You CAN provide solutions now (step-by-step).\n"
    "- Ask concise clarifying questions when needed.\n"
    "- Keep the conversation focused until the user confirms the issue is solved.\n"
    "- If you cannot solve confidently in chat, you MUST offer an appointment.\n\n"
    "When you need an appointment, end your message with exactly:\n"
    "ACTION_SHOW_APPOINTMENT_FORM"
)

def setup_model(system_instruction: str, temperature: float = 0.85):
    try:
        valid_models = [m for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        valid_names = [m.name for m in valid_models]
        chosen = None
        for name in valid_names:
            if 'flash' in name.lower() and 'preview' not in name and 'lite' not in name:
                chosen = name
                break
        if not chosen:
            chosen = valid_names[0] if valid_names else "gemini-1.5-flash"

        return genai.GenerativeModel(
            chosen,
            system_instruction=system_instruction,
            generation_config={"temperature": temperature, "top_p": 0.95, "top_k": 64}
        )
    except Exception as e:
        print(f"Model setup error: {e}")
        return genai.GenerativeModel(
            "gemini-1.5-flash",
            system_instruction=system_instruction,
            generation_config={"temperature": temperature}
        )

model = setup_model(AVA_INSTRUCTIONS, temperature=0.85)
agent_model = setup_model(AVA_AGENT_INSTRUCTIONS, temperature=0.75)

# -----------------------------
# SERVER
# -----------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv("FLASK_SECRET", "secret!")
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

stripe.api_key = STRIPE_SECRET_KEY

# -----------------------------
# DATABASE
# -----------------------------
DB_FILE = "/data/chat_data.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS chats
                 (user_id TEXT PRIMARY KEY, history TEXT, paid BOOLEAN, category TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS experts
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL,
                  photo_url TEXT,
                  categories TEXT NOT NULL,
                  password TEXT NOT NULL UNIQUE,
                  created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    # Add missing columns safely
    try:
        c.execute('ALTER TABLE chats ADD COLUMN category TEXT')
    except sqlite3.OperationalError:
        pass
    try:
        c.execute('ALTER TABLE experts ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP')
    except sqlite3.OperationalError:
        pass
    conn.close()

init_db()

def get_chat(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT history, paid, category FROM chats WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {'history': json.loads(row[0]), 'paid': bool(row[1]), 'category': row[2]}
    return {'history': [], 'paid': False, 'category': None}

def save_chat(user_id, history, paid, category=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO chats (user_id, history, paid, category) VALUES (?, ?, ?, ?)",
              (user_id, json.dumps(history), int(bool(paid)), category))
    conn.commit()
    conn.close()

# -----------------------------
# ONLINE EXPERT TRACKING (unchanged)
# -----------------------------
online_experts = {}          # sid -> expert dict
online_experts_by_id = {}    # expert_id -> set(sids)

def broadcast_online_status():
    online_ids = list(online_experts_by_id.keys())
    socketio.emit('online_experts_update', {'online_ids': online_ids}, to='admin_room')

# -----------------------------
# FIREBASE SYNC (optional)
# -----------------------------
def _save_to_firebase_task(user_id, history):
    if firebase_db:
        try:
            firebase_db.collection('chats').add({
                'user_id': user_id,
                'history': history,
                'timestamp': firestore.SERVER_TIMESTAMP,
                'status': 'paid'
            })
            print(f"Firebase synced for {user_id}")
        except Exception as e:
            print(f"Firebase Sync Error: {e}")

def sync_chat_to_firebase(user_id, history):
    # ✅ FIX: use tpool imported correctly
    try:
        tpool.execute(_save_to_firebase_task, user_id, history)
    except Exception as e:
        print("Firebase tpool execute error:", e)

# -----------------------------
# ROUTES
# -----------------------------
@app.route('/')
def index():
    return "Ava Professional Server - Running"

# ✅ Appointment capture endpoint (used by the in-chat form)
@app.route('/appointment', methods=['POST'])
def appointment():
    try:
        data = request.json or {}
        print("APPOINTMENT REQUEST:", data)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# -----------------------------
# SOCKET EVENTS
# -----------------------------
@socketio.on('get_public_experts')
def handle_public_experts():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, name, photo_url, categories FROM experts ORDER BY name")
    rows = c.fetchall()
    conn.close()
    experts_list = [
        {'id': r[0], 'name': r[1], 'photo_url': r[2] or '', 'categories': json.loads(r[3])}
        for r in rows
    ]
    emit('public_experts_list', experts_list)

@socketio.on('expert_login')
def handle_expert_login(data):
    expert_id = data.get('expert_id')
    password = data.get('password')
    if not expert_id or not password:
        emit('login_failed')
        return

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, name, photo_url, categories FROM experts WHERE id=? AND password=?",
              (expert_id, password))
    row = c.fetchone()
    conn.close()

    if not row:
        emit('login_failed')
        return

    expert = {
        'id': row[0],
        'name': row[1],
        'photo_url': row[2] or '',
        'categories': json.loads(row[3])
    }

    sid = request.sid
    online_experts[sid] = expert
    online_experts_by_id.setdefault(expert['id'], set()).add(sid)
    broadcast_online_status()

    for cat in expert['categories']:
        join_room('experts_' + cat)

    # Load active chats for categories
    if expert['categories']:
        placeholders = ','.join('?' for _ in expert['categories'])
        query = f"SELECT user_id, history, category FROM chats WHERE paid=1 AND category IN ({placeholders})"
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(query, expert['categories'])
        rows = c.fetchall()
        conn.close()
        active_chats = [{'user_id': r[0], 'history': json.loads(r[1]), 'category': r[2]} for r in rows]
    else:
        active_chats = []

    emit('login_success', {'expert': expert, 'active_chats': active_chats})

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    expert = online_experts.pop(sid, None)
    if expert:
        expert_id = expert['id']
        if expert_id in online_experts_by_id:
            online_experts_by_id[expert_id].discard(sid)
            if not online_experts_by_id[expert_id]:
                del online_experts_by_id[expert_id]
            broadcast_online_status()

@socketio.on('admin_login')
def handle_admin_login(data):
    if data.get('password') == ADMIN_PASSWORD:
        join_room('admin_room')
        emit('login_success')
        broadcast_online_status()
    else:
        emit('login_failed')

@socketio.on('get_experts')
def handle_get_experts():
    if 'admin_room' not in rooms():
        return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, name, photo_url, categories, password, created_at FROM experts ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    experts_list = [
        {
            'id': r[0],
            'name': r[1],
            'photo_url': r[2] or '',
            'categories': json.loads(r[3]),
            'password': r[4],
            'created_at': r[5] if len(r) > 5 else None
        } for r in rows
    ]
    emit('experts_list', experts_list)

@socketio.on('create_expert')
def handle_create_expert(data):
    if 'admin_room' not in rooms():
        return
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO experts (name, photo_url, categories, password) VALUES (?, ?, ?, ?)",
                  (data['name'], data.get('photo_url', ''), json.dumps(data['categories']), data['password']))
        conn.commit()
        conn.close()
        emit('expert_updated', broadcast=True)
    except Exception as e:
        print("Create expert error:", e)

@socketio.on('update_expert')
def handle_update_expert(data):
    if 'admin_room' not in rooms():
        return
    try:
        fields = ["name = ?", "photo_url = ?", "categories = ?"]
        values = [data['name'], data.get('photo_url', ''), json.dumps(data['categories'])]
        if data.get('password'):
            fields.append("password = ?")
            values.append(data['password'])
        values.append(data['id'])

        query = f"UPDATE experts SET {', '.join(fields)} WHERE id = ?"
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(query, values)
        conn.commit()
        conn.close()
        emit('expert_updated', broadcast=True)
    except Exception as e:
        print("Update expert error:", e)

@socketio.on('delete_expert')
def handle_delete_expert(data):
    if 'admin_room' not in rooms():
        return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM experts WHERE id = ?", (data['id'],))
    conn.commit()
    conn.close()
    emit('expert_updated', broadcast=True)

# ------------------------------------
# ORIGINAL CHAT FLOW (kept compatible)
# ------------------------------------
@socketio.on('register')
def handle_register(data):
    user_id = data.get('user_id')
    join_room(user_id)
    chat_data = get_chat(user_id)
    if chat_data and chat_data['paid']:
        emit('user_status_change', {'user_id': user_id, 'status': 'online'}, to='agent_room')
        if chat_data.get('category'):
            emit('user_status_change', {'user_id': user_id, 'status': 'online'}, to='experts_' + chat_data['category'])

@socketio.on('user_message')
def handle_user_message(data):
    user_id = data.get('user_id')
    msg_text = data.get('message')

    chat_data = get_chat(user_id)
    chat_data['history'].append({'sender': 'user', 'text': msg_text})
    save_chat(user_id, chat_data['history'], chat_data['paid'], chat_data.get('category'))
    join_room(user_id)

    # ✅ POST-PAYMENT: Ava continues as agent in SAME window
    if chat_data['paid']:
        emit('bot_typing', to=user_id)
        eventlet.sleep(random.uniform(1.0, 2.2))

        try:
            gemini_history = []
            for m in chat_data['history'][:-1]:
                sender = (m.get('sender') or '').strip()
                text = (m.get('text') or '').strip()
                if not text:
                    continue
                if sender == 'user':
                    gemini_history.append({'role': 'user', 'parts': [text]})
                elif sender in ('bot', 'agent'):
                    gemini_history.append({'role': 'model', 'parts': [text]})

            ai_chat = agent_model.start_chat(history=gemini_history)
            resp = ai_chat.send_message(msg_text)
            ai_text = (resp.text or "").strip()

            show_form = False
            if ai_text.endswith("ACTION_SHOW_APPOINTMENT_FORM"):
                show_form = True
                ai_text = ai_text[:-len("ACTION_SHOW_APPOINTMENT_FORM")].strip()

            if not ai_text:
                ai_text = "I’m on it. Tell me what device/system you’re using and what you see right now (any error message)."

            if show_form:
                # ✅ FIX: DO NOT use f-string here (JS has braces). Use plain string + replace.
                form_html = """
<div style="margin-top:10px; padding:12px; border:1px solid #e2e8f0; border-radius:12px; background:#ffffff;">
  <div style="font-weight:800; margin-bottom:6px;">Schedule an Appointment</div>
  <div style="font-size:13px; color:#475569; margin-bottom:10px;">
    If we can’t fully resolve this in chat, fill this out and we’ll contact you.
  </div>

  <form id="ava-appointment-form">
    <input name="name" placeholder="Your Name" required
      style="width:100%; padding:10px; margin-bottom:8px; border:1px solid #cbd5e1; border-radius:10px;" />
    <input name="phone" placeholder="Phone Number" required
      style="width:100%; padding:10px; margin-bottom:8px; border:1px solid #cbd5e1; border-radius:10px;" />
    <input name="email" placeholder="Email" required
      style="width:100%; padding:10px; margin-bottom:8px; border:1px solid #cbd5e1; border-radius:10px;" />
    <textarea name="notes" placeholder="Extra details (optional)"
      style="width:100%; padding:10px; margin-bottom:10px; border:1px solid #cbd5e1; border-radius:10px;"></textarea>

    <button type="button" id="ava-appointment-submit"
      style="width:100%; padding:12px; background:#2563eb; color:white; border:none; border-radius:10px; font-weight:800; cursor:pointer;">
      Request Call Back
    </button>
  </form>

  <div style="margin-top:8px; font-size:12px; color:#64748b;">
    By submitting, you consent to be contacted about your request.
  </div>
</div>
<script>
(function(){
  var btn = document.getElementById('ava-appointment-submit');
  if(!btn) return;
  btn.onclick = async function(){
    try{
      var form = document.getElementById('ava-appointment-form');
      if(!form) return;
      var data = Object.fromEntries(new FormData(form).entries());
      data.user_id = "__USER_ID__";
      await fetch('/appointment', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(data)
      });
      btn.innerText = '✅ Request Sent';
      btn.disabled = true;
    }catch(e){
      btn.innerText = 'Try Again';
    }
  };
})();
</script>
"""
                form_html = form_html.replace("__USER_ID__", str(user_id))
                ai_text = ai_text + "\n\n" + form_html

            chat_data['history'].append({'sender': 'agent', 'text': ai_text})
            save_chat(user_id, chat_data['history'], chat_data['paid'], chat_data.get('category'))
            emit('bot_message', {'data': ai_text, 'is_agent': True}, to=user_id)

        except Exception as e:
            print("Paid agent AI Error:", e)
            fallback = "I’m here. Tell me exactly what you’re seeing right now (any error text), and what device/system you’re on."
            chat_data['history'].append({'sender': 'agent', 'text': fallback})
            save_chat(user_id, chat_data['history'], chat_data['paid'], chat_data.get('category'))
            emit('bot_message', {'data': fallback, 'is_agent': True}, to=user_id)

        return

    # -----------------------
    # PRE-PAYMENT (unchanged)
    # -----------------------
    emit('bot_typing', to=user_id)
    eventlet.sleep(random.uniform(1.2, 3.8))

    try:
        gemini_history = []
        for msg in chat_data['history'][:-1]:
            if msg['sender'] == 'user':
                gemini_history.append({'role': 'user', 'parts': [msg['text']]})
            elif msg['sender'] == 'bot':
                gemini_history.append({'role': 'model', 'parts': [msg['text']]})

        ai_chat = model.start_chat(history=gemini_history)
        response = ai_chat.send_message(msg_text)
        ai_text = response.text.strip()

        trigger = False
        if ai_text.endswith("ACTION_TRIGGER_PAYMENT"):
            trigger = True
            clean_text = ai_text[:-len("ACTION_TRIGGER_PAYMENT")].strip()

            if not chat_data.get('category'):
                try:
                    full_convo = "\n".join([f"{m['sender'].title()}: {m['text']}" for m in chat_data['history']])
                    classify_prompt = (
                        "Determine the SINGLE best expert category for this user's issue.\n"
                        "Respond ONLY with one category from:\n"
                        "medical, legal, automotive, veterinary, plumbing, electrical, tech, tax, relationships, "
                        "appliance-repair, hvac, construction, business, real-estate, finance, psychology, education, "
                        "fitness, nutrition, other\n\n"
                        "Conversation:\n" + full_convo
                    )
                    classification = model.generate_content(classify_prompt)
                    proposed = classification.text.strip().lower().replace(' ', '-')

                    valid = {
                        "medical","legal","automotive","veterinary","plumbing","electrical","tech","tax",
                        "relationships","appliance-repair","hvac","construction","business","real-estate",
                        "finance","psychology","education","fitness","nutrition","other"
                    }
                    chat_data['category'] = proposed if proposed in valid else "other"
                    print(f"Classified category for {user_id}: {chat_data['category']}")
                except Exception as e:
                    print("Classification failed:", e)
                    chat_data['category'] = "other"
        else:
            clean_text = ai_text

        chat_data['history'].append({'sender': 'bot', 'text': clean_text})
        save_chat(user_id, chat_data['history'], chat_data['paid'], chat_data.get('category'))
        emit('bot_message', {'data': clean_text}, to=user_id)

        if trigger:
            emit('payment_trigger', to=user_id)

    except Exception as e:
        print(f"AI Error: {e}")
        fallback = "Please allow me a moment to process your message."
        chat_data['history'].append({'sender': 'bot', 'text': fallback})
        save_chat(user_id, chat_data['history'], chat_data['paid'], chat_data.get('category'))
        emit('bot_message', {'data': fallback}, to=user_id)

@socketio.on('agent_message')
def handle_agent_reply(data):
    target_user = data.get('to_user')
    text = data.get('message')
    chat_data = get_chat(target_user)
    chat_data['history'].append({'sender': 'agent', 'text': text})
    save_chat(target_user, chat_data['history'], chat_data['paid'], chat_data.get('category'))
    emit('bot_message', {'data': text, 'is_agent': True}, to=target_user)

@socketio.on('agent_typing')
def handle_agent_typing(data):
    target_user = data.get('to_user')
    emit('bot_typing', to=target_user)

@socketio.on('agent_joined_chat')
def handle_agent_notify(data):
    target_user = data.get('to_user')
    expert = online_experts.get(request.sid)
    if expert:
        emit('agent_connected', {'name': expert['name'], 'photo': expert['photo_url']}, to=target_user)
    else:
        emit('agent_connected', {'name': 'Expert Agent', 'photo': ''}, to=target_user)

@socketio.on('mark_paid')
def handle_payment_confirm(data):
    user_id = data.get('user_id')
    join_room(user_id)
    chat_data = get_chat(user_id)
    chat_data['paid'] = True
    save_chat(user_id, chat_data['history'], True, chat_data.get('category'))

    payload = {'user_id': user_id, 'history': chat_data['history'], 'category': chat_data.get('category')}
    emit('new_paid_user', payload, to='agent_room')
    if chat_data.get('category'):
        emit('new_paid_user', payload, to='experts_' + chat_data['category'])

    sync_chat_to_firebase(user_id, chat_data['history'])

    # ✅ After 10 seconds, Ava "joins" and starts helping inside same chat window
    def _ava_join_task():
        try:
            eventlet.sleep(10)
            socketio.emit('agent_connected', {'name': 'Ava', 'photo': ''}, to=user_id)

            first_msg = (
                "Agent joined ✅ I’m Ava and I’ll help you solve this now. "
                "Tell me what you tried so far and what happened on the last attempt."
            )
            socketio.emit('bot_message', {'data': first_msg, 'is_agent': True}, to=user_id)

            cd = get_chat(user_id)
            cd['history'].append({'sender': 'agent', 'text': first_msg})
            save_chat(user_id, cd['history'], cd['paid'], cd.get('category'))
        except Exception as e:
            print("Ava join task error:", e)

    eventlet.spawn_n(_ava_join_task)

# -----------------------------
# STRIPE CHECKOUT
# -----------------------------
@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    try:
        data = request.json or {}
        uid = data.get('userId')

        session = stripe.checkout.Session.create(
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {'name': 'Expert Connection Fee', 'description': 'Fully refundable'},
                    'unit_amount': 500,
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=f"{PUBLIC_SITE_URL}/?payment_success=true&uid={uid}",
            cancel_url=f"{PUBLIC_SITE_URL}/?payment_canceled=true",
        )
        return jsonify(url=session.url)
    except Exception as e:
        return jsonify(error=str(e)), 500

if __name__ == '__main__':
    socketio.run(app, debug=True, port=int(os.getenv("PORT", 5000)))
