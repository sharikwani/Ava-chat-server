import eventlet
eventlet.monkey_patch()

import os
import time
import stripe
import google.generativeai as genai
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS

app = Flask(__name__)

# Secret key for sessions (use env var in production)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "secret!")

# Enable CORS for all routes (you can restrict this later if needed)
CORS(app, resources={r"/*": {"origins": "*"}})

# --- STRIPE CONFIG ---
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
if not stripe.api_key:
    print("WARNING: STRIPE_SECRET_KEY not found. Payments will fail.")

# --- SOCKET.IO CONFIG (eventlet async mode) ---
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# --- GEMINI / GOOGLE AI CONFIG ---
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
model = None


def configure_robust_ai():
    """
    Configure the Gemini client and pick a working model.
    If anything fails, return None and keep the server running.
    """
    if not GOOGLE_API_KEY:
        print("FATAL: GOOGLE_API_KEY not found. AI will be offline.")
        return None

    try:
        genai.configure(api_key=GOOGLE_API_KEY)

        # Try models in this order
        candidate_models = [
            "gemini-1.5-flash",
            "gemini-1.5-flash-001",
            "gemini-1.5-pro",
            "gemini-pro",
        ]

        for name in candidate_models:
            try:
                print(f"[AI] Testing model: {name} ...")
                test_model = genai.GenerativeModel(name)

                # Light sanity test – avoid heavy prompt
                resp = test_model.generate_content("ping")
                # If we got here without exception, we assume it's fine
                print(f"[AI] SUCCESS: Using model '{name}'")
                return test_model
            except Exception as e:
                print(f"[AI] Failed model '{name}': {e}")

        print("[AI] ALL MODELS FAILED. AI is OFFLINE.")
        return None

    except Exception as e:
        print(f"[AI] FATAL AI CLIENT ERROR: {e}")
        return None


model = configure_robust_ai()

# Per-connection chat histories
chat_histories = {}

# --- AVA PROMPT / SYSTEM INSTRUCTION ---
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


# --- BASIC HEALTH CHECK ROUTE ---
@app.route("/")
def index():
    ai_status = "Online" if model else "Offline"
    return f"Ava Server Running. AI status: {ai_status}"


# --- STRIPE PAYMENT INTENT ROUTE ---
@app.route("/create-payment-intent", methods=["POST"])
def create_payment():
    print("💰 Payment Intent Requested")
    if not stripe.api_key:
        return jsonify({"error": "Server Error: Payment key missing or invalid"}), 500

    try:
        intent = stripe.PaymentIntent.create(
            amount=500,  # $5.00 in cents
            currency="usd",
            automatic_payment_methods={"enabled": True},
        )
        print(f"✅ Stripe Intent Created: {intent.id}")
        return jsonify({"clientSecret": intent.client_secret})
    except Exception as e:
        print(f"❌ Stripe Error: {str(e)}")
        return jsonify({"error": str(e)}), 403


# --- SOCKET.IO EVENTS ---

@socketio.on("connect")
def handle_connect():
    sid = request.sid
    print(f"[SocketIO] Client connected: {sid}")

    # Initialize chat history with system instruction + internal ack
    chat_histories[sid] = [
        {"role": "user", "parts": [SYSTEM_INSTRUCTION]},
        {"role": "model", "parts": ["Understood. I will ask 5 questions."]},
    ]

    emit(
        "bot_message",
        {
            "data": "Hi! I'm Ava. I can connect you with a verified expert. "
                    "What problem are you facing today?"
        },
    )


@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid
    if sid in chat_histories:
        del chat_histories[sid]
    print(f"[SocketIO] Client disconnected: {sid}")


@socketio.on("user_message")
def handle_user_message(data):
    sid = request.sid
    user_text = (data or {}).get("message", "").strip()

    if not user_text:
        emit("bot_message", {"data": "I didn’t catch that. Could you type it again?"})
        return

    # Typing indicator on
    emit("bot_typing", {"status": "true"})

    # Natural delay (short)
    time.sleep(1)

    # Ensure history exists for this sid
    history = chat_histories.get(sid)
    if history is None:
        history = [
            {"role": "user", "parts": [SYSTEM_INSTRUCTION]},
            {"role": "model", "parts": ["Understood. I will ask 5 questions."]},
        ]

    # Append user message
    history.append({"role": "user", "parts": [user_text]})

    # Call AI
    try:
        if model:
            response = model.generate_content(history)
            ai_reply = response.text or ""

            # Append AI reply to history
            history.append({"role": "model", "parts": [ai_reply]})
            chat_histories[sid] = history

            # Check for payment trigger marker
            if "[PAYMENT_REQUIRED]" in ai_reply:
                clean_reply = ai_reply.replace("[PAYMENT_REQUIRED]", "").strip()
                emit("bot_message", {"data": clean_reply})
                emit("payment_trigger", {"amount": 5.00})
            else:
                emit("bot_message", {"data": ai_reply})
        else:
            emit(
                "bot_message",
                {
                    "data": "System Error: AI brain is currently offline. "
                            "Please try again later or contact support."
                },
            )

    except Exception as e:
        print(f"[AI] Error while generating response: {e}")
        emit(
            "bot_message",
            {
                "data": "I'm having a slight connection issue with the AI. "
                        "Could you repeat that or try again in a moment?"
            },
        )


# --- LOCAL DEV ENTRYPOINT ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"Starting Ava server on port {port} ...")
    # For local run: python server.py
    socketio.run(app, host="0.0.0.0", port=port, debug=False)

