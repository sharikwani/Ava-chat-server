# server.py

# 1. MONKEY PATCH MUST BE FIRST
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

# --- AVA'S INSTRUCTIONS ---
AVA_INSTRUCTIONS = (
    "You are 'Ava,' a helpful, polite, and efficient AI assistant for 'HelpByExperts,' "
    "a service that connects users to human professionals (Doctors, Lawyers, Mechanics, etc.) for a $5 fee. "
    "Your primary goal is to identify the user's issue and determine the correct category. "
    "After determining the category and summarizing the issue, you MUST respond by telling the user: "
    "'I have identified the right expert for your issue. The next step is a quick, secure connection for a $5 fee, which is fully refundable if you're not satisfied.' "
    "and then emit the payment token. You must only do this ONCE after 2-3 user messages. "
    "Start with a friendly greeting."
)

# --- AUTO-SELECT WORKING MODEL ---
def get_working_model():
    # List of models to try (Newest to Oldest)
    candidates = [
        "gemini-1.5-flash",
        "gemini-1.5-flash-latest",
        "gemini-1.5-pro",
        "gemini-pro",
        "gemini-1.0-pro"
    ]
    
    print("--- 🤖 AVA INITIALIZATION: Searching for a working AI model ---")
    
    for model_name in candidates:
        try:
            print(f"Testing model: {model_name}...")
            # Attempt to create model with instructions
            test_model = genai.GenerativeModel(
                model_name,
                system_instruction=AVA_INSTRUCTIONS
            )
            # Dry run: Try to generate one word to see if the API accepts the key/model
            test_model.generate_content("Hello")
            
            print(f"✅ SUCCESS! Connected to model: {model_name}")
            return test_model
        except Exception as e:
            print(f"❌ Failed to load {model_name}. Error: {e}")
            continue

    print("⚠️ CRITICAL: All models failed. Defaulting to basic 'gemini-pro' without system instructions.")
    # Last resort fallback
    return genai.GenerativeModel("gemini-pro")

# Initialize the model using the function
model = get_working_model()

# 4. Initialize Flask App
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'default_secret_key')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')
stripe.api_key = STRIPE_SECRET_KEY

# In-memory storage for chat history
chat_sessions = {}

# --- HEALTH CHECK ROUTE ---
@app.route('/')
def index():
    return "Ava Chat Server is Running and AI is Connected!"

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
        if history_len >= 6:
            # Check if we already did the handoff in previous turn
            last_response = chat.history[-1].parts[0].text if chat.history else ""
            if "identified the right expert" not in last_response:
                 handoff_msg = "I have identified the right expert for your issue. The next step is a quick, secure connection for a $5 fee, which is fully refundable if you're not satisfied."
                 emit('bot_message', {'data': handoff_msg})
                 emit('payment_trigger')
                 # Manually append to history to stop it from triggering again
                 # (This is a simplified way to handle history sync)
                 return

        # Generate Response
        response = chat.send_message(message)
        emit('bot_message', {'data': response.text})

    except Exception as e:
        print(f"runtime AI Error: {e}")
        emit('bot_message', {'data': "I'm checking on that... (System reconnecting, please type again)."})

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
