# 1. MONKEY PATCH MUST BE FIRST (EVENTLET VERSION)
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
from flask_socketio import SocketIO, emit, join_room, leave_room
from dotenv import load_dotenv
import google.generativeai as genai
import stripe
import firebase_admin
from firebase_admin import credentials, firestore

# --- CONFIGURATION ---
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
# Ensure this matches your actual Render Environment Variable name exactly!
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://www.helpbyexperts.com/account.html") 
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
        # Fallback for local testing
        cred = credentials.Certificate("firebase-adminsdk.json")
        firebase_admin.initialize_app(cred)
        firebase_db = firestore.client()
        print("✅ Firebase Admin Connected (Local File)")
except Exception as e:
    print(f"⚠️ Firebase Error: {e}")

# --- AI SETUP ---
genai.configure(api_key=GOOGLE_API_KEY)

AVA_INSTRUCTIONS = (
    "You are 'Ava,' the Intake Specialist for 'HelpByExperts.' "
    "Your role is to gather details and contact info for the Human Expert. "
    "You are NOT the expert. You MUST NOT give technical advice, diagnoses, or solutions. "
    "If a user asks for a specific fix, say: 'That is something our certified expert will need to confirm to ensure it is done safely.' "
    "\n\n"
    "STRICT CONVERSATION FLOW (Do not skip steps):"
    "1. Greet the user and ask what issue they are facing."
    "2. Ask 1 relevant follow-up question about symptoms."
    "3. Say: 'I need to start a case file for the expert. What is your Full Name?'"
    "4. Wait for answer. Then ask: 'Thank you. What is the best Email Address to send the chat transcript to?'"
    "5. Wait for answer. Then ask: 'And a Phone Number in case we get disconnected?'"
    "6. Once you have the Name, Email, and Phone, say: "
    "'Thank you. I have captured all the details. This sounds like a specific issue that requires a certified professional to resolve. I have an expert available right now to guide you. The connection fee is a fully refundable $5.' "
    "7. END your final message with exactly: ACTION_TRIGGER_PAYMENT"
)

def setup_model():
    try:
        return genai.GenerativeModel("gemini-1.5-flash", system_instruction=AVA_INSTRUCTIONS)
    except:
        return genai.GenerativeModel("gemini-pro")

model = setup_model()

# --- SERVER SETUP ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
CORS(app, resources={r"/*": {"origins": "*"}})

# *** CRITICAL FIX: ASYNC_MODE SET TO EVENTLET ***
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')
stripe.api_key = STRIPE_SECRET_KEY

# --- DATABASE (LOCAL LOGS) ---
DB_FILE = "chat_data.db"
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS chats 
                 (user_id TEXT PRIMARY KEY, history TEXT, paid BOOLEAN)''')
    conn.commit()
    conn.close()
init_db()

def save_local_log(user_id, sender, text):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT history, paid FROM chats WHERE user_id=?", (user_id,))
        row = c.fetchone()
        
        history = []
        paid = False
        if row:
            history = json.loads(row[0])
            paid = row[1]
        
        history.append({'sender': sender, 'text': text})
        
        c.execute("INSERT OR REPLACE INTO chats (user_id, history, paid) VALUES (?, ?, ?)", 
                  (user_id, json.dumps(history), paid))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Database Error: {e}")

# --- HELPER: CHECK IF HUMAN IS LIVE ---
def is_human_live(user_id):
    if not firebase_db: return False
    try:
        user_doc = firebase_db.collection('users').document(user_id).get()
        if user_doc.exists:
            data = user_doc.to_dict()
            session_id = data.get('activeSessionId')
            if session_id:
                chat_doc = firebase_db.collection('chats').document(session_id).get()
                if chat_doc.exists and chat_doc.to_dict().get('status') == 'live':
                    return True
    except Exception as e:
        print(f"Error checking status: {e}")
    return False

# --- SOCKET EVENTS ---

@socketio.on('register')
def handle_register(data):
    user_id = data.get('user_id')
    join_room(user_id)
    print(f"User connected: {user_id}")

@socketio.on('user_message')
def handle_user_message(data):
    user_id = data.get('user_id')
    msg_text = data.get('message')
    
    print(f"📩 RECEIVED MESSAGE from {user_id}: {msg_text}") # DEBUG LOG

    save_local_log(user_id, 'user', msg_text)
    
    # SWITCHBOARD LOGIC
    if is_human_live(user_id):
        print(f"twisted_rightwards_arrows SWITCHBOARD: User {user_id} is LIVE with Agent. Skipping AI.") # DEBUG LOG
        emit('new_message_for_agent', {
            'text': msg_text,
            'user_id': user_id,
            'sender': 'user'
        }, room=user_id) 
        return

    print(f"robot SWITCHBOARD: User {user_id} is NOT live. Sending to Ava...") # DEBUG LOG

    # AI LOGIC
    emit('bot_typing', to=user_id)
    
    # Eventlet sleep
    eventlet.sleep(1) 
    
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT history FROM chats WHERE user_id=?", (user_id,))
        row = c.fetchone()
        conn.close()
        
        gemini_history = []
        if row:
            raw_hist = json.loads(row[0])
            for msg in raw_hist[-10:]:
                if msg['sender'] == 'user': gemini_history.append({'role': 'user', 'parts': [msg['text']]})
                elif msg['sender'] == 'bot': gemini_history.append({'role': 'model', 'parts': [msg['text']]})

        print("🤖 CALLING GEMINI API...") # DEBUG LOG
        ai_chat = model.start_chat(history=gemini_history)
        response = ai_chat.send_message(msg_text)
        ai_text = response.text
        print(f"✅ GEMINI REPLIED: {ai_text[:50]}...") # DEBUG LOG
        
        if "ACTION_TRIGGER_PAYMENT" in ai_text:
            clean_text = ai_text.replace("ACTION_TRIGGER_PAYMENT", "")
            save_local_log(user_id, 'bot', clean_text)
            emit('bot_message', {'data': clean_text}, to=user_id)
            emit('payment_trigger', to=user_id)
        else:
            save_local_log(user_id, 'bot', ai_text)
            emit('bot_message', {'data': ai_text}, to=user_id)
            
    except Exception as e:
        print(f"❌ AI ERROR: {e}") # DEBUG LOG
        emit('bot_message', {'data': "I'm having trouble connecting to the brain. One moment."}, to=user_id)

# --- AGENT EVENTS ---

@socketio.on('agent_join')
def handle_agent_join(data):
    target_user_id = data.get('target_user_id')
    join_room(target_user_id)
    print(f"Agent joined room: {target_user_id}")
    emit('agent_status', {'status': 'connected'}, to=target_user_id)

@socketio.on('agent_message')
def handle_agent_message(data):
    target_user_id = data.get('target_user_id')
    message = data.get('message')
    save_local_log(target_user_id, 'Agent', message)
    emit('bot_message', {'data': message, 'is_agent': True}, to=target_user_id)

@socketio.on('mark_paid')
def handle_payment_confirm(data):
    user_id = data.get('user_id')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE chats SET paid=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

# --- STRIPE ---
@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    try:
        data = request.json
        uid = data.get('userId')
        success_url = f"{FRONTEND_URL}?payment_success=true&uid={uid}"
        cancel_url = f"{FRONTEND_URL}?payment_canceled=true"
        
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
            success_url=success_url,
            cancel_url=cancel_url,
        )
        return jsonify(url=session.url)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route('/')
def index():
    return "HelpByExperts API (Eventlet) is Running"

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    # This runs the production-ready Eventlet server
    socketio.run(app, host='0.0.0.0', port=port)
