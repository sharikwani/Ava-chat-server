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
    "You are Ava, a highly professional, calm, confident, and trustworthy intake specialist for HelpByExperts — a premium service that connects users instantly to certified human experts in ANY field.\n\n"
    "You speak like an elite concierge: polished, mature, authoritative, extremely concise (2-3 sentences max per reply).\n\n"
    "STRICT RULES:\n"
    "- Never use emojis, slang, or casual language.\n"
    "- Never ask for name, email, or phone.\n"
    "- Never give advice yourself — always defer to the expert.\n"
    "- Ask exactly one short clarifying question per reply to gather 3 additional critical details.\n"
    "- After initial issue + 3 details (total 4 pieces of info), immediately close with the $5 fee using this EXACT wording:\n"
    "'There is a $5 connection fee which is fully refundable only if you are not satisfied or the specialist is unable to resolve your query. A certified specialist is available right now. Shall I connect you?'\n\n"
    "Then output on separate lines:\n"
    "Category: [EXACT category from list below]\n"
    "ACTION_TRIGGER_PAYMENT\n\n"
    "Exact categories you MUST choose from (use word-for-word):\n"
    "Medical/Health, Veterinary/Pet Care, Automotive/Mechanical, Plumbing, Electrical, Tech Support, Legal, Tax/Finance, Relationships/Psychology, Home Repair/Construction, Business Consulting, Real Estate, Appliances/HVAC, General/Other\n\n"
    "Example final message:\n"
    "There is a $5 connection fee which is fully refundable only if you are not satisfied or the specialist is unable to resolve your query. A certified specialist is available right now. Shall I connect you?\n"
    "Category: Automotive/Mechanical\n"
    "ACTION_TRIGGER_PAYMENT"
)

