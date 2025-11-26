import os
import time
import stripe
import google.generativeai as genai
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'secret!')

# --- ENABLE CORS FOR HTTP ROUTES ---
# This fixes the "Payment Form Not Loading" issue
CORS(app, resources={r"/*": {"origins": "*"}})

# --- STRIPE CONFIGURATION ---
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')

# Enable SocketIO
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --- ROBUST AI CONFIGURATION ---
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')
model = None

def configure_robust_ai():
    if not GOOGLE_API_KEY:
        print("FATAL: GOOGLE_API_KEY not found.")
        return None

    genai.configure(api_key=GOOGLE_API_KEY)
    
    candidates = [
        'gemini-1.5-flash',
        'gemini-1.5-flash-001',
        'gemini-1.5-pro',
        'gemini-pro',
        'gemini-1.0-pro'
    ]
    
    try:
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                clean_name = m.name.replace('models/', '')
                if clean_name not in candidates:
                    if 'preview' not in clean_name and 'exp' not in clean_name:
                        candidates.append(clean_name)
    except Exception as e:
        print(f"Warning: Could not list models: {e}")

    for name in candidates:
        try:
            print(f"Testing: {name}...")
            temp_model = genai.GenerativeModel(name)
            temp_model.generate_content("Test")
            print(f"SUCCESS: Connected to {name}")
            return temp_model
        except Exception as e:
            print(f"Failed {name}: {e}")
    
    print("ALL MODELS FAILED. AI IS OFFLINE.")
    return None

model = configure_robust_ai()
chat_histories = {}

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
7. Mention the $5 refundable fee before triggering the tag.
"""

@app.route('/')
def index():
    status = "Online" if model else "AI Offline"
    return f"Ava AI & Payment Server is Running! AI Status: {status}"

# --- PAYMENT ROUTE ---
@app.route('/create-payment-intent', methods=['POST'])
def create_payment():
    try:
        # Create a PaymentIntent with the order amount and currency
        intent = stripe.PaymentIntent.create(
            amount=500, # $5.00
            currency='usd',
            automatic_payment_methods={'enabled': True},
        )
        return jsonify({'clientSecret': intent.client_secret})
    except Exception as e:
        print(f"Stripe Error: {str(e)}")
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
    
    try:
        if model:
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
            emit('bot_message', {'data': "System Error: AI Brain Offline."})

    except Exception as e:
        print(f"AI Error: {e}")
        emit('bot_message', {'data': "I'm having a slight connection issue. Could you repeat that?"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    socketio.run(app, host='0.0.0.0', port=port)
