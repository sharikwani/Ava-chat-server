import os
import json
from flask import Flask, request, jsonify
from google import genai
from google.genai import types

# Initialize the Flask app
# The application variable is named 'app', which Gunicorn will look for.
app = Flask(__name__)

# --- Core Gemini LLM Function ---
def get_assistant_response(prompt):
    """
    Connects to the Gemini API and gets a response for a given prompt.
    This function expects the GEMINI_API_KEY environment variable to be set
    securely in the Render dashboard.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        # If the key isn't set, we can't run the AI model.
        return "Error: GEMINI_API_KEY environment variable not set. Please check Render secrets."
    
    try:
        # Initialize the client with the API key
        client = genai.Client(api_key=api_key)

        # Define the AI's persona and settings
        config = types.GenerateContentConfig(
            system_instruction="You are Ava, a helpful, cheerful, and insightful personal assistant. Keep your responses concise and focused.",
            temperature=0.7,
        )

        # Call the model
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=config,
        )
        
        return response.text

    except Exception as e:
        app.logger.error(f"Gemini API Error: {e}")
        return f"An internal error occurred while communicating with the AI: {str(e)}"

# --- Flask Routes ---

@app.route("/")
def health_check():
    """
    A simple GET endpoint used by Render's health checks.
    It confirms the application is running and accessible.
    """
    return jsonify({"status": "Ava is running on Render", "service": "Backend is healthy"})

@app.route("/ask", methods=["POST"])
def ask_assistant():
    """
    The main POST endpoint for receiving a user query and returning the AI's response.
    It expects a JSON payload like: {"prompt": "What is the meaning of life?"}
    """
    # Parse the incoming JSON data
    data = request.get_json(silent=True)
    user_prompt = data.get("prompt", "")

    if not user_prompt:
        return jsonify({"response": "Error: Missing 'prompt' in the request body."}), 400

    # Get response from the LLM
    ai_response = get_assistant_response(user_prompt)

    return jsonify({"response": ai_response})

# Local Development Server Configuration
if __name__ == "__main__":
    # This ensures the local Flask development server runs on the correct host/port
    # but Gunicorn is used in the Render production environment.
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
