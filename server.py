# --- 1. CRITICAL: Eventlet monkey patch must be FIRST ---
import eventlet
eventlet.monkey_patch()

import os
import time
import stripe
import google.generativeai as genai
from flask import Flask, request, jsonify, request as flask_request
from flask_socketio import SocketIO, emit
from flask_cors import CORS

# ---------- FLASK APP ----------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "secret!")

CORS(app, resources={r"/*": {"origins": "*"}})

# ---------- STRIPE CONFIG ----------
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
if not stripe.api_key:
    print("WARNING: STRIPE_SECRET_KEY not found. Payments will fail.")

# ---------- SOCKET.IO CONFIG ----------
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ---------- GEMINI / GOOGLE AI CONFIG ----------
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")

model = None
AI_STATUS = "Initializing"
AI_ERROR = None
AI_MODEL_NAME = None


def configure_robust_ai():
    """
    Configure Gemini. If it fails, we still run the server and use a rule-based fallback.
    """
    global AI_STATUS, AI_ERROR, AI_MODEL_NAME

    if not GOOGLE_API_KEY:
        AI_STATUS = "Offline"
        AI_ERROR = "GOOGLE_API_KEY environment variable is missing"
        print("[AI] FATAL: GOOGLE_API_KEY not found. AI will be offline.")
        return None

    try:
        genai.configure(api_key=GOOGLE_API_KEY)

        # v1 model names for google-generativeai >= 0.8.x
        candidate_models = [
            "gemini-1.5-flash-latest",
            "gemini-1.5-pro-latest",
            "gemini-1.5-flash",
            "gemini-1.5-pro",
            "gemini-1.0-pro",
        ]

        for name in candidate_models:
            try:
                print(f"[AI] Testing model: {name} ...")
                test_model = genai.GenerativeModel(name)
                # Very light sanity test
                _ = test_model.generate_content("ping")
                print(f"[AI] SUCCESS: Using model '{name}'")
                AI_STATUS = "Online"
                AI_ERROR = None
                AI_MODEL_NAME = name
                return test_model
            except Exception as e:
                print(f"[AI] Failed model '{name}': {e}")
                AI_STATUS = "Degraded"
                AI_ERROR = f"Last failed model: {name} – {e}"

        print("[AI] ALL MODELS FAILED. AI is OFFLINE.")
        if not AI_ERROR:
            AI_ERROR = "All candidate models failed."
        AI_STATUS = "Offline"
        return None

    except Exception as e:
        print(f"[AI] FATAL AI CLIENT ERROR: {e}")
        AI_STATUS = "Offline"
        AI_ERROR = f"Fatal AI client error: {e}"
        return None


model = configure_robust_ai()

# ---------- STATE: CHAT HISTORIES & FALLBACK FLOW ----------
chat_histories = {}

# For rule-based fallback Ava when AI is offline
fallback_questions = [
    "Can you briefly describe the main problem you’re facing?",
    "How long has this issue been happening?",
    "Have you already tried anything to fix it? If yes, what?",
    "Is there any device, website, or service involved (for example: Dell PC, Norton, banking app, etc.)?",
    "How urgent is this for you on a scale from 1 (not urgent) to 5 (very urgent)?",
]

# track per-socket fallback state
fallback_state = {}  # sid -> {"step": int, "done": bool}

# ---------- AVA SYSTEM INSTRUCTION (for real AI) ----------
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


# ---------- BASIC ROUTES ----------
@app.route("/")
def index():
    return f"Ava Server Running. AI status: {AI_STATUS} (model={AI_MODEL_NAME})"


@app.route("/ai-status")
def ai_status():
    return jsonify(
        {
            "ai_status": AI_STATUS,
            "ai_model": AI_MODEL_NAME,
            "ai_error": AI_ERROR,
            "has_google_api_key_env": bool(GOOGLE_API_KEY),
        }
    )


@app.route("/create-payment-intent", methods=["POST"])
def create_payment():
    print("💰 Payment Intent Requested")
    if not stripe.api_key:
        return jsonify({"error": "Server Error: Payment key missing or invalid"}), 500

    try:
        intent = stripe.PaymentIntent.create(
            amount=500,  # 500 cents = $5
            currency="usd",
            automatic_payment_methods={"enabled": True},
        )
        print(f"✅ Stripe Intent Created: {intent.id}")
        return jsonify({"clientSecret": intent.client_secret})
    except Exception as e:
        print(f"❌ Stripe Error: {str(e)}")
        return jsonify({"error": str(e)}), 403


# ---------- SOCKET.IO HANDLERS ----------

@socketio.on("connect")
def handle_connect():
    sid = flask_request.sid
    print(f"[SocketIO] Client connected: {sid}")

    # init AI history
    chat_histories[sid] = [
        {"role": "user", "parts": [SYSTEM_INSTRUCTION]},
        {"role": "model", "parts": ["Understood. I will ask 5 questions."]},
    ]

    # init fallback state
    fallback_state[sid] = {"step": 0, "done": False}

    emit(
        "bot_message",
        {
            "data": "Hi! I'm Ava. I can connect you with a verified expert. "
                    "What problem are you facing today?"
        },
    )


@socketio.on("disconnect")
def handle_disconnect():
    sid = flask_request.sid
    if sid in chat_histories:
        del chat_histories[sid]
    if sid in fallback_state:
        del fallback_state_
