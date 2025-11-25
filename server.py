import os
from flask import Flask, request
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'secret!')

# Enable CORS so your website can connect
# async_mode='eventlet' is crucial for performance on Render
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# MEMORY: Stores the conversation state for each user
# Structure: { 'user_id': { 'state': 'INITIAL', 'data': {} } }
user_sessions = {}

@app.route('/')
def index():
    return "Ava Smart Chat Server is Running!"

@socketio.on('connect')
def handle_connect():
    print(f'Client connected: {request.sid}')
    # Initialize user state
    user_sessions[request.sid] = {'state': 'INITIAL', 'context': {}}
    emit('bot_message', {'data': 'Welcome! I\'m Ava, the Expert\'s Assistant. How can I help with your important question today?'})

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in user_sessions:
        del user_sessions[request.sid]

@socketio.on('user_message')
def handle_message(data):
    user_text = data.get('message', '').strip()
    # We use request.sid as the unique user ID
    user_id = request.sid
    
    # Get user's current state (default to INITIAL if missing)
    session = user_sessions.get(user_id, {'state': 'INITIAL', 'context': {}})
    state = session['state']
    
    # Default fallback response
    response = "I can connect you to the right expert. Is this question regarding a **Car**, a **Pet**, or a **Legal** issue?"

    print(f"User: {user_id} | State: {state} | Message: {user_text}")

    # --- STATE MACHINE LOGIC ---

    # 1. START: Detect the problem type
    if state == 'INITIAL':
        text_lower = user_text.lower()
        if any(word in text_lower for word in ['car', 'start', 'engine', 'battery', 'noise', 'brake', 'toyota', 'honda', 'ford', 'truck', 'vehicle']):
            response = "I'm sorry to hear your vehicle is having trouble. Could you please tell me the **make and model**? (e.g., Toyota Tundra 2019)"
            session['state'] = 'CAR_GET_MODEL'
        
        elif any(word in text_lower for word in ['dog', 'cat', 'pet', 'vomit', 'ate', 'puppy', 'kitten']):
            response = "I'm sorry to hear your pet isn't feeling well. What **breed and age** is your pet?"
            session['state'] = 'VET_GET_DETAILS'
            
        elif any(word in text_lower for word in ['law', 'legal', 'sue', 'court', 'contract', 'divorce']):
            response = "I understand this is a legal matter. What **state or country** are you located in?"
            session['state'] = 'LEGAL_GET_LOCATION'

    # --- CAR PATH ---
    elif state == 'CAR_GET_MODEL':
        session['context']['model'] = user_text
        response = f"Thank you for sharing that it's a {user_text}. Can you tell me if you're hearing any sounds, like **clicking or cranking**, when you try to start it?"
        session['state'] = 'CAR_GET_SYMPTOMS'

    elif state == 'CAR_GET_SYMPTOMS':
        session['context']['symptom'] = user_text
        response = "Got it. How long has this issue been happening?"
        session['state'] = 'CAR_GET_DURATION'

    elif state == 'CAR_GET_DURATION':
        session['context']['duration'] = user_text
        response = "Thank you. Have you noticed any other issues recently, like **dimming lights** or problems with the **battery**?"
        session['state'] = 'CAR_GET_BATTERY'

    elif state == 'CAR_GET_BATTERY':
        response = f"OK. Thanks for the info. A certified Mechanic can help diagnose why your {session['context'].get('model', 'car')} is having these issues. I am connecting you now..."
        session['state'] = 'FINISHED'

    # --- VET PATH ---
    elif state == 'VET_GET_DETAILS':
        response = "Thank you. Is your pet showing any signs of **lethargy, vomiting, or not eating**?"
        session['state'] = 'VET_GET_SYMPTOMS'

    elif state == 'VET_GET_SYMPTOMS':
        response = "I understand. An experienced Veterinarian will be able to advise you on this immediately. Connecting you now..."
        session['state'] = 'FINISHED'

    # --- LEGAL PATH ---
    elif state == 'LEGAL_GET_LOCATION':
        response = "Thank you. Could you briefly describe the legal issue (e.g., contract dispute, traffic ticket)?"
        session['state'] = 'LEGAL_GET_ISSUE'
        
    elif state == 'LEGAL_GET_ISSUE':
        response = "Understood. I am connecting you with a lawyer specialized in your area now."
        session['state'] = 'FINISHED'

    # --- FINAL STATE ---
    elif state == 'FINISHED':
        response = "An expert has been notified and is reviewing your details. They will join this chat in approximately 2 minutes."

    # Update the session memory
    user_sessions[user_id] = session
    
    # Send reply
    emit('bot_message', {'data': response})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    socketio.run(app, host='0.0.0.0', port=port)
