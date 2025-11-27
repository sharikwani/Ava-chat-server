import os
    import json
    from flask import Flask, request, jsonify
    from google import genai
    from google.genai import types

    # Initialize the Flask app
    # The application variable is still named 'app', which Gunicorn will use.
    app = Flask(__name__)

    # --- Core Gemini LLM Function ---
    def get_assistant_response(prompt):
        """
        Connects to the Gemini API and gets a response for a given prompt.
        This function expects the GEMINI_API_KEY environment variable to be set.
        """
        # Render will inject the key via environment variables
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            # If the key isn't set, we can't run the AI model.
            return "Error: GEMINI_API_KEY not set. Cannot connect to the AI model."
        
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
            return f"An internal error occurred: {str(e)}"

    # --- Flask Routes ---

    @app.route("/")
    def health_check():
        """Simple health check for Render monitoring."""
        return jsonify({"status": "Ava is running on Render", "service": "Backend"})

    @app.route("/ask", methods=["POST"])
    def ask_assistant():
        """Endpoint to handle user queries."""
        data = request.get_json(silent=True)
        user_prompt = data.get("prompt", "")

        if not user_prompt:
            return jsonify({"response": "Please provide a prompt."}), 400

        # Get response from the LLM
        ai_response = get_assistant_response(user_prompt)

        return jsonify({"response": ai_response})

    # This is here for local testing, but Gunicorn will run the app in production
    if __name__ == "__main__":
        port = int(os.environ.get("PORT", 8080))
        app.run(host="0.0.0.0", port=port)
