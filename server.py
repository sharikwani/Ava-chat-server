# server.py (Using Flask and Flask-SocketIO)

import os
from flask import Flask, jsonify, request
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv
from openai import OpenAI
import stripe

# 1. Load Environment Variables (API Keys)
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")

# 2. Initialize Flask App and Extensions
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'default_secret_key') # Set a proper key in .env
socketio = SocketIO(app, cors_allowed_origins="*") # Important for allowing connections from your index.html

# 3. Initialize AI and Payment Clients
openai_client = OpenAI(api_key=OPENAI_API_KEY)
stripe.api_key = STRIPE_SECRET_KEY

# Simple in-memory storage for context (replace with database for production)
chat_history = {}

# --- SOCKET.IO CHAT LOGIC ---
@socketio.on('user_message')
def handle_user_message(data):
    user_id = data.get('user_id')
    message = data.get('message')

    if user_id not in chat_history:
        # Define Ava's persona and rules (System Prompt)
        chat_history[user_id] = [{
            "role": "system",
            "content": "You are 'Ava,' a helpful, polite, and efficient AI assistant for 'HelpByExperts,' a service that connects users to human professionals (Doctors, Lawyers, Mechanics, etc.) for a $5 fee. Your primary goal is to identify the user's issue and determine the correct category. After determining the category and summarizing the issue, you MUST respond by telling the user: 'I have identified the right expert for your issue. The next step is a quick, secure connection for a $5 fee, which is fully refundable if you're not satisfied.' and then **emit the payment_trigger event**. You must only do this ONCE after 2-3 user messages. Start with a friendly greeting."
        }]

    chat_history[user_id].append({"role": "user", "content": message})

    # Basic handoff logic
    is_ready_for_handoff = len(chat_history[user_id]) >= 7 # System + 3 user + 3 bot responses
    
    emit('bot_typing') # Send typing indicator

    try:
        if is_ready_for_handoff and not any('identified the right expert' in h['content'] for h in chat_history[user_id]):
            # Handoff message
            llm_response = "I have identified the right expert for your issue. The next step is a quick, secure connection for a $5 fee, which is fully refundable if you're not satisfied."
            emit('bot_message', {'data': llm_response})
            emit('payment_trigger') # <--- KEY EVENT TO TRIGGER FRONTEND MODAL
            chat_history[user_id].append({"role": "assistant", "content": llm_response})
        else:
            # Regular AI interaction
            completion = openai_client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=chat_history[user_id],
            )
            llm_response = completion.choices[0].message.content
            emit('bot_message', {'data': llm_response})
            chat_history[user_id].append({"role": "assistant", "content": llm_response})
            
    except Exception as e:
        print(f"AI/Socket Error: {e}")
        emit('bot_message', {'data': "I'm sorry, I'm having trouble connecting to the system right now. Please try again."})

# --- STRIPE PAYMENT ENDPOINT (REST API) ---
@app.route('/create-payment-intent', methods=['POST'])
def create_payment():
    try:
        # Amount is $5.00
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
    # Use Gunicorn on Render, but Flask built-in server for local development
    socketio.run(app, debug=True, port=int(os.getenv("PORT", 5000)))
