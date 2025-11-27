# server.py

# 1. MONKEY PATCH MUST BE FIRST (Before importing Flask or anything else)
import eventlet
eventlet.monkey_patch()

import os
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
model = genai.GenerativeModel(
    "gemini-1.5-flash",
    system_instruction="You are 'Ava,' a helpful, polite, and efficient AI assistant for 'HelpByExperts,' a service that connects users to human professionals (Doctors, Lawyers, Mechanics, etc.) for a $5 fee. Your primary goal is to identify the user's issue and determine the correct category. After determining the category and summarizing the issue, you MUST respond by telling the user: 'I have identified the right expert for your issue. The next step is a quick, secure connection for a $5 fee, which is fully refundable if you're not satisfied.' and then emit the payment token. You must only do this ONCE after 2-3 user messages. Start with a friendly greeting."
)

# 4. Initialize Flask App
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'default_secret_key')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet') # Explicitly use eventlet
stripe.api_key = STRIPE_SECRET_KEY

# In-memory storage for chat history
chat_sessions = {}

# --- NEW: HEALTH CHECK ROUTE ---
# This stops the "404" errors in your logs when Render checks the server
@app.route('/')
def index():
    return "Ava Chat Server is Running!"

# --- SOCKET.IO CHAT LOGIC ---
@socketio.on('user_message')
def handle_user_message(data):
    user_id = data.get('user_id')
    message = data.get('message')

    if user_id not in chat_sessions:
        chat_sessions[user_id] = model.start_chat(history=[])
    
    chat = chat_sessions[user_id]
    emit('bot_typing') 

    try:
        history_len = len(chat.history)
        
        # Handoff Logic
        if history_len >= 6 and "identified the right expert" not in [part.text for part in chat.history[-1].parts]:
             handoff_msg = "I have identified the right expert for your issue. The next step is a quick, secure connection for a $5 fee, which is fully refundable if you're not satisfied."
             emit('bot_message', {'data': handoff_msg})
             emit('payment_trigger')
             return

        # Generate Response
        response = chat.send_message(message)
        emit('bot_message', {'data': response.text})

    except Exception as e:
        print(f"AI Error: {e}")
        emit('bot_message', {'data': "I'm sorry, I'm having trouble connecting to the system right now. Please try again."})

# --- STRIPE PAYMENT ENDPOINT ---
@app.route('/create-payment-intent', methods=['POST'])
def create_payment():
    try:
        intent = stripe.PaymentIntent.create(
            amount=500,  
            currency='usd',
            automatic_payment_methods={'enabled': True},
        )
        return jsonify(clientSecret=intent.client_secret)
    except Exception as e:
        return jsonify(error={'message': str(e)}), 403

# --- RUN SERVER ---
if __name__ == '__main__':
    socketio.run(app, debug=True, port=int(os.getenv("PORT", 5000)))
