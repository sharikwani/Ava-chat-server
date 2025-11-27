# server.py

# 1. MONKEY PATCH MUST BE FIRST
import eventlet
eventlet.monkey_patch()

import os
import time
import random
from flask import Flask, jsonify
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv
import google.generativeai as genai
import stripe

# 2. Load Environment Variables
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")

# 3. Configure Google Gemini
genai.configure(api_key=GOOGLE_API_KEY)

# --- AVA'S UPDATED INSTRUCTIONS ---
# Updated with strict 5-question rule and clarified refund policy
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

# --- SMART MODEL DISCOVERY ---
def setup_model():
    print("--- 🤖 AVA INITIALIZATION: Searching for a working AI model ---")
    try:
        valid_models = []
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                valid_models.append(m.name)
        
        if not valid_models:
            return genai.GenerativeModel("gemini-pro")

        chosen_model_name = valid_models[0]
        # Prefer 1.5 Flash or Pro
        for name in valid_models:
            if "flash" in name and "1.5" in name:
                chosen_model_name = name
                break
            elif "1.5" in name:
                chosen_model_name = name
        
        print(f"✅ SUCCESS! Auto-selected model: {chosen_model_name}")

        if "1.5" in chosen_model_name:
            return genai.GenerativeModel(chosen_model_name, system_instruction=AVA_INSTRUCTIONS)
        else:
            return genai.GenerativeModel(chosen_model_name)

    except Exception:
        return genai.GenerativeModel("gemini-pro")

model = setup_model()

# 4. Initialize Flask App
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'default_secret_key')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')
stripe.api_key = STRIPE_SECRET_KEY

chat_sessions = {}

# --- HEALTH CHECK ---
@app.route('/')
def index():
    return "Ava 2.1 is Running!"

# --- SOCKET.IO CHAT LOGIC ---
@socketio.on('user_message')
def handle_user_message(data):
    user_id = data.get('user_id')
    message = data.get('message')

    if user_id not in chat_sessions:
        chat_sessions[user_id] = model.start_chat(history=[])
        # If using older model, prime it here
        if not hasattr(model, '_system_instruction') or not model._system_instruction:
             chat_sessions[user_id].send_message(AVA_INSTRUCTIONS)
    
    chat = chat_sessions[user_id]
    
    # 1. SHOW TYPING INDICATOR
    emit('bot_typing') 

    # 2. FASTER DELAY (Fixed to max 3 seconds)
    # Changed to uniform random between 2.0 and 3.0 seconds
    time_to_sleep = random.uniform(2.0, 3.0)
    eventlet.sleep(time_to_sleep) 

    try:
        # 3. GET AI RESPONSE
        response = chat.send_message(message)
        text_response = response.text
        
        # 4. CHECK FOR SECRET CODE
        if "ACTION_TRIGGER_PAYMENT" in text_response:
            clean_text = text_response.replace("ACTION_TRIGGER_PAYMENT", "")
            emit('bot_message', {'data': clean_text})
            emit('payment_trigger') 
        else:
            emit('bot_message', {'data': text_response})

    except Exception as e:
        print(f"AI Error: {e}")
        emit('bot_message', {'data': "I'm checking on that... (Connection blip, please type again)."})

# --- STRIPE PAYMENT ---
@app.route('/create-payment-intent', methods=['POST'])
def create_payment():
    try:
        intent = stripe.PaymentIntent.create(
            amount=500, currency='usd', automatic_payment_methods={'enabled': True},
        )
        return jsonify(clientSecret=intent.client_secret)
    except Exception as e:
        return jsonify(error={'message': str(e)}), 403

if __name__ == '__main__':
    socketio.run(app, debug=True, port=int(os.getenv("PORT", 5000)))
