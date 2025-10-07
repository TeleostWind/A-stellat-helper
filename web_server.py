import threading
import os
from flask import Flask

# Initialize the Flask application
app = Flask(__name__)

@app.route('/')
def home():
    """Simple health check endpoint for UptimeRobot."""
    # This response tells UptimeRobot that the server is alive.
    return "Bot is running and healthy!"

def run_server():
    """
    Runs the Flask server.
    It binds to 0.0.0.0 and uses the PORT environment variable provided by Railway.
    """
    # Railway sets the PORT automatically.
    port = os.environ.get('PORT', 5000)
    app.run(host='0.0.0.0', port=port) 

def start_server_thread():
    """
    Starts the web server in a separate thread so it doesn't block the Discord bot from running.
    """
    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True # Allows the main process to exit even if this thread is running
    server_thread.start()
    print("Keep-Alive Web Server started in the background.")

if __name__ == '__main__':
    # This block is used only if you run this file directly, but Gunicorn handles it in production.
    pass
