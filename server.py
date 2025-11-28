import eventlet
# eventlet.monkey_patch()  <-- REMOVED/COMMENTED OUT

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

# CRITICAL UPDATE: THE "NO ADVICE" INTAKE SCRIPT
AVA_INSTRUCTIONS = (
    "You are 'Ava,' the Intake Specialist for 'HelpByExperts.' "
    "Your role is to gather details and contact info for the Human Expert. "
    "You are NOT the expert. You MUST NOT give technical advice, diagnoses, or solutions. "
    "If a user asks for a specific fix, say: 'That is something our certified expert will need to confirm to ensure it is done safely.' "
    "\n\n"
    "STRICT CONVERSATION FLOW (Do not skip steps):"
    "1. Greet the user and ask what issue they are facing."
    "2. Ask 1 relevant follow-up question about symptoms (e.g., 'How long has this been happening?' or 'Are there any error codes?')."
    "3. Say: 'I need to start a case file for the expert. What is your Full Name?'"
    "4. Wait for answer. Then ask: 'Thank you. What is the best Email Address to send the chat transcript to?'"
    "5. Wait for answer. Then ask: 'And a Phone Number in case we get disconnected?'"
    "6. Once you have the Name, Email, and Phone, say: "
    "'Thank you. I have captured all the details. This sounds like a specific issue that requires a certified professional to resolve. I have an expert available right now to guide you. The connection fee is a fully refundable $5.' "
    "7. END your final message with exactly: ACTION_TRIGGER_PAYMENT"
)

def setup_model():
    try:
        valid_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        if not valid_models: return genai.GenerativeModel("gemini-pro")
        
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
def sync_chat_to_firebase(user_id, history):
    if firebase_db:
        try:
            # Extract contact info from history if possible (basic parsing)
            # This saves the whole chat so the Agent sees the Name/Phone provided to Ava
            firebase_db.collection('chats').add({
                'user_id': user_id,
                'history': history,
                'timestamp': firestore.SERVER_TIMESTAMP,
                'status': 'paid'
            })
        except Exception as e:
            print(f"❌ Firebase Sync Error: {e}")

# --- ROUTES ---
@app.route('/')
def index():
    return "Ava Pro Server (Intake Specialist Mode) is Running!"

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

    # HUMAN MODE
    if chat_data['paid']:
        emit('new_msg_for_agent', {'user_id': user_id, 'text': msg_text}, to='agent_room')
        return

    # AI MODE (Ava)
    emit('bot_typing', to=user_id)
    eventlet.sleep(1.5) 
    
    try:
        # MEMORY RECONSTRUCTION
        gemini_history = []
        for msg in chat_data['history']:
            if msg['text'] == msg_text and msg is chat_data['history'][-1]: continue
            if msg['sender'] == 'user':
                gemini_history.append({'role': 'user', 'parts': [msg['text']]})
            elif msg['sender'] == 'bot':
                gemini_history.append({'role': 'model', 'parts': [msg['text']]})
        
        ai_chat = model.start_chat(history=gemini_history)
        response = ai_chat.send_message(msg_text)
        ai_text = response.text
        
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
        emit('bot_message', {'data': "I'm noting that down. Could you verify?"}, to=user_id)

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

