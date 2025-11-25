import os
import google.generativeai as genai
from flask import Flask, request
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'secret!')

# Enable CORS so your website (and cPanel) can connect
# async_mode='eventlet' is crucial for performance on Render
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --- 1. CONFIGURE AI ---
# We get the key from Render Environment Variables
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')

if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel('gemini-pro')
else:
    print("WARNING: GOOGLE_API_KEY not found. AI features will not work.")

# --- 2. MEMORY STORAGE ---
# Stores the chat history for each user so Ava remembers context
chat_histories = {}

# --- 3. AVA'S PERSONALITY (System Prompt) ---
SYSTEM_INSTRUCTION = """
You are Ava, a friendly, professional, and empathetic Expert's Assistant for 'HelpByExperts'.
Your goal is to triage the customer's problem before connecting them to a human expert.

INSTRUCTIONS:
1.  **Greet & Triage:** When the user states a problem, ask 2-3 relevant clarifying questions to understand the severity and details (e.g., for a car: model, symptoms; for medical: duration, pain level).
2.  **Be Empathetic:** Use phrases like "I'm sorry to hear that" or "That sounds stressful."
3.  **Do NOT Solve:** Never offer specific medical, legal, or mechanical advice. You are just the assistant gathering info.
4.  **The Hand-off:** Once you have gathered 2-3 pieces of info, tell the user you have found a verified expert who can solve this immediately.
5.  **TRIGGER PAYMENT:** When you are ready to connect them, you MUST end your final message with this exact tag: [PAYMENT_REQUIRED]
6.  **Mention Fee:** Before the tag, mention there is a fully refundable $5 expert connection fee.
"""

@app.route('/')
def index():
    return "Ava AI Brain is Running!"

@socketio.on('connect')
def handle_connect():
    print(f'Client connected: {request.sid}')
    # Initialize history with the System Prompt
    chat_histories[request.sid] = [
        {'role': 'user', 'parts': [SYSTEM_INSTRUCTION]},
        {'role': 'model', 'parts': ["Understood. I am ready to act as Ava."]}
    ]
    # Initial Greeting
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

    # Retrieve this user's history
    history = chat_histories.get(user_id, [])
    
    # Add user's message to history
    history.append({'role': 'user', 'parts': [user_text]})
    
    try:
        if GOOGLE_API_KEY:
            # Send history to Google Gemini
            response = model.generate_content(history)
            ai_reply = response.text
            
            # Add AI reply to history
            history.append({'role': 'model', 'parts': [ai_reply]})
            chat_histories[user_id] = history

            # --- CHECK FOR PAYMENT TRIGGER ---
            if "[PAYMENT_REQUIRED]" in ai_reply:
                # Remove the tag so the user doesn't see weird text
                clean_reply = ai_reply.replace("[PAYMENT_REQUIRED]", "").strip()
                
                # 1. Send the text message
                emit('bot_message', {'data': clean_reply})
                
                # 2. Send the invisible signal to show the button
                print(f"Triggering Payment for {user_id}")
                emit('payment_trigger', {'amount': 5.00})
            else:
                # Normal reply
                emit('bot_message', {'data': ai_reply})
        else:
            # Fallback if no API key
            emit('bot_message', {'data': "I'm sorry, my AI brain is currently offline. Please check the server configuration."})

    except Exception as e:
        print(f"AI Error: {e}")
        emit('bot_message', {'data': "I'm having a little trouble connecting to the server. Could you say that again?"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    socketio.run(app, host='0.0.0.0', port=port)
