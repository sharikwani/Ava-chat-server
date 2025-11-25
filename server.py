import os
import google.generativeai as genai
from flask import Flask, request
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'secret!')

# Enable CORS
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --- 2. ROBUST AI CONFIGURATION ---
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')
model = None

def configure_robust_ai():
    """
    Tries multiple model names to find one that works for this API Key/Region.
    """
    if not GOOGLE_API_KEY:
        print("FATAL: GOOGLE_API_KEY not found.")
        return None

    genai.configure(api_key=GOOGLE_API_KEY)
    
    # Priority list of models to try
    candidates = [
        'gemini-1.5-flash',
        'gemini-1.5-flash-001',
        'gemini-1.5-pro',
        'gemini-pro',
        'gemini-1.0-pro'
    ]
    
    # Also ask Google what is available
    try:
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                clean_name = m.name.replace('models/', '')
                if clean_name not in candidates:
                    candidates.append(clean_name)
    except Exception as e:
        print(f"Warning: Could not list models: {e}")

    print(f"Testing models: {candidates}")

    # Test each candidate
    for name in candidates:
        try:
            print(f"Testing: {name}...")
            temp_model = genai.GenerativeModel(name)
            # Fire a test prompt
            temp_model.generate_content("Test")
            print(f"SUCCESS: Connected to {name}")
            return temp_model
        except Exception as e:
            print(f"Failed {name}: {e}")
    
    print("ALL MODELS FAILED. AI IS OFFLINE.")
    return None

# Initialize the best working model
model = configure_robust_ai()

# --- 3. MEMORY STORAGE ---
chat_histories = {}

# --- 4. AVA'S INSTRUCTIONS ---
SYSTEM_INSTRUCTION = """
You are Ava, the expert assistant for 'HelpByExperts'.
Your goal: Triage the user's problem before connecting them to a human.

RULES:
1. Greet the user and ask 2-3 clarifying questions about their issue.
2. Be professional and empathetic.
3. Do NOT give the final solution.
4. After you have enough info, tell them: "I have found a verified expert who can solve this."
5. CRITICAL: When ready to connect, end message with: [PAYMENT_REQUIRED]
6. Mention the $5 refundable fee before triggering the tag.
"""

@app.route('/')
def index():
    status = "AI Online" if model else "AI Offline (Check Logs)"
    return f"Ava Server is Running! Status: {status}"

@socketio.on('connect')
def handle_connect():
    print(f'Client connected: {request.sid}')
    chat_histories[request.sid] = [
        {'role': 'user', 'parts': [SYSTEM_INSTRUCTION]},
        {'role': 'model', 'parts': ["Understood. I am ready to act as Ava."]}
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
            emit('bot_message', {'data': "System Error: No working AI model found. Please check server logs."})

    except Exception as e:
        print(f"AI Error: {e}")
        emit('bot_message', {'data': f"Error: {str(e)}"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    socketio.run(app, host='0.0.0.0', port=port)
