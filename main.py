import os
import discord
import asyncio
from web_server import start_server_thread 
from discord.ext import tasks
from aiohttp import ClientSession, ClientConnectorError
import json
import time

# --- Configuration ---
# Load environment variables (set in Railway dashboard)
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# --- Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True 
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)

# --- State Management ---
class BotState:
    def __init__(self):
        self.scheduled_task = None
        self.last_channel_activity_time = time.time()
        self.last_bot_send_time = time.time()
        self.scheduled_message_content = ""
        self.scheduled_channel_id = None
        self.is_automatic = False
        self.ai_prompt = ""
        self.interval_hours = 0
        self.interval_seconds = 0

BOT_STATE = BotState()

# --- AI Service Functions ---

async def call_gemini_api(prompt):
    """
    Calls the Gemini API to generate content based on a prompt.
    Uses exponential backoff for retries.
    """
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY is missing. Cannot use AI mode.")
        return "Error: Gemini API Key not configured."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-05-20:generateContent?key={GEMINI_API_KEY}"
    
    system_prompt = "You are a fun, engaging, and concise community announcer bot. Generate a short, relevant message based on the user's prompt. Do not use markdown titles or headers, just plain text."
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with ClientSession() as session:
                async with session.post(url, headers={'Content-Type': 'application/json'}, json=payload) as response:
                    if response.status == 200:
                        result = await response.json()
                        text = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', 'AI failed to generate a response.')
                        return text
                    elif response.status == 429:
                        print(f"Rate limit hit. Retrying in {2**attempt} seconds...")
                        await asyncio.sleep(2**attempt)
                    else:
                        error_text = await response.text()
                        print(f"API Error (Status {response.status}): {error_text}")
                        return f"Error: AI service returned status {response.status}"
        except ClientConnectorError as e:
            print(f"Connection Error: {e}. Retrying...")
            await asyncio.sleep(2**attempt)
        except Exception as e:
            print(f"An unexpected error occurred during API call: {e}")
            break

    return "Error: Failed to connect to AI service after multiple retries."


# --- Background Task ---

@tasks.loop(seconds=1) 
async def send_scheduled_message():
    if BOT_STATE.scheduled_channel_id is None or BOT_STATE.interval_seconds == 0:
        return

    if time.time() - BOT_STATE.last_bot_send_time >= BOT_STATE.interval_seconds:
        
        # Anti-Stacking Logic: Skip if channel has been idle since the last bot message
        if BOT_STATE.last_channel_activity_time <= BOT_STATE.last_bot_send_time:
            print("Channel is idle since last bot message. Skipping scheduled message to prevent spam.")
            BOT_STATE.last_bot_send_time = time.time()
            return

        channel = client.get_channel(BOT_STATE.scheduled_channel_id)
        if not channel:
            print(f"Error: Channel with ID {BOT_STATE.scheduled_channel_id} not found.")
            return

        message_to_send = ""

        if BOT_STATE.is_automatic:
            message_to_send = await call_gemini_api(BOT_STATE.ai_prompt)
        else:
            message_to_send = BOT_STATE.scheduled_message_content

        try:
            await channel.send(f"**[Scheduled Announcement]** {message_to_send}")
            BOT_STATE.last_bot_send_time = time.time()
            print(f"Scheduled message sent at: {time.ctime()}")
        except discord.Forbidden:
            print("Error: Missing permissions to send message to the channel.")
        except Exception as e:
            print(f"An error occurred while sending the message: {e}")


# --- Discord Events ---

@client.event
async def on_ready():
    await tree.sync()
    print(f'Logged in as {client.user} (ID: {client.user.id})')
    print('Bot is ready and running.')

    if not send_scheduled_message.is_running():
        send_scheduled_message.start()
        print("Scheduler task started.")

@client.event
async def on_message(message):
    if message.author != client.user:
        BOT_STATE.last_channel_activity_time = time.time()
    await client.process_commands(message)


# --- Slash Commands (Essential for a working bot) ---

