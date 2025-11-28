# 1. MONKEY PATCH MUST BE FIRST
import eventlet
eventlet.monkey_patch()

import os
import time
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
AGENT_PASSWORD = "admin"  # Change this for production security

# --- FIREBASE SETUP (SECURE ENV VAR) ---
# This reads the Base64 string you added to Render, preventing GitHub leaks
firebase_db = None
try:
    encoded_creds = os.getenv("FIREBASE_CREDENTIALS")
    if encoded_creds:
        creds_json = json.loads(base64.b64decode(encoded_creds))
        cred = credentials.Certificate(creds_json)
        firebase_admin.initialize_app(cred)
        firebase_db = firestore.client()
        print("✅ Firebase Admin Connected via Environment Variable")
    else:
        print("⚠️ FIREBASE_CREDENTIALS not found. Chat history won't sync to Account Dashboard.")
except Exception as e:
    print(f"⚠️ Firebase Error: {e}")

# --- AI SETUP ---
genai.configure(api_key=GOOGLE_API_KEY)
AVA_INSTRUCTIONS = (
    "You are 'Ava,' a professional, blonde American assistant for 'HelpByExperts.' "
    "Your goal is to gather detailed information before connecting the user to a human expert. "
    "You MUST follow this strict process:\n"
    "1. Start with a friendly, professional greeting.\n"
    "2. Ask exactly 5 relevant follow-up questions, ONE BY ONE, to understand the user's issue deeply. Do not ask them all at once.\n"
    "3. Wait for the user's answer after each question.\n"
    "4. After the user answers your 5th question, you must summarize their issue and say: "
    "'Thank you. I have gathered all the details. I have identified the perfect expert to solve this immediately. The next step is a secure connection for a $5 fee, which is fully refundable if the expert cannot solve your problem or if you are unsatisfied.' "
    "5. AT THE VERY END of that final message, you MUST include this exact code: ACTION_TRIGGER_PAYMENT"
)

# Auto-Select Working Model
def setup_model():
    print("--- 🤖 AVA INITIALIZATION: Searching for a working AI model ---")
    try:
        valid_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        if not valid_models: return genai.GenerativeModel("gemini-pro")
        
        # Prefer 1.5 Flash or Pro
        chosen = valid_models[0]
        for name in valid_models:
            if "flash" in name and "1.5" in name: chosen = name; break
            elif "1.5" in name: chosen = name
            
        print(f"✅ AI Connected: {chosen}")
        if "1.5" in chosen: return genai.GenerativeModel(chosen, system_instruction=AVA_INSTRUCTIONS)
        return genai.GenerativeModel(chosen)
    except: return genai.GenerativeModel("gemini-pro")

model = setup_model()

# --- SERVER SETUP ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')
stripe.api_key = STRIPE_SECRET_KEY

# --- LOCAL DATABASE (SQLite) ---
# Prevents "Amnesia" if Render restarts
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

# --- FIREBASE SYNC HELPER ---
def sync_chat_to_firebase(user_id, history):
    if firebase_db:
        try:
            # Sync to Firestore so it appears in Account Dashboard
            firebase_db.collection('chats').add({
                'user_id': user_id,
                'history': history,
                'timestamp': firestore.SERVER_TIMESTAMP,
                'status': 'paid'
            })
            print(f"✅ Firebase: Chat synced for {user_id}")
        except Exception as e:
            print(f"❌ Firebase Sync Error: {e}")

# --- ROUTES ---
@app.route('/')
def index():
    return "Ava Pro Server (Firebase + SQLite + Stripe) is Running!"

# --- SOCKET EVENTS ---

@socketio.on('register')
def handle_register(data):
    user_id = data.get('user_id')
    join_room(user_id)
    chat_data = get_chat(user_id)
    # Notify agent if a paid user reconnects
    if chat_data and chat_data['paid']:
        emit('user_status_change', {'user_id': user_id, 'status': 'online'}, to='agent_room')

@socketio.on('join_as_agent')
def handle_agent_join(data):
    if data.get('password') == AGENT_PASSWORD:
        join_room('agent_room')
        # Load all active paid chats from DB
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
    
    # 1. Load or Init Chat
    chat_data = get_chat(user_id)
    if not chat_data:
        chat_data = {'history': [], 'paid': False}
        # For older models without system instruction support, we'd prime it here
        # But we assume 1.5 Flash is working or fallback handles it.
    
    # 2. Save User Message
    chat_data['history'].append({'sender': 'user', 'text': msg_text})
    save_chat(user_id, chat_data['history'], chat_data['paid'])
    
    join_room(user_id)

    # 3. HUMAN MODE (If Paid)
    if chat_data['paid']:
        emit('new_msg_for_agent', {'user_id': user_id, 'text': msg_text}, to='agent_room')
        return

    # 4. AI MODE (If Not Paid)
    emit('bot_typing', to=user_id)
    eventlet.sleep(2.0) # Realistic typing delay
    
    try:
        # Start fresh AI session
        ai_chat = model.start_chat(history=[])
        # Send message
        response = ai_chat.send_message(msg_text)
        ai_text = response.text
        
        # Check for Secret Handoff Code
        if "ACTION_TRIGGER_PAYMENT" in ai_text:
            clean_text = ai_text.replace("ACTION_TRIGGER_PAYMENT", "")
            chat_data['history'].append({'sender': 'bot', 'text': clean_text})
            save_chat(user_id, chat_data['history'], chat_data['paid'])
            emit('bot_message', {'data': clean_text}, to=user_id)
            emit('payment_trigger', to=user_id)
        else:
            chat_data['history'].append({'sender': 'bot', 'text': ai_text})
            save_chat(user_id, chat_data['history'], chat_data['paid'])
            emit('bot_message', {'data': ai_text}, to=user_id)
            
    except Exception as e:
        print(f"AI Error: {e}")
        emit('bot_message', {'data': "I'm checking on that... (System reconnecting)."}, to=user_id)

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
        # Mark as paid in local DB
        chat_data['paid'] = True
        save_chat(user_id, chat_data['history'], True)
        
        # 1. Notify Agent Console
        emit('new_paid_user', {'user_id': user_id, 'history': chat_data['history']}, to='agent_room')
        
        # 2. Sync to Firebase (For User Dashboard)
        sync_chat_to_firebase(user_id, chat_data['history'])

# --- STRIPE CHECKOUT ---
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
            # Pass UID so we know who paid when they return
            success_url=f"{base_url}/?payment_success=true&uid={uid}",
            cancel_url=f"{base_url}/?payment_canceled=true",
        )
        return jsonify(url=session.url)
    except Exception as e:
        return jsonify(error=str(e)), 500

if __name__ == '__main__':
    socketio.run(app, debug=True, port=int(os.getenv("PORT", 5000)))
