import os
from flask import Flask, request
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'

# CORS * allows your website to connect from anywhere
socketio = SocketIO(app, cors_allowed_origins="*", logger=True, engineio_logger=True)

@app.route('/')
def index():
    return "Ava Chat Server is Running!"

@socketio.on('connect')
def handle_connect():
    print(f'Client connected: {request.sid}')
    # UPDATED: Greeting from Ava
    emit('bot_message', {'data': 'Hi there! I\'m Ava. I can connect you with a verified expert. What do you need help with today?'})

@socketio.on('user_message')
def handle_message(data):
    user_text = data.get('message', '')
    user_id = data.get('user_id', 'unknown')
    
    print(f"Message from {user_id}: {user_text}")

    # --- AVA'S LOGIC ---
    response = f"Thanks. I've received: '{user_text}'. Let me find the right expert for you..."
    
    # Simple keyword routing
    if "dog" in user_text.lower() or "cat" in user_text.lower():
        response = "I see this is a pet issue. I'm paging a Veterinarian now. Please hold."
    elif "car" in user_text.lower() or "engine" in user_text.lower():
        response = "Got it. Connecting you with a certified Mechanic..."
    elif "legal" in user_text.lower() or "sue" in user_text.lower():
        response = "I understand. I am connecting you with a Lawyer for advice."

    emit('bot_message', {'data': response})

if __name__ == '__main__':
    # Use 0.0.0.0 to allow external connections
    # We use port 8080 as it's commonly open
    port = int(os.environ.get("PORT", 8080))
    print(f"Ava is starting on 0.0.0.0:{port}...")
    socketio.run(app, host='0.0.0.0', port=port)
