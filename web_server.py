import threading
from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    """Simple health check endpoint for UptimeRobot."""
    # This response lets the monitoring service know the bot is alive.
    return "Bot is running and healthy!"

def run_server():
    """Runs the Flask server on a separate thread."""
    # The port needs to be set to whatever Railway dynamically assigns (usually retrieved from environment).
    # We use 8080 as a default, but Railway handles the binding.
    app.run(host='0.0.0.0', port=8080) 

def start_server_thread():
    """Starts the web server in the background so the main bot thread isn't blocked."""
    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True # Allows the main program to exit even if this thread is running
    server_thread.start()
    print("Keep-Alive Web Server started in the background.")

if __name__ == '__main__':
    # If run directly, just starts the server
    run_server()
