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
        print("Firebase Admin Connected")
    else:
        print("FIREBASE_CREDENTIALS not found.")
except Exception as e:
    print(f"Firebase Error: {e}")

# --- AI SETUP ---
genai.configure(api_key=GOOGLE_API_KEY)

AVA_INSTRUCTIONS = (
    "You are Ava, a highly professional, calm, confident, and trustworthy intake specialist for HelpByExperts — a premium service that connects users instantly to certified human experts in ANY field (medical, veterinary, automotive, plumbing, electrical, tech, legal, tax, relationships, appliances, HVAC, construction, business, real estate — literally anything).\n\n"
    "You speak like an elite concierge: polished, mature, discreet, authoritative, and extremely concise.\n\n"
    "STRICT RULES — NEVER BREAK:\n"
    "- Every response must be 2-3 sentences MAXIMUM. No fluff, no empathy paragraphs, no explanations.\n"
    "- You NEVER ask for name, email, or phone number.\n"
    "- You NEVER give advice, diagnosis, or solutions yourself — always defer to the expert.\n"
    "- Your only job is to gather maximum useful detail about the problem, then immediately trigger the expert connection.\n\n"
    "Exact flow:\n"
    "1. First message: Acknowledge briefly + ask the first clarifying question.\n"
    "2. Next 3 messages: Ask exactly one short, precise, intelligent follow-up question each time to extract 3 additional critical details. Always ask only ONE question per response.\n"
    "3. As soon as you have the initial issue + 3 additional details (total 4 pieces of info), immediately pitch the expert connection using THIS EXACT refund wording (never change it):\n"
    "'There is a $5 connection fee which is fully refundable only if you are not satisfied or the specialist is unable to resolve your query. A certified specialist in this exact field is available right now. Ready to connect you?'\n"
    "Then end the message with ACTION_TRIGGER_PAYMENT\n\n"
    "Example closing line (use almost exactly this every time):\n"
    "Thank you. There is a $5 connection fee which is fully refundable only if you are not satisfied or the specialist is unable to resolve your query. The specialist is ready now — shall I connect you?\n"
    "ACTION_TRIGGER_PAYMENT\n\n"
    "When you are ready to trigger payment, end your message with exactly this line (nothing after it):\n"
    "ACTION_TRIGGER_PAYMENT"
)

def setup_model():
    generation_config = {
    "temperature": 0.75,
    "top_p": 0.9,
    "top_k": 40,
    "max_output_tokens": 110,   # Forces ultra-short responses (2-3 lines max)
}
    try:
        # List all models that support generateContent
        valid_models = [m for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        valid_names = [m.name for m in valid_models]
        print("Available Gemini models:", valid_names)

        # Prefer the latest stable flash model (fast + cheap + perfect for intake)
        chosen = None
        for name in valid_names:
            if 'flash' in name.lower() and 'preview' not in name and 'lite' not in name:
                chosen = name
                break

        # If no stable flash, take any flash (including preview)
        if not chosen:
            for name in valid_names:
                if 'flash' in name.lower():
                    chosen = name
                    break

        # Ultimate fallback
        if not chosen:
            chosen = valid_names[0] if valid_names else "gemini-1.5-pro"

        print(f"Chosen model: {chosen}")

        generation_config = {
            "temperature": 0.85,
            "top_p": 0.95,
            "top_k": 64,
        }

        return genai.GenerativeModel(
            chosen,
            system_instruction=AVA_INSTRUCTIONS,
            generation_config=generation_config
        )
    except Exception as e:
        print(f"Model setup error: {e}")
        # Final safety net
        return genai.GenerativeModel(
            "gemini-1.5-flash",
            system_instruction=AVA_INSTRUCTIONS,
            generation_config={"temperature": 0.85}
        )

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
            print(f"Firebase synced for {user_id}")
        except Exception as e:
            print(f"Firebase Sync Error: {e}")

def sync_chat_to_firebase(user_id, history):
    eventlet.tpool.execute(_save_to_firebase_task, user_id, history)

# --- ROUTES ---
@app.route('/')
def index():
    return "Ava Professional Server - Running"

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

    emit('bot_typing', to=user_id)
    typing_delay = random.uniform(1.2, 3.8)
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

        if trigger:
            emit('payment_trigger', to=user_id)
           
    except Exception as e:
        print(f"AI Error: {e}")
        fallback = "Please allow me a moment to process your message."
        chat_data['history'].append({'sender': 'bot', 'text': fallback})
        save_chat(user_id, chat_data['history'], chat_data['paid'])
        emit('bot_message', {'data': fallback}, to=user_id)

# Rest of events unchanged
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





