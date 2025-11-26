import os
import time
import requests
import stripe
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'secret!')

# --- ENABLE CORS ---
CORS(app, resources={r"/*": {"origins": "*"}})

# --- STRIPE CONFIGURATION ---
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')

# --- SOCKET IO ---
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

# --- AI CONFIGURATION ---
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')

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

def ask_google_brain(history):
    """
    Direct HTTP call to Google Gemini to avoid gRPC/Gevent conflicts.
    """
    if not GOOGLE_API_KEY:
        return "Error: AI Key Missing in Environment Variables"

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GOOGLE_API_KEY}"
    
    # Convert our chat history to Google's REST format
    gemini_contents = []
    for msg in history:
        role = "user" if msg['role'] == "user" else "model"
        gemini_contents.append({
            "role": role,
            "parts": [{"text": msg['parts'][0]}]
        })

    # Construct payload
    payload = {
        "contents": gemini_contents,
        "system_instruction": {
            "parts": [{"text": SYSTEM_INSTRUCTION}]
        }
    }

    try:
        # Standard HTTP Post request with increased timeout
        response = requests.post(url, json=payload, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            # Extract text from the JSON response
            if 'candidates' in data and data['candidates']:
                return data['candidates'][0]['content']['parts'][0]['text']
            else:
                return "I'm thinking..."
        else:
            print(f"Google API Error: {response.status_code} - {response.text}")
            return f"System Error {response.status_code}: {response.text}"
            
    except Exception as e:
        print(f"Connection Error: {e}")
        # Return the ACTUAL error so we can debug it
        return f"System Error: {str(e)}"

# --- MEMORY ---
chat_histories = {}

@app.route('/')
def index():
    return "Ava Server (REST API Version) is Running!"

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
    # Initialize clean history for the API
    chat_histories[request.sid] = [] 
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
    time.sleep(2) # Slightly faster response feel
    
    history = chat_histories.get(user_id, [])
    history.append({'role': 'user', 'parts': [user_text]})
    
    # Get response via REST API
    ai_reply = ask_google_brain(history)
    
    # Save AI reply
    history.append({'role': 'model', 'parts': [ai_reply]})
    chat_histories[user_id] = history

    if "[PAYMENT_REQUIRED]" in ai_reply:
        clean_reply = ai_reply.replace("[PAYMENT_REQUIRED]", "").strip()
        emit('bot_message', {'data': clean_reply})
        emit('payment_trigger', {'amount': 5.00})
    else:
        emit('bot_message', {'data': ai_reply})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    socketio.run(app, host='0.0.0.0', port=port)
