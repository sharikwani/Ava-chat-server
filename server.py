import os
from flask import Flask, request
from flask_socketio import SocketIO, emit

# THIS LINE IS CRITICAL - It defines 'app' for Gunicorn
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'secret!')

# Enable CORS so your website can connect
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

@app.route('/')
def index():
    return "Ava Chat Server is Running!"

@socketio.on('connect')
def handle_connect():
    print(f'Client connected: {request.sid}')
    emit('bot_message', {'data': 'Hi! I\'m Ava. How can I help you find an expert today?'})

@socketio.on('user_message')
def handle_message(data):
    user_text = data.get('message', '')
    user_id = data.get('user_id', 'unknown')
    print(f"Message from {user_id}: {user_text}")
    
    # Ava's Logic
    response = f"I've received your message: '{user_text}'. Checking for experts..."
    if "dog" in user_text.lower() or "cat" in user_text.lower():
        response = "I see this is a pet issue. Paging a Veterinarian..."
    elif "car" in user_text.lower() or "engine" in user_text.lower():
        response = "Connecting you with a certified Mechanic..."
        
    emit('bot_message', {'data': response})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    socketio.run(app, host='0.0.0.0', port=port)
