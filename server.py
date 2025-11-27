# server.py (Updated for Google Gemini)
import os
from flask import Flask, jsonify
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv
import google.generativeai as genai
import stripe

# 1. Load Environment Variables
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") # This is what we need now
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")

# 2. Configure Google Gemini
genai.configure(api_key=GOOGLE_API_KEY)
# We use the system instruction parameter for the persona
model = genai.GenerativeModel(
    "gemini-1.5-flash",
    system_instruction="You are 'Ava,' a helpful, polite, and efficient AI assistant for 'HelpByExperts,' a service that connects users to human professionals (Doctors, Lawyers, Mechanics, etc.) for a $5 fee. Your primary goal is to identify the user's issue and determine the correct category. After determining the category and summarizing the issue, you MUST respond by telling the user: 'I have identified the right expert for your issue. The next step is a quick, secure connection for a $5 fee, which is fully refundable if you're not satisfied.' and then emit the payment token. You must only do this ONCE after 2-3 user messages. Start with a friendly greeting."
)

# 3. Initialize Flask App
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'default_secret_key')
socketio = SocketIO(app, cors_allowed_origins="*")
stripe.api_key = STRIPE_SECRET_KEY

# In-memory storage for chat history
# Structure: { user_id: chat_session_object }
chat_sessions = {}

# --- SOCKET.IO CHAT LOGIC ---
@socketio.on('user_message')
def handle_user_message(data):
    user_id = data.get('user_id')
    message = data.get('message')

    # 1. Initialize Chat Session for new user
    if user_id not in chat_sessions:
        chat_sessions[user_id] = model.start_chat(history=[])
    
    chat = chat_sessions[user_id]
    
    # 2. Send Typing Indicator
    emit('bot_typing') 

    try:
        # 3. Check conversation length for handoff logic
        # Gemini history counts both user and model turns
        history_len = len(chat.history)
        
        # Simple Logic: If history is long enough, trigger payment (approx 3 turns each)
        if history_len >= 6 and "identified the right expert" not in [part.text for part in chat.history[-1].parts]:
             handoff_msg = "I have identified the right expert for your issue. The next step is a quick, secure connection for a $5 fee, which is fully refundable if you're not satisfied."
             emit('bot_message', {'data': handoff_msg})
             emit('payment_trigger')
             # We manually add this to history to keep context correct if needed
             # (Note: manual history insertion in SDK is tricky, so we just emit it here)
             return

        # 4. Generate AI Response
        response = chat.send_message(message)
        text_response = response.text
        
        emit('bot_message', {'data': text_response})

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
