# --- 1. CRITICAL FIX: Monkey Patch must be FIRST ---
# Eventlet is required for flask-socketio and is monkey-patched here.
import eventlet
eventlet.monkey_patch()

import os
import time
import stripe
import google.generativeai as genai  # Module that was failing to import
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS

app = Flask(__app_id) # Using __app_id for uniqueness
# Load secret from environment; falls back to 'secret!' if not set (development)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'secret!')

# --- ENABLE CORS for Payment Route ---
CORS(app, resources={r"/*": {"origins": "*"}})

# --- STRIPE CONFIGURATION (SECURE) ---
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')

if not stripe.api_key:
    print("WARNING: STRIPE_SECRET_KEY not found. Payments will fail.")

# Enable SocketIO
# We explicitly set async_mode='eventlet' to work with the monkey patch.
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --- ROBUST AI CONFIGURATION ---
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')
model = None

def configure_robust_ai():
    """Configures the Gemini client and finds a working model."""
    if not GOOGLE_API_KEY:
        print("FATAL: GOOGLE_API_KEY not found.")
        return None

    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        
        # Priority list of stable models for chat
        candidates = ['gemini-1.5-flash', 'gemini-1.5-flash-001', 'gemini-1.5-pro', 'gemini-pro']
        
        # Test each candidate until one is successfully initialized and responds
        for name in candidates:
            try:
                print(f"Testing: {name}...")
                temp_model = genai.GenerativeModel(name)
                # Quick test to ensure connectivity
                temp_model.generate_content("Test", stream=False)
                print(f"SUCCESS: Connected to {name}")
                return temp_model
            except Exception as e:
                print(f"Failed {name}: {e}")
        
        print("ALL MODELS FAILED. AI IS OFFLINE.")
        return None
        
    except Exception as e:
        print(f"FATAL AI CLIENT ERROR: {e}")
        return None

model = configure_robust_ai()
chat_histories = {}

# --- AVA'S STRICT INSTRUCTIONS ---
SYSTEM_INSTRUCTION = """
You are Ava, the senior triage assistant for 'HelpByExperts'.
Your goal is to gather a COMPLETE case history before finding an expert.

RULES:
1. You MUST ask exactly 5 relevant clarifying questions, ONE BY ONE.
2. Do not connect the user until you have asked all 5 questions.
3. KEEP QUESTIONS SHORT (max 1 sentence).
4. Be professional and empathetic.
5. After the 5th answer, ask: "Is there anything else I should know before I connect you?"
6. Once they answer that final check, end your message with: [PAYMENT_REQUIRED]
7. PAYMENT RULE: You must explicitly state that the $5 fee is refundable ONLY if the customer is not satisfied with the answer. Do not imply it is automatically refundable.
"""

@app.route('/')
def index():
    ai_status = "Online" if model else "Offline"
    return f"Ava Server Running. AI: {ai_status}"

# --- PAYMENT ROUTE ---
@app.route('/create-payment-intent', methods=['POST'])
def create_payment():
    print("💰 Payment Intent Requested")
    if not stripe.api_key:
        return jsonify({'error': 'Server Error: Payment key missing or invalid'}), 500
        
    try:
        intent = stripe.PaymentIntent.create(
            amount=500, # $5.00
            currency='usd',
            automatic_payment_methods={'enabled': True},
        )
        print(f"✅ Intent Created: {intent.id}")
        return jsonify({'clientSecret': intent.client_secret})
    except Exception as e:
        print(f"❌ Stripe Error: {str(e)}")
        return jsonify({'error': str(e)}), 403

@socketio.on('connect')
def handle_connect():
    print(f'Client connected: {request.sid}')
    # Initialize chat history with system instructions
    chat_histories[request.sid] = [
        {'role': 'user', 'parts': [SYSTEM_INSTRUCTION]},
        {'role': 'model', 'parts': ["Understood. I will ask 5 questions."]}
    ]
    emit('bot_message', {'data': "Hi! I'm Ava. I can connect you with a verified expert. What problem are you facing today?"})

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in chat_histories:
        del chat_histories[request.sid]
        print(f'Client disconnected: {request.sid}')

@socketio.on('user_message')
def handle_message(data):
    user_text = data.get('message', '').strip()
    user_id = request.sid
    
    # Typing Indicator
    emit('bot_typing', {'status': 'true'})
    # Natural Delay
    time.sleep(1) # Reduced delay for quicker interaction
    
    history = chat_histories.get(user_id, [])
    history.append({'role': 'user', 'parts': [user_text]})
    
    try:
        if model:
            # Pass the full history for contextual generation
            response = model.generate_content(history)
            ai_reply = response.text
            
            history.append({'role': 'model', 'parts': [ai_reply]})
            chat_histories[user_id] = history

            if "[PAYMENT_REQUIRED]" in ai_reply:
                clean_reply = ai_reply.replace("[PAYMENT_REQUIRED]", "").strip()
                emit('bot_message', {'data': clean_reply})
                emit('payment_trigger', {'amount': 5.00})
            else:
                emit('bot_message', {'data': ai_reply})
        else:
            emit('bot_message', {'data': "System Error: AI Brain Offline. (Check API Key and Model status)"})

    except Exception as e:
        print(f"AI Error: {e}")
        emit('bot_message', {'data': "I'm having a slight connection issue with the AI. Could you repeat that?"})
        
        
# This block is what a production server should use to run SocketIO correctly with eventlet.
# However, many PAAS environments use the Gunicorn command line, which we cannot change here.
# Leaving this here for local testing guidance.
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    # We use eventlet's WSGI server directly when running locally/manually.
    print(f"Starting server on port {port}...")
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
