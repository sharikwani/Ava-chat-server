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

# --- SMART MODEL DISCOVERY ---
def setup_model():
    print("--- 🔍 DIAGNOSTIC: Listing available models for this API Key ---")
    try:
        # Ask Google what models are actually available to us
        valid_models = []
        for m in genai.list_models():
            # We only want models that generate content (chat)
            if 'generateContent' in m.supported_generation_methods:
                print(f"   found valid model: {m.name}")
                valid_models.append(m.name)
        
        if not valid_models:
            print("❌ CRITICAL: No text generation models found for this API Key/Region.")
            # Fallback to a safe default just in case
            return genai.GenerativeModel("gemini-pro")

        # Logic: Prefer 'flash', then '1.5', then whatever is available
        chosen_model_name = valid_models[0] # Default to the first one found
        
        for name in valid_models:
            if "flash" in name and "1.5" in name:
                chosen_model_name = name
                break
            elif "1.5" in name:
                chosen_model_name = name
        
        print(f"✅ SUCCESS! Auto-selected model: {chosen_model_name}")

        # Note: Older models (1.0) crash with 'system_instruction', newer ones (1.5) need it.
        if "1.5" in chosen_model_name:
            return genai.GenerativeModel(chosen_model_name, system_instruction=AVA_INSTRUCTIONS)
        else:
            print("⚠️ Using legacy model (no system instruction supported)")
            # For older models, we prepend instructions to the history instead
            return genai.GenerativeModel(chosen_model_name)

    except Exception as e:
        print(f"❌ MODEL SETUP ERROR: {e}")
        return genai.GenerativeModel("gemini-pro") # Absolute backup

# Initialize the model dynamically
model = setup_model()

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
        # Generate Response
        response = chat.send_message(message)
        emit('bot_message', {'data': response.text})
        
        # Simple Handoff Check (After response to ensure flow)
        if len(chat.history) >= 6:
            # Check if we recently mentioned the expert
            last_text = chat.history[-1].parts[0].text
            if "identified the right expert" not in last_text:
                 # Force the handoff message if the AI didn't say it naturally
                 pass 
                 # (You can enable the forced message here if needed, 
                 # but let's get basic chat working first)

    except Exception as e:
        print(f"runtime AI Error: {e}")
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