@tree.command(name="manual", description="Schedule a fixed message to be sent at intervals.")
@discord.app_commands.describe(message="The exact message to repeat.", interval_hours="The interval in hours (e.g., 2 or 0.5).")
async def manual_schedule(interaction: discord.Interaction, message: str, interval_hours: float):
    if interval_hours <= 0: await interaction.response.send_message("The interval must be > 0.", ephemeral=True); return
    BOT_STATE.interval_hours = interval_hours
    BOT_STATE.interval_seconds = interval_hours * 3600
    BOT_STATE.scheduled_message_content = message
    BOT_STATE.scheduled_channel_id = interaction.channel_id
    BOT_STATE.is_automatic = False
    BOT_STATE.last_bot_send_time = time.time()
    BOT_STATE.last_channel_activity_time = time.time()
    await interaction.response.send_message(f"✅ **Manual Scheduled!** Interval: **{interval_hours} hours**.", ephemeral=False)

@tree.command(name="automatic", description="Schedule an AI-generated message based on a prompt.")
@discord.app_commands.describe(prompt="The prompt for the AI.", interval_hours="The interval in hours (e.g., 2 or 0.5).")
async def automatic_schedule(interaction: discord.Interaction, prompt: str, interval_hours: float):
    if not GEMINI_API_KEY: await interaction.response.send_message("❌ **Error:** `GEMINI_API_KEY` is missing.", ephemeral=True); return
    if interval_hours <= 0: await interaction.response.send_message("The interval must be > 0.", ephemeral=True); return
    BOT_STATE.interval_hours = interval_hours
    BOT_STATE.interval_seconds = interval_hours * 3600
    BOT_STATE.ai_prompt = prompt
    BOT_STATE.scheduled_channel_id = interaction.channel_id
    BOT_STATE.is_automatic = True
    BOT_STATE.last_bot_send_time = time.time()
    BOT_STATE.last_channel_activity_time = time.time()
    await interaction.response.send_message(f"🤖 **Automatic Scheduled!** Interval: **{interval_hours} hours**.", ephemeral=False)

@tree.command(name="stop", description="Stop the currently running scheduled announcement.")
async def stop_schedule(interaction: discord.Interaction):
    if BOT_STATE.scheduled_channel_id is None: await interaction.response.send_message("No schedule running.", ephemeral=True); return
    BOT_STATE.scheduled_channel_id = None
    BOT_STATE.interval_seconds = 0
    await interaction.response.send_message("🛑 **Schedule has been stopped.**", ephemeral=False)

@tree.command(name="status", description="Check the status of the current scheduled announcement.")
async def get_status(interaction: discord.Interaction):
    if BOT_STATE.scheduled_channel_id is None: await interaction.response.send_message("Status: **Idle**.", ephemeral=True); return
    
    channel = client.get_channel(BOT_STATE.scheduled_channel_id)
    channel_name = channel.name if channel else "Unknown Channel"
    mode = "Automatic (AI)" if BOT_STATE.is_automatic else "Manual (Fixed)"
    time_since_send = time.time() - BOT_STATE.last_bot_send_time
    is_waiting = "Yes (Awaiting chat activity)" if BOT_STATE.last_channel_activity_time <= BOT_STATE.last_bot_send_time else "No"
    
    response_text = (
        f"**Status:** Running\n"
        f"**Mode:** {mode}\n"
        f"**Channel:** #{channel_name}\n"
        f"**Interval:** {BOT_STATE.interval_hours} hours\n"
        f"**Time Since Last Send:** {time_since_send:.1f} seconds\n"
        f"**Paused (Idle Channel):** {is_waiting}"
    )
    await interaction.response.send_message(response_text, ephemeral=True)


# --- Main Entry Point ---

if __name__ == '__main__':
    # 1. Start the keep-alive web server in the background
    start_server_thread()
    
    # 2. Start the Discord bot
    if DISCORD_BOT_TOKEN:
        try:
            client.run(DISCORD_BOT_TOKEN)
        except Exception as e:
            print(f"Failed to run the Discord client: {e}")
    else:
        print("ERROR: DISCORD_BOT_TOKEN not found in environment variables. Please set it in Railway Secrets.")
