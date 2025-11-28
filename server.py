import eventlet
eventlet.monkey_patch()

import os
import sqlite3
import json
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room
from dotenv import load_dotenv
import google.generativeai as genai
import stripe

# --- NEW: FIREBASE IMPORTS ---
import firebase_admin
from firebase_admin import credentials, firestore

# --- CONFIG ---
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
AGENT_PASSWORD = "admin" 

# --- FIREBASE SETUP ---
# It looks for the file you uploaded to GitHub
try:
    cred = credentials.Certificate("firebase-key.json")
    firebase_admin.initialize_app(cred)
    firebase_db = firestore.client()
    print("✅ Firebase Admin Connected")
except Exception as e:
    print(f"⚠️ Firebase Connection Failed: {e}")
    # We continue running even if Firebase fails, to keep chat alive
    firebase_db = None

# --- AI SETUP ---
genai.configure(api_key=GOOGLE_API_KEY)
AVA_INSTRUCTIONS = (
    "You are 'Ava,' a professional, blonde American assistant for 'HelpByExperts.' "
    "1. Ask exactly 5 follow-up questions, ONE BY ONE. "
    "2. After the 5th answer, summarize and say: 'I have identified the expert. The next step is a secure connection for a $5 fee.' "
    "3. End the final message with: ACTION_TRIGGER_PAYMENT"
)

def get_ai_model():
    try:
        return genai.GenerativeModel("gemini-1.5-flash", system_instruction=AVA_INSTRUCTIONS)
    except:
        return genai.GenerativeModel("gemini-pro")

model = get_ai_model()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')
stripe.api_key = STRIPE_SECRET_KEY

# --- LOCAL DATABASE (SQLite) ---
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

# --- ROUTES ---
@app.route('/')
def index():
    return "Ava Pro Server (Firebase Connected) is Running!"

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
        # Also sync new messages to Firebase in real-time if you want live updates in account.html
        # For now, we sync on payment to save writes.
        return

    # AI MODE
    emit('bot_typing', to=user_id)
    eventlet.sleep(1.5)
    
    try:
        ai_chat = model.start_chat(history=[])
        # In a real app, you would reconstruct history here for the AI
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
        emit('bot_message', {'data': "One moment..."}, to=user_id)

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

# --- THE CRITICAL SYNC FUNCTION ---
def sync_chat_to_firebase(user_id, history):
    if firebase_db:
        try:
            # We add a new document to the 'chats' collection
            # user_id allows us to query it in account.html
            firebase_db.collection('chats').add({
                'user_id': user_id,
                'history': history,
                'timestamp': firestore.SERVER_TIMESTAMP,
                'status': 'paid'
            })
            print(f"✅ Synced chat for {user_id} to Firebase")
        except Exception as e:
            print(f"❌ Firebase Sync Error: {e}")

@socketio.on('mark_paid')
def handle_payment_confirm(data):
    user_id = data.get('user_id')
    join_room(user_id)
    
    chat_data = get_chat(user_id)
    if chat_data:
        chat_data['paid'] = True
        save_chat(user_id, chat_data['history'], True)
        
        # 1. Notify Agent Console
        emit('new_paid_user', {'user_id': user_id, 'history': chat_data['history']}, to='agent_room')
        
        # 2. SYNC TO FIREBASE (So user sees it in Account Dashboard)
        sync_chat_to_firebase(user_id, chat_data['history'])

@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    try:
        data = request.json
        uid = data.get('userId')
        base_url = request.headers.get('Origin', 'https://ava-assistant-api.onrender.com')
        session = stripe.checkout.Session.create(
            line_items=[{'price_data': {'currency': 'usd', 'product_data': {'name': 'Expert Connection'}, 'unit_amount': 500}, 'quantity': 1}],
            mode='payment',
            success_url=f"{base_url}/?payment_success=true&uid={uid}",
            cancel_url=f"{base_url}/?payment_canceled=true",
        )
        return jsonify(url=session.url)
    except Exception as e:
        return jsonify(error=str(e)), 500

if __name__ == '__main__':
    socketio.run(app, debug=True, port=int(os.getenv("PORT", 5000)))
