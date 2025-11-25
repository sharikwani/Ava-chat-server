import os
import google.generativeai as genai
from flask import Flask, request
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'secret!')

# Enable CORS so your website (and cPanel) can connect
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --- 2. CONFIGURE AI ---
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')

if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
    # Using gemini-1.5-flash which is the current standard
    model = genai.GenerativeModel('gemini-1.5-flash')
else:
    print("WARNING: GOOGLE_API_KEY not found. Ava will be lobotomized.")

# --- 3. MEMORY STORAGE ---
chat_histories = {}

# --- 4. AVA'S INSTRUCTIONS (System Prompt) ---
SYSTEM_INSTRUCTION = """
You are Ava, the expert assistant for 'HelpByExperts'.
Your goal: Triage the user's problem before connecting them to a human.

RULES:
1. Greet the user and ask 2-3 clarifying questions about their issue (e.g., for car: model/year/noise; for medical: symptoms/duration).
2. Be professional and empathetic.
3. Do NOT give the final solution. You are just gathering info.
4. After you have enough info (usually 3 exchanges), tell them: "I have found a verified expert who can solve this."
5. CRITICAL: When you are ready to connect, end your message with: [PAYMENT_REQUIRED]
6. Mention the $5 fully refundable expert connection fee before triggering the tag.
"""

@app.route('/')
def index():
    return "Ava AI Brain is Live (Debug Mode)!"

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
    print(f'Client disconnected: {request.sid}')

@socketio.on('user_message')
def handle_message(data):
    user_text = data.get('message', '').strip()
    user_id = request.sid
    
    print(f"User ({user_id}): {user_text}")

    history = chat_histories.get(user_id, [])
    history.append({'role': 'user', 'parts': [user_text]})
    
    try:
        if GOOGLE_API_KEY:
            response = model.generate_content(history)
            ai_reply = response.text
            
            history.append({'role': 'model', 'parts': [ai_reply]})
            chat_histories[user_id] = history

            if "[PAYMENT_REQUIRED]" in ai_reply:
                clean_reply = ai_reply.replace("[PAYMENT_REQUIRED]", "").strip()
                emit('bot_message', {'data': clean_reply})
                print(f"Triggering Payment for {user_id}")
                emit('payment_trigger', {'amount': 5.00})
            else:
                emit('bot_message', {'data': ai_reply})
        else:
            emit('bot_message', {'data': "⚠️ Error: GOOGLE_API_KEY not found in Render Environment Variables."})

    except Exception as e:
        print(f"AI Error: {e}")
        # DEBUG: Send the actual error to the frontend so the user can see it
        error_msg = f"⚠️ System Error: {str(e)}"
        emit('bot_message', {'data': error_msg})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    socketio.run(app, host='0.0.0.0', port=port)
