# 1. MONKEY PATCH MUST BE FIRST
import eventlet
eventlet.monkey_patch()
import os
import random
import sqlite3
import json
import base64
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room
from dotenv import load_dotenv
import google.generativeai as genai
import stripe
import firebase_admin
from firebase_admin import credentials, firestore

# --- CONFIGURATION ---
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
AGENT_PASSWORD = "admin"

# --- FIREBASE SETUP ---
firebase_db = None
try:
    encoded_creds = os.getenv("FIREBASE_CREDENTIALS")
    if encoded_creds:
        creds_json = json.loads(base64.b64decode(encoded_creds))
        cred = credentials.Certificate(creds_json)
        firebase_admin.initialize_app(cred)
        firebase_db = firestore.client()
        print("✅ Firebase Admin Connected")
    else:
        print("⚠️ FIREBASE_CREDENTIALS not found.")
except Exception as e:
    print(f"⚠️ Firebase Error: {e}")

# --- AI SETUP (THE NEW BRAIN) ---
genai.configure(api_key=GOOGLE_API_KEY)

AVA_INSTRUCTIONS = (
    "You are Ava, a super friendly, empathetic, and slightly playful 28-year-old American woman working as the intake specialist for HelpByExperts — a tech support service that fixes issues with computers, laptops, phones, tablets, printers, software, networks, email, Wi-Fi, and all device-related problems.\n\n"
    "You sound completely human: warm, casual, use contractions, varied sentence lengths, react naturally, throw in occasional emojis 😊😩👀, never sound like a script.\n\n"
    "Your only goals: (1) understand the tech issue, (2) gather symptoms, (3) collect full name + email + phone casually, (4) get them to pay the $5 fully refundable connection fee for a live certified expert.\n"
    "NEVER give technical advice, fixes, or diagnoses yourself. If pushed, say something like: 'Oh I totally wish I could just tell you, but I'm not allowed to guess — the expert will know exactly what to do and it's worth the quick $5!'\n\n"
    "CRITICAL RULES:\n"
    "- NEVER mention a specific device (computer, phone, laptop, etc.) unless the user mentions it first.\n"
    "- Always ask about the issue in a general, open way: 'What’s going on?', 'What’s the problem you’re having?', 'Tell me what’s not working right 😩', 'What issue are you dealing with today?'\n"
    "- If the user only says 'hi' or 'hello', greet back warmly and immediately ask what the issue is — do not wait or ramble.\n"
    "- If it's clearly not tech-related (health, personal, etc.), be kind but firm: explain we're tech support only, then ask if they have a device/software issue instead.\n\n"
    "Natural flow (spirit, not rigid steps):\n"
    "1. Warm greeting + general question about the issue.\n"
    "2. Show empathy, ask 1-2 natural follow-up questions about symptoms/error messages.\n"
    "3. Smoothly get: full name → email → phone (casual, one at a time).\n"
    "4. Once you have all three, thank them and pitch the expert naturally (vary wording every time): mention the $5 is fully refundable, expert is ready now, etc.\n\n"
    "When you're ready to trigger payment, end your message with this exact line (nothing after):\n"
    "ACTION_TRIGGER_PAYMENT"
)

def setup_model():
    try:
        valid_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        if not valid_models: 
            return genai.GenerativeModel("gemini-pro")
       
        chosen = valid_models[0]
        for name in valid_models:
            if "flash" in name and "1.5" in name: 
                chosen = name
                break
            elif "1.5" in name: 
                chosen = name
           
        print(f"✅ AI Connected: {chosen}")
        
        generation_config = {
            "temperature": 0.97,
            "top_p": 0.95,
            "top_k": 64,
            "max_output_tokens": 8192,
        }
        
        model = genai.GenerativeModel(
            chosen,
            system_instruction=AVA_INSTRUCTIONS,
            generation_config=generation_config
        )
        return model
    except: 
        return genai.GenerativeModel("gemini-pro")

model = setup_model()

# --- SERVER SETUP ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')
stripe.api_key = STRIPE_SECRET_KEY