def setup_model():
    try:
        valid_models = [m for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        valid_names = [m.name for m in valid_models]
        
        # Pick the latest stable flash model
        chosen = None
        for name in valid_names:
            if 'flash' in name.lower() and 'preview' not in name:
                chosen = name
                break
        if not chosen:
            for name in valid_names:
                if 'flash' in name.lower():
                    chosen = name
                    break
        if not chosen and valid_names:
            chosen = valid_names[0]

        print(f"AI Connected: {chosen}")

        return genai.GenerativeModel(
            chosen,
            system_instruction=AVA_INSTRUCTIONS,
            generation_config={
                "temperature": 0.75,
                "top_p": 0.9,
                "top_k": 40,
                "max_output_tokens": 100
            }
        )
    except Exception as e:
        print(f"Model setup fallback due to: {e}")
        return genai.GenerativeModel(
            "gemini-1.5-flash",
            system_instruction=AVA_INSTRUCTIONS,
            generation_config={"temperature": 0.75, "max_output_tokens": 100}
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
                 (user_id TEXT PRIMARY KEY, history TEXT, paid BOOLEAN, category TEXT)''')
    conn.commit()
    conn.close()
init_db()

def get_chat(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT history, paid, category FROM chats WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {'history': json.loads(row[0]), 'paid': row[1], 'category': row[2] or 'General/Other'}
    return {'history': [], 'paid': False, 'category': 'General/Other'}

def save_chat(user_id, history, paid, category='General/Other'):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO chats (user_id, history, paid, category) VALUES (?, ?, ?, ?)",
              (user_id, json.dumps(history), paid, category))
    conn.commit()
    conn.close()

# --- FIREBASE SYNC ---
def sync_chat_to_firebase(user_id, history, category):
    if firebase_db:
        eventlet.tpool.execute(lambda: firebase_db.collection('chats').add({
            'user_id': user_id,
            'history': history,
            'category': category,
            'timestamp': firestore.SERVER_TIMESTAMP,
            'status': 'paid'
        }))

# --- SOCKET EVENTS ---
@socketio.on('register')
def handle_register(data):
    user_id = data.get('user_id')
    join_room(user_id)

@socketio.on('join_as_agent')
def handle_agent_join(data):
    if data.get('password') != AGENT_PASSWORD:
        return
    categories = data.get('categories', [])
    for cat in categories:
        room_name = f"{cat.lower().replace('/', '_')}_experts"
        join_room(room_name)

    # Send all paid chats — frontend filters by category
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, history, category FROM chats WHERE paid=1")
    rows = c.fetchall()
    conn.close()
    active_chats = [{'user_id': r[0], 'history': json.loads(r[1]), 'category': r[2] or 'General/Other'} for r in rows]
    emit('agent_init_data', active_chats)

@socketio.on('user_message')
def handle_user_message(data):
    user_id = data.get('user_id')
    msg_text = data.get('message')

    chat_data = get_chat(user_id)
    chat_data['history'].append({'sender': 'user', 'text': msg_text})
    save_chat(user_id, chat_data['history'], chat_data['paid'], chat_data['category'])
    join_room(user_id)

    if chat_data['paid']:
        room = f"{chat_data['category'].lower().replace('/', '_')}_experts"
        emit('new_msg_for_agent', {'user_id': user_id, 'text': msg_text}, to=room)
        return

    emit('bot_typing', to=user_id)
    eventlet.sleep(random.uniform(1.2, 3.8))

    try:
        # Build clean history for Gemini (exclude internal Category: lines)
        gemini_history = []
        for msg in chat_data['history'][:-1]:
            if msg['sender'] == 'bot' and msg['text'].strip().startswith('Category:'):
                continue
            role = 'user' if msg['sender'] == 'user' else 'model'
            gemini_history.append({'role': role, 'parts': [msg['text']]})

        ai_chat = model.start_chat(history=gemini_history)
        response = ai_chat.send_message(msg_text)
        ai_text = response.text.strip()

        category = chat_data['category']
        trigger = False
        clean_text = ai_text

        if "ACTION_TRIGGER_PAYMENT" in ai_text:
            trigger = True
            lines = ai_text.split('\n')
            for line in lines:
                if line.strip().startswith("Category:"):
                    category = line.strip()[9:].strip()
            clean_lines = [l for l in lines if not l.strip().startswith("Category:") and l.strip() != "ACTION_TRIGGER_PAYMENT"]
            clean_text = "\n".join(clean_lines).strip()

        chat_data['history'].append({'sender': 'bot', 'text': clean_text})
        chat_data['category'] = category
        save_chat(user_id, chat_data['history'], chat_data['paid'], category)
        emit('bot_message', {'data': clean_text}, to=user_id)

        if trigger:
            emit('payment_trigger', {'category': category}, to=user_id)

    except Exception as e:
        print(f"AI Error: {e}")
        emit('bot_message', {'data': "Please allow me a moment."}, to=user_id)

@socketio.on('mark_paid')
def handle_payment_confirm(data):
    user_id = data.get('user_id')
    chat_data = get_chat(user_id)
    if not chat_data['paid']:
        chat_data['paid'] = True
        save_chat(user_id, chat_data['history'], True, chat_data['category'])

        room = f"{chat_data['category'].lower().replace('/', '_')}_experts"
        payload = {
            'user_id': user_id,
            'history': chat_data['history'],
            'category': chat_data['category']
        }
        emit('new_paid_user', payload, to=room)
        sync_chat_to_firebase(user_id, chat_data['history'], chat_data['category'])

@socketio.on('agent_message')
def handle_agent_reply(data):
    target_user = data.get('to_user')
    text = data.get('message')
    chat_data = get_chat(target_user)
    if chat_data and chat_data['paid']:
        chat_data['history'].append({'sender': 'agent', 'text': text})
        save_chat(target_user, chat_data['history'], True, chat_data['category'])
        emit('bot_message', {'data': text, 'is_agent': True}, to=target_user)

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
                    'product_data': {'name': 'Expert Connection Fee', 'description': 'Fully refundable if not satisfied'},
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
