# server.py
import eventlet
eventlet.monkey_patch()

import os
import time
import random
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room
from dotenv import load_dotenv
import google.generativeai as genai
import stripe

# --- CONFIGURATION ---
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
AGENT_PASSWORD = "admin"  # Simple password for your Agent Dashboard

genai.configure(api_key=GOOGLE_API_KEY)
AVA_INSTRUCTIONS = (
    "You are 'Ava,' a professional, blonde American assistant for 'HelpByExperts.' "
    "Your goal is to gather detailed information before connecting the user to a human expert. "
    "You MUST follow this strict process:\n"
    "1. Start with a friendly, professional greeting.\n"
    "2. Ask exactly 5 relevant follow-up questions, ONE BY ONE. Wait for answers.\n"
    "3. After the 5th answer, summarize and say: "
    "'Thank you. I have gathered all the details. I have identified the perfect expert to solve this immediately. The next step is a secure connection for a $5 fee, which is fully refundable if the expert cannot solve your problem or if you are unsatisfied.' "
    "4. AT THE VERY END of that final message, include: ACTION_TRIGGER_PAYMENT"
)

# --- MODEL SETUP (Keep your existing auto-select logic) ---
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

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')
stripe.api_key = STRIPE_SECRET_KEY

# --- STORAGE ---
# In a real app, use a Database. For now, we use memory.
chat_store = {}  # { user_id: { 'history': [], 'paid': False, 'model_session': obj } }

@app.route('/')
def index():
    return "Ava Custom Chat System is Running!"

# --- SOCKET LOGIC ---

@socketio.on('join_as_agent')
def handle_agent_join(data):
    if data.get('password') == AGENT_PASSWORD:
        join_room('agent_room')
        # Send all active paid chats to agent immediately
        active_chats = []
        for uid, data in chat_store.items():
            if data.get('paid', False):
                active_chats.append({'user_id': uid, 'history': data['history']})
        emit('agent_init_data', active_chats)

@socketio.on('user_message')
def handle_user_message(data):
    user_id = data.get('user_id')
    msg_text = data.get('message')
    
    # Init user if new
    if user_id not in chat_store:
        chat_store[user_id] = {
            'history': [], 
            'paid': False, 
            'model_session': model.start_chat(history=[])
        }
        # Prime older models if needed
        if not hasattr(model, '_system_instruction') or not model._system_instruction:
             chat_store[user_id]['model_session'].send_message(AVA_INSTRUCTIONS)

    # Save User Message
    chat_store[user_id]['history'].append({'sender': 'user', 'text': msg_text})
    
    # join user to their own room so agent can message them
    join_room(user_id) 

    # IF USER IS PAID -> FORWARD TO AGENT (Human Mode)
    if chat_store[user_id]['paid']:
        # Send to Agent Dashboard
        emit('new_msg_for_agent', {'user_id': user_id, 'text': msg_text}, to='agent_room')
        return # Stop AI from replying

    # IF USER IS NOT PAID -> AI REPLIES (Ava Mode)
    emit('bot_typing')
    eventlet.sleep(2.0)
    
    try:
        session = chat_store[user_id]['model_session']
        response = session.send_message(msg_text)
        ai_text = response.text
        
        # Check for Handoff Code
        if "ACTION_TRIGGER_PAYMENT" in ai_text:
            clean_text = ai_text.replace("ACTION_TRIGGER_PAYMENT", "")
            chat_store[user_id]['history'].append({'sender': 'bot', 'text': clean_text})
            emit('bot_message', {'data': clean_text})
            emit('payment_trigger')
        else:
            chat_store[user_id]['history'].append({'sender': 'bot', 'text': ai_text})
            emit('bot_message', {'data': ai_text})
            
    except Exception as e:
        print(f"AI Error: {e}")
        emit('bot_message', {'data': "I'm checking on that..."})

@socketio.on('agent_message')
def handle_agent_reply(data):
    target_user = data.get('to_user')
    text = data.get('message')
    
    if target_user in chat_store:
        # Save to history
        chat_store[target_user]['history'].append({'sender': 'agent', 'text': text})
        # Send to specific User
        emit('bot_message', {'data': text}, to=target_user)

@socketio.on('mark_paid')
def handle_payment_confirm(data):
    user_id = data.get('user_id')
    if user_id in chat_store:
        chat_store[user_id]['paid'] = True
        # Notify Agent that a new paid user is ready
        emit('new_paid_user', {
            'user_id': user_id, 
            'history': chat_store[user_id]['history']
        }, to='agent_room')

# --- STRIPE ---
@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    try:
        data = request.json
        uid = data.get('userId') # Pass user ID so we know who paid
        base_url = request.headers.get('Origin', 'https://ava-assistant-api.onrender.com')
        
        session = stripe.checkout.Session.create(
            line_items=[{'price_data': {'currency': 'usd', 'product_data': {'name': 'Expert Connection'}, 'unit_amount': 500}, 'quantity': 1}],
            mode='payment',
            success_url=f"{base_url}/?payment_success=true&uid={uid}", # Pass ID back
            cancel_url=f"{base_url}/?payment_canceled=true",
        )
        return jsonify(url=session.url)
    except Exception as e:
        return jsonify(error=str(e)), 500

if __name__ == '__main__':
    socketio.run(app, debug=True, port=int(os.getenv("PORT", 5000)))