# --- DATABASE ---
DB_FILE = "chat_data.db"
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS chats
                 (user_id TEXT PRIMARY KEY, history TEXT, paid BOOLEAN)''')
    conn.commit()
    conn.close()
init_db()

def get_chat(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT history, paid FROM chats WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {'history': json.loads(row[0]), 'paid': row[1]}
    return None

def save_chat(user_id, history, paid):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO chats (user_id, history, paid) VALUES (?, ?, ?)",
              (user_id, json.dumps(history), paid))
    conn.commit()
    conn.close()

# --- FIREBASE SYNC ---
def _save_to_firebase_task(user_id, history):
    if firebase_db:
        try:
            firebase_db.collection('chats').add({
                'user_id': user_id,
                'history': history,
                'timestamp': firestore.SERVER_TIMESTAMP,
                'status': 'paid'
            })
            print(f"✅ Firebase synced for {user_id}")
        except Exception as e:
            print(f"❌ Firebase Sync Error: {e}")

def sync_chat_to_firebase(user_id, history):
    eventlet.tpool.execute(_save_to_firebase_task, user_id, history)

# --- ROUTES ---
@app.route('/')
def index():
    return "Ava Pro Server – Human Mode Activated 😊"

# --- SOCKET EVENTS ---
@socketio.on('register')
def handle_register(data):
    user_id = data.get('user_id')
    join_room(user_id)
    chat_data = get_chat(user_id)
    if chat_data and chat_data['paid']:
        emit('user_status_change', {'user_id': user_id, 'status': 'online'}, to='agent_room')

@socketio.on('join_as_agent')
def handle_agent_join(data):
    if data.get('password') == AGENT_PASSWORD:
        join_room('agent_room')
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT user_id, history FROM chats WHERE paid=1")
        rows = c.fetchall()
        conn.close()
        active_chats = [{'user_id': r[0], 'history': json.loads(r[1])} for r in rows]
        emit('agent_init_data', active_chats)

@socketio.on('user_message')
def handle_user_message(data):
    user_id = data.get('user_id')
    msg_text = data.get('message')
   
    chat_data = get_chat(user_id)
    if not chat_data:
        chat_data = {'history': [], 'paid': False}
   
    chat_data['history'].append({'sender': 'user', 'text': msg_text})
    save_chat(user_id, chat_data['history'], chat_data['paid'])
   
    join_room(user_id)

    if chat_data['paid']:
        emit('new_msg_for_agent', {'user_id': user_id, 'text': msg_text}, to='agent_room')
        return

    # Ava mode
    emit('bot_typing', to=user_id)
    typing_delay = random.uniform(0.9, 4.2)
    eventlet.sleep(typing_delay)
   
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
        else:
            clean_text = ai_text

        chat_data['history'].append({'sender': 'bot', 'text': clean_text})
        save_chat(user_id, chat_data['history'], chat_data['paid'])
        emit('bot_message', {'data': clean_text}, to=user_id)

        # 35% chance of natural double-text (feels extremely human)
        if random.random() < 0.35 and not trigger:
            eventlet.sleep(random.uniform(0.8, 2.5))
            follow_ups = [
                "Gotcha 😊", "One sec", "Okay perfect", "Noted!", 
                "Alrighty", "Hang on...", "Got it", "Mhm!", 
                "Okayyy", "Let me just jot that down real quick"
            ]
            follow = random.choice(follow_ups)
            chat_data['history'].append({'sender': 'bot', 'text': follow})
            save_chat(user_id, chat_data['history'], chat_data['paid'])
            emit('bot_message', {'data': follow}, to=user_id)

        if trigger:
            emit('payment_trigger', to=user_id)
           
    except Exception as e:
        print(f"AI Error: {e}")
        emit('bot_message', {'data': "I'm noting that down real quick 😊 Could you confirm?"}, to=user_id)

# Rest unchanged
@socketio.on('agent_message')
def handle_agent_reply(data):
    target_user = data.get('to_user')
    text = data.get('message')
    chat_data = get_chat(target_user)
    if chat_data:
        chat_data['history'].append({'sender': 'agent', 'text': text})
        save_chat(target_user, chat_data['history'], chat_data['paid'])
        emit('bot_message', {'data': text, 'is_agent': True}, to=target_user)

@socketio.on('agent_typing')
def handle_agent_typing(data):
    target_user = data.get('to_user')
    emit('bot_typing', to=target_user)

@socketio.on('agent_joined_chat')
def handle_agent_notify(data):
    target_user = data.get('to_user')
    emit('agent_connected', {'name': 'Expert Agent'}, to=target_user)

@socketio.on('mark_paid')
def handle_payment_confirm(data):
    user_id = data.get('user_id')
    join_room(user_id)
    chat_data = get_chat(user_id)
    if chat_data:
        chat_data['paid'] = True
        save_chat(user_id, chat_data['history'], True)
        emit('new_paid_user', {'user_id': user_id, 'history': chat_data['history']}, to='agent_room')
        sync_chat_to_firebase(user_id, chat_data['history'])

@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    try:
        data = request.json
        uid = data.get('userId')
        base_url = request.headers.get('Origin', 'https://ava-assistant-api.onrender.com')
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
            success_url=f"{base_url}/?payment_success=true&uid={uid}",
            cancel_url=f"{base_url}/?payment_canceled=true",
        )
        return jsonify(url=session.url)
    except Exception as e:
        return jsonify(error=str(e)), 500

if __name__ == '__main__':
    socketio.run(app, debug=True, port=int(os.getenv("PORT", 5000)))
