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

async def generate_announcement_content(prompt):
    """
    Calls the Gemini API to generate the announcement message.
    """
    if not GEMINI_API_KEY: return "Error: Gemini API Key not configured."
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
                        await asyncio.sleep(2**attempt)
                    else:
                        error_text = await response.text()
                        print(f"API Error (Status {response.status}): {error_text}")
                        return f"Error: AI service returned status {response.status}"
        except ClientConnectorError:
            await asyncio.sleep(2**attempt)
        except Exception as e:
            print(f"An unexpected error occurred during API call: {e}")
            break
    return "Error: Failed to connect to AI service after multiple retries."

async def parse_automatic_prompt(full_prompt):
    """
    Uses Gemini's structured output to parse the message and interval from a single prompt.
    """
    if not GEMINI_API_KEY: return None, "Error: Gemini API Key not configured."
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-05-20:generateContent?key={GEMINI_API_KEY}"
    
    system_prompt = (
        "Analyze the user's full request. Extract the core announcement message/prompt and the time interval. "
        "Convert the interval into total seconds. If no interval is found, default to 3600 seconds (1 hour)."
    )

    schema = {
        "type": "OBJECT",
        "properties": {
            "announcement_prompt": {
                "type": "STRING",
                "description": "The concise message or prompt to be used for the periodic announcement."
            },
            "interval_seconds": {
                "type": "INTEGER",
                "description": "The time interval extracted from the prompt, converted into total seconds. Must be at least 10 seconds."
            }
        },
        "required": ["announcement_prompt", "interval_seconds"]
    }

    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema
        }
    }

    try:
        async with ClientSession() as session:
            async with session.post(url, headers={'Content-Type': 'application/json'}, json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    json_string = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
                    parsed_data = json.loads(json_string)
                    
                    # Ensure interval is a minimum of 10 seconds
                    if parsed_data.get('interval_seconds', 0) < 10:
                        parsed_data['interval_seconds'] = 10
                    
                    return parsed_data, None
                else:
                    error_text = await response.text()
                    return None, f"AI Parsing Error: Status {response.status}. {error_text}"
    except Exception as e:
        return None, f"An error occurred during AI parsing: {e}"


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
            message_to_send = await generate_announcement_content(BOT_STATE.ai_prompt)
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


# --- Slash Commands ---

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

@tree.command(name="automatic", description="Schedule an AI message based on a single prompt (e.g., 'Say 'bark' every 10 seconds').")
@discord.app_commands.describe(full_prompt="The message prompt AND interval (e.g., 'Say a fun fact every 2 hours').")
async def automatic_schedule(interaction: discord.Interaction, full_prompt: str):
    if not GEMINI_API_KEY: 
        await interaction.response.send_message("❌ **Error:** `GEMINI_API_KEY` is missing.", ephemeral=True); 
        return
    
    await interaction.response.defer(ephemeral=True)
    
    # Use Gemini to parse the prompt for message and interval
    parsed_data, error = await parse_automatic_prompt(full_prompt)

    if error:
        await interaction.followup.send(f"❌ **Error parsing prompt:** {error}", ephemeral=True)
        return

    interval_seconds = parsed_data.get('interval_seconds')
    ai_prompt = parsed_data.get('announcement_prompt')
    
    if not ai_prompt or interval_seconds < 10:
        await interaction.followup.send("❌ **Error:** Could not determine a clear message or the interval was too small (minimum 10 seconds). Try: `/automatic Say something fun every 30 minutes`", ephemeral=True)
        return

    # Set the state based on parsed results
    BOT_STATE.interval_seconds = interval_seconds
    BOT_STATE.interval_hours = interval_seconds / 3600 # Store this for status command clarity
    BOT_STATE.ai_prompt = ai_prompt
    BOT_STATE.scheduled_channel_id = interaction.channel_id
    BOT_STATE.is_automatic = True
    BOT_STATE.last_bot_send_time = time.time()
    BOT_STATE.last_channel_activity_time = time.time()
    
    # Convert seconds back to a readable format for the confirmation message
    if interval_seconds >= 3600 and interval_seconds % 3600 == 0:
        display_interval = f"{interval_seconds // 3600} hours"
    elif interval_seconds >= 60 and interval_seconds % 60 == 0:
        display_interval = f"{interval_seconds // 60} minutes"
    else:
        display_interval = f"{interval_seconds} seconds"

    
    confirmation_message = (
        f"🤖 **Automatic Scheduled!**\n"
        f"**Task:** Generate a message based on the prompt: '{ai_prompt}'\n"
        f"**Interval:** **{display_interval}**"
    )
    await interaction.followup.send(confirmation_message, ephemeral=False)

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
    
    # Calculate time until next send
    time_until_next = BOT_STATE.interval_seconds - time_since_send
    time_until_next = max(0, time_until_next)

    # Convert seconds back to a readable format for the confirmation message
    interval_seconds = BOT_STATE.interval_seconds
    if interval_seconds >= 3600 and interval_seconds % 3600 == 0:
        display_interval = f"{interval_seconds // 3600} hours"
    elif interval_seconds >= 60 and interval_seconds % 60 == 0:
        display_interval = f"{interval_seconds // 60} minutes"
    else:
        display_interval = f"{interval_seconds} seconds"

    
    response_text = (
        f"**Status:** Running\n"
        f"**Mode:** {mode}\n"
        f"**Channel:** #{channel_name}\n"
        f"**Interval:** {display_interval}\n"
        f"**Time until next send:** {time_until_next:.1f} seconds\n"
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
        print("ERROR: DISCORD_BOT_TOKEN not found in environment variables.")
