import os
import time
import stripe
import google.generativeai as genai
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'secret!')

# --- ENABLE CORS ---
CORS(app, resources={r"/*": {"origins": "*"}})

# --- STRIPE CONFIGURATION ---
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')

if not stripe.api_key:
    print("WARNING: STRIPE_SECRET_KEY not found.")

# --- SOCKET IO ---
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

# --- GLOBALS ---
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')
active_model = None  # Will be loaded on first request
chat_histories = {}

# --- AI LOADING LOGIC ---
def get_ai_model():
    global active_model
    if active_model:
        return active_model
    
    if not GOOGLE_API_KEY:
        return None

    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        # Try the most stable model first
        candidates = ['gemini-1.5-flash', 'gemini-1.5-flash-001', 'gemini-1.5-pro', 'gemini-pro']
        
        for name in candidates:
            try:
                print(f"Attempting to load AI model: {name}")
                temp_model = genai.GenerativeModel(name)
                # Quick test
                temp_model.generate_content("Hello")
                print(f"SUCCESS: AI Model {name} loaded.")
                active_model = temp_model
                return active_model
            except Exception as inner_e:
                print(f"Failed to load {name}: {inner_e}")
                continue
                
    except Exception as e:
        print(f"Fatal AI Config Error: {e}")
        return None
        
    return None

# --- AVA'S INSTRUCTIONS ---
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
    return "Ava Server (Gevent/LazyLoad) is Running!"

@app.route('/create-payment-intent', methods=['POST'])
def create_payment():
    print("Payment Intent Requested")
    if not stripe.api_key:
        return jsonify({'error': 'Server Error: Stripe Key Missing'}), 500
    try:
        intent = stripe.PaymentIntent.create(
            amount=500,
            currency='usd',
            automatic_payment_methods={'enabled': True},
        )
        return jsonify({'clientSecret': intent.client_secret})
    except Exception as e:
        return jsonify({'error': str(e)}), 403

@socketio.on('connect')
def handle_connect():
    print(f'Client connected: {request.sid}')
    chat_histories[request.sid] = [
        {'role': 'user', 'parts': [SYSTEM_INSTRUCTION]},
        {'role': 'model', 'parts': ["Understood. I will ask 5 questions."]}
    ]
    emit('bot_message', {'data': "Hi! I'm Ava. I can connect you with a verified expert. What problem are you facing today?"})

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in chat_histories:
        del chat_histories[request.sid]

@socketio.on('user_message')
def handle_message(data):
    user_text = data.get('message', '').strip()
    user_id = request.sid
    
    emit('bot_typing', {'status': 'true'})
    time.sleep(3)
    
    history = chat_histories.get(user_id, [])
    history.append({'role': 'user', 'parts': [user_text]})
    
    # Try to get the AI model (Lazy Load)
    model_instance = get_ai_model()
    
    if not model_instance:
        # DEBUG MODE: Tell the user exactly why it failed
        error_reason = "API Key Missing" if not GOOGLE_API_KEY else "Connection Refused to Google"
        emit('bot_message', {'data': f"⚠️ System Error: AI Brain Offline ({error_reason}). Check Render Logs."})
        return

    try:
        response = model_instance.generate_content(history)
        ai_reply = response.text
        
        history.append({'role': 'model', 'parts': [ai_reply]})
        chat_histories[user_id] = history

        if "[PAYMENT_REQUIRED]" in ai_reply:
            clean_reply = ai_reply.replace("[PAYMENT_REQUIRED]", "").strip()
            emit('bot_message', {'data': clean_reply})
            emit('payment_trigger', {'amount': 5.00})
        else:
            emit('bot_message', {'data': ai_reply})

    except Exception as e:
        print(f"AI Execution Error: {e}")
        # Send exact error to chat for debugging
        emit('bot_message', {'data': f"⚠️ AI Error: {str(e)}"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    socketio.run(app, host='0.0.0.0', port=port)
