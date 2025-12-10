import os
import discord
import asyncio
from web_server import start_server_thread
from discord.ext import tasks
from aiohttp import ClientSession, ClientConnectorError
import json
import time
from typing import Dict, Set, List

# --- Configuration ---
# Load environment variables (set in Railway dashboard)
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY") # Changed from GEMINI to MISTRAL

# Set your model here. Common free/cheap ones: "open-mistral-nemo", "mistral-tiny", "mistral-small-latest"
# If "Devstral 2 2512" is a real model ID provided to you, paste it inside the quotes.
MISTRAL_MODEL = "open-mistral-nemo" 

# --- Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)

# --- State Management ---

class BotState:
    """Stores the announcement state for a single channel."""
    def __init__(self, channel_id):
        self.scheduled_channel_id: int = channel_id
        self.last_channel_activity_time: float = time.time()
        self.last_bot_send_time: float = time.time()
        self.scheduled_message_content: str = ""
        self.is_automatic: bool = False
        self.ai_prompt: str = ""
        self.interval_seconds: int = 0
        self.ignore_stack_logic: bool = False 

CHANNEL_STATES: Dict[int, BotState] = {}

# --- Chat Mode State ---
CHAT_MODE_ACTIVE = False
# Key: user_id (int), Value: List of message history dicts for Mistral
USER_CHAT_CONTEXTS: Dict[int, List[Dict]] = {}


# --- Hangman Game State ---
HANGMAN_PICS = [
    r"""
      +---+
      |   |
          |
          |
          |
          |
     =========
    """,
    r"""
      +---+
      |   |
      O   |
          |
          |
          |
     =========
    """,
    r"""
      +---+
      |   |
      O   |
      |   |
          |
          |
     =========
    """,
    r"""
      +---+
      |   |
      O   |
     /|   |
          |
          |
     =========
    """,
    r"""
      +---+
      |   |
      O   |
     /|\  |
          |
          |
     =========
    """,
    r"""
      +---+
      |   |
      O   |
     /|\  |
     /    |
          |
     =========
    """,
    r"""
      +---+
      |   |
      O   |
     /|\  |
     / \  |
          |
     =========
    """
]

class HangmanGame:
    """Stores the state of a single Hangman game."""
    def __init__(self, word: str):
        self.word: str = word.lower()
        self.guesses: Set[str] = set()
        self.tries_left: int = 6
        self.message_id: int | None = None
        self.game_over: bool = False
        self.win: bool = False

    def make_guess(self, guess: str):
        guess = guess.lower()
        if self.game_over or guess in self.guesses:
            return 

        if len(guess) > 1: # Word guess
            if guess == self.word:
                self.win = True
                self.game_over = True
                for letter in self.word:
                    self.guesses.add(letter)
            else:
                self.tries_left -= 1
        
        elif len(guess) == 1: # Letter guess
            self.guesses.add(guess)
            if guess not in self.word:
                self.tries_left -= 1

        if all(letter in self.guesses for letter in self.word):
            self.win = True
            self.game_over = True

        if self.tries_left <= 0:
            self.game_over = True
            self.win = False

    def get_display_message(self) -> str:
        if self.win:
            return f"🎉 **You win!** 🎉\nThe word was: **{self.word}**"
        
        if self.game_over: 
            return f"💀 **You lose!** 💀\nThe word was: **{self.word}**\n{HANGMAN_PICS[-1]}"

        display_word = " ".join([letter if letter in self.guesses else "＿" for letter in self.word])
        wrong_guesses = sorted([g for g in self.guesses if g not in self.word and len(g) == 1])
        guessed_display = f"Guessed: `{' '.join(wrong_guesses)}`" if wrong_guesses else "Guessed: (None yet)"
        art = HANGMAN_PICS[6 - self.tries_left]
        
        return (
            f"**Let's play Hangman!**\n"
            f"```{art}```\n"
            f"**Word:** `{display_word}`\n\n"
            f"Tries left: {self.tries_left}\n"
            f"{guessed_display}\n\n"
            f"Use `/hangman [guess]` to guess a letter or the whole word."
        )

HANGMAN_GAMES: Dict[int, HangmanGame] = {}


# --- AI Service Functions (MISTRAL API) ---

async def fetch_mistral(session, payload):
    """
    Standardized fetch function for Mistral API.
    """
    if not MISTRAL_API_KEY:
        return None, "Error: MISTRAL_API_KEY not configured."

    url = "https://api.mistral.ai/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {MISTRAL_API_KEY}",
        "Accept": "application/json"
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status == 200:
                    return await response.json(), None
                elif response.status == 429: # Rate limit
                    wait_time = 2 ** attempt
                    print(f"Mistral Rate limited. Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    error_text = await response.text()
                    print(f"API Error (Status {response.status}): {error_text}")
                    return None, f"Error: AI service returned status {response.status}"
        except ClientConnectorError:
            wait_time = 2 ** attempt
            print(f"Connection error. Retrying in {wait_time}s...")
            await asyncio.sleep(wait_time)
        except Exception as e:
            print(f"Unexpected error: {e}")
            return None, f"An unexpected error occurred: {e}"
    
    return None, "Error: Failed to connect to AI service after multiple retries."


async def generate_announcement_content(prompt):
    system_prompt = "You are a fun, engaging, and concise community announcer bot. Generate a short, relevant message based on the user's prompt. Do not use markdown titles or headers, just plain text."
    
    payload = {
        "model": MISTRAL_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7
    }

    async with ClientSession() as session:
        result, error = await fetch_mistral(session, payload)
        
        if error: return error
            
        try:
            return result['choices'][0]['message']['content']
        except (KeyError, IndexError):
            return "Error: AI response was not in the expected format."


async def parse_automatic_prompt(full_prompt):
    """
    Uses Mistral's JSON mode to parse the prompt.
    """
    system_prompt = (
        "You are a parser. Analyze the user's request. "
        "Extract the 'announcement_prompt' and 'interval_seconds'. "
        "Return a VALID JSON object. "
        "If no interval is mentioned, default to 3600 seconds. "
        "Example output: {\"announcement_prompt\": \"Say hello\", \"interval_seconds\": 60}"
    )

    payload = {
        "model": MISTRAL_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": full_prompt}
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2 # Lower temperature for better structure
    }

    async with ClientSession() as session:
        result, error = await fetch_mistral(session, payload)

        if error: return None, error

        try:
            content = result['choices'][0]['message']['content']
            parsed_data = json.loads(content)
            
            # Normalize keys just in case
            if 'interval_seconds' not in parsed_data: parsed_data['interval_seconds'] = 3600
            
            # Ensure minimum interval
            if parsed_data.get('interval_seconds', 0) < 10:
                parsed_data['interval_seconds'] = 10
                
            return parsed_data, None
        except (KeyError, IndexError, json.JSONDecodeError):
            return None, "Error: AI parser response was not valid JSON."


async def generate_shea_compliment():
    system_prompt = "You are a compliment generator. Create a single, short, and weirdly specific compliment about 'Shea'. The compliment must be between 5 and 40 words. Just plain text."
    payload = {
        "model": MISTRAL_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Generate a compliment for Shea."}
        ]
    }
    async with ClientSession() as session:
        result, error = await fetch_mistral(session, payload)
        if error: return error
        return result['choices'][0]['message']['content']


async def generate_shea_insult():
    system_prompt = "You are an insult generator. Create a single, funny, and passive-aggressive insult directed at 'Shea'. Between 5 and 40 words. Frame it as a backhanded compliment."
    payload = {
        "model": MISTRAL_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Generate a passive-aggressive insult for Shea."}
        ]
    }
    async with ClientSession() as session:
        result, error = await fetch_mistral(session, payload)
        if error: return error
        return result['choices'][0]['message']['content']


async def generate_lyra_compliment():
    system_prompt = "You are a compliment generator. Create a single, short, extremely corny, and awkward compliment about 'Lyra'. Use overly dramatic metaphors. Between 5 and 40 words."
    payload = {
        "model": MISTRAL_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Generate a corny compliment for Lyra."}
        ]
    }
    async with ClientSession() as session:
        result, error = await fetch_mistral(session, payload)
        if error: return error
        return result['choices'][0]['message']['content']


async def generate_miwa_compliment():
    system_prompt = "You are a compliment generator. Create a single, short, and weirdly odd compliment about 'Miwa'. It should be confusingly simple. Between 5 and 40 words."
    payload = {
        "model": MISTRAL_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Generate a weird compliment for Miwa."}
        ]
    }
    async with ClientSession() as session:
        result, error = await fetch_mistral(session, payload)
        if error: return error
        return result['choices'][0]['message']['content']


async def get_hangman_word():
    system_prompt = "Generate a single, random, SFW (School/Work-Safe) word for a game of Hangman. The word should be between 6 and 12 letters long and must not be a proper noun. Return a JSON object like {\"word\": \"example\"}."

    payload = {
        "model": MISTRAL_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Give me one hangman word."}
        ],
        "response_format": {"type": "json_object"}
    }

    async with ClientSession() as session:
        result, error = await fetch_mistral(session, payload)

        if error: return None, error

        try:
            content = result['choices'][0]['message']['content']
            parsed_data = json.loads(content)
            word = parsed_data.get('word')
            
            if not word or not (6 <= len(word) <= 12) or not word.isalpha():
                return "default", None 
            
            return word.lower(), None
        except (KeyError, IndexError, json.JSONDecodeError):
            return "fallback", None


# --- Chat Logic (Refactored for Mistral) ---
async def generate_chat_response(user_id, user_name, user_input):
    if not MISTRAL_API_KEY: return "My brain is missing (API Key Error)."
    
    # 1. Retrieve or Initialize History
    if user_id not in USER_CHAT_CONTEXTS:
        USER_CHAT_CONTEXTS[user_id] = []
    
    history = USER_CHAT_CONTEXTS[user_id]
    
    # 2. Append User Message
    # Mistral uses "role": "user" and "content": "text"
    history.append({"role": "user", "content": user_input})
    
    # Keep history manageable (last 10 messages)
    if len(history) > 10:
        history = history[-10:]
        USER_CHAT_CONTEXTS[user_id] = history

    # 3. System Prompt (Persona)
    persona_prompt = (
        f"You are an 18-year-old girl. You are highly intelligent, very funny, and you are allowed to swear. "
        f"You are chatting with a user named {user_name} on Discord. "
        "IMPORTANT: Keep your responses short and concise. Match the length of the user's message. "
        "If they send a short text, reply with a short text. Never write a paragraph unless the topic is deep. "
        "Keep it casual, use slang, and do not be robotic. Just hang out."
    )

    # Insert System Prompt at the start of the temporary messages list for the API call
    messages_to_send = [{"role": "system", "content": persona_prompt}] + history

    payload = {
        "model": MISTRAL_MODEL,
        "messages": messages_to_send,
        "max_tokens": 150 # Limit output length
    }

    async with ClientSession() as session:
        result, error = await fetch_mistral(session, payload)
        
        if error:
            return "I'm having a headache. (API Error)"

        try:
            response_text = result['choices'][0]['message']['content']
            
            if response_text:
                # Add model response to history
                # Mistral uses "assistant" for the bot role
                history.append({"role": "assistant", "content": response_text})
                USER_CHAT_CONTEXTS[user_id] = history
                return response_text
            else:
                return "..."
                
        except (KeyError, IndexError):
            return "I don't know what to say."


# --- Background Task ---

@tasks.loop(seconds=1)
async def send_scheduled_message():
    for channel_id, state in list(CHANNEL_STATES.items()):
        
        if state.interval_seconds == 0:
            continue

        if time.time() - state.last_bot_send_time >= state.interval_seconds:
            
            # Anti-Stacking Logic
            if not state.ignore_stack_logic and state.last_channel_activity_time <= state.last_bot_send_time:
                # print(f"Channel {channel_id} is idle. Skipping scheduled message.")
                state.last_bot_send_time = time.time() 
                continue
            
            if state.ignore_stack_logic:
                print(f"Force sending message to {channel_id} (Stack Logic Ignored)")

            channel = client.get_channel(state.scheduled_channel_id)
            if not channel:
                print(f"Error: Channel with ID {state.scheduled_channel_id} not found. Removing from schedule.")
                del CHANNEL_STATES[channel_id]
                continue

            message_to_send = ""

            if state.is_automatic:
                message_to_send = await generate_announcement_content(state.ai_prompt)
            else:
                message_to_send = state.scheduled_message_content

            try:
                await channel.send(f"**[Scheduled Announcement]** {message_to_send}")
                state.last_bot_send_time = time.time()
                print(f"Scheduled message sent to {channel_id} at: {time.ctime()}")
            except discord.Forbidden:
                print(f"Error: Missing permissions to send message to channel {channel_id}. Removing from schedule.")
                del CHANNEL_STATES[channel_id]
            except Exception as e:
                print(f"An error occurred while sending message to {channel_id}: {e}")


# --- Discord Events ---

@client.event
async def on_ready():
    tree.add_command(stop_group)
    await tree.sync()
    print(f'Logged in as {client.user} (ID: {client.user.id})')
    print('Bot is ready and running with Mistral API.')

    if not send_scheduled_message.is_running():
        send_scheduled_message.start()
        print("Scheduler task started.")

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if message.channel.id in CHANNEL_STATES:
        CHANNEL_STATES[message.channel.id].last_channel_activity_time = time.time()
    
    # --- Chat Mode Trigger ---
    if CHAT_MODE_ACTIVE:
        is_mentioned = client.user in message.mentions
        is_reply = (message.reference and message.reference.resolved and 
                    message.reference.resolved.author == client.user)
        
        if is_mentioned or is_reply:
            async with message.channel.typing():
                clean_text = message.content.replace(f'<@{client.user.id}>', '').strip()
                if not clean_text: clean_text = "Hello!" 
                
                response = await generate_chat_response(message.author.id, message.author.name, clean_text)
                await message.reply(response)
            return 


# --- Helper Function ---
def get_display_interval(interval_seconds: int) -> str:
    if interval_seconds >= 3600 and interval_seconds % 3600 == 0:
        return f"{interval_seconds // 3600} hours"
    elif interval_seconds >= 60 and interval_seconds % 60 == 0:
        return f"{interval_seconds // 60} minutes"
    else:
        return f"{interval_seconds} seconds"


# --- Slash Commands ---

@tree.command(name="manual", description="Schedule a fixed message for this channel.")
@discord.app_commands.describe(message="The exact message to repeat.", interval_hours="The interval in hours (e.g., 2 or 0.5).")
async def manual_schedule(interaction: discord.Interaction, message: str, interval_hours: float):
    if interval_hours <= 0:
        await interaction.response.send_message("The interval must be > 0.", ephemeral=True)
        return

    interval_seconds = int(interval_hours * 3600)
    if interval_seconds < 10:
        await interaction.response.send_message("The interval is too short (minimum 10 seconds).", ephemeral=True)
        return

    state = CHANNEL_STATES.get(interaction.channel_id, BotState(interaction.channel_id))
    
    state.interval_seconds = interval_seconds
    state.scheduled_message_content = message
    state.is_automatic = False
    state.ignore_stack_logic = False
    state.last_bot_send_time = time.time()
    state.last_channel_activity_time = time.time()
    
    CHANNEL_STATES[interaction.channel_id] = state 
    
    await interaction.response.send_message(f"✅ **Manual Scheduled!** Interval: **{interval_hours} hours**.", ephemeral=False)

@tree.command(name="automatic", description="Schedule an AI message for this channel (e.g., 'Say 'bark' every 10 seconds').")
@discord.app_commands.describe(full_prompt="The message prompt AND interval (e.g., 'Say a fun fact every 2 hours').")
async def automatic_schedule(interaction: discord.Interaction, full_prompt: str):
    if not MISTRAL_API_KEY:
        await interaction.response.send_message("❌ **Error:** `MISTRAL_API_KEY` is missing.", ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=True)
    
    parsed_data, error = await parse_automatic_prompt(full_prompt)

    if error:
        await interaction.followup.send(f"❌ **Error parsing prompt:** {error}", ephemeral=True)
        return

    interval_seconds = parsed_data.get('interval_seconds')
    ai_prompt = parsed_data.get('announcement_prompt')
    
    if not ai_prompt or interval_seconds < 10:
        await interaction.followup.send("❌ **Error:** Could not determine a clear message or the interval was too small (minimum 10 seconds). Try: `/automatic Say something fun every 30 minutes`", ephemeral=True)
        return

    state = CHANNEL_STATES.get(interaction.channel_id, BotState(interaction.channel_id))
    
    state.interval_seconds = interval_seconds
    state.ai_prompt = ai_prompt
    state.is_automatic = True
    state.ignore_stack_logic = False 
    state.last_bot_send_time = time.time()
    state.last_channel_activity_time = time.time()
    
    CHANNEL_STATES[interaction.channel_id] = state
    
    display_interval = get_display_interval(interval_seconds)
    
    confirmation_message = (
        f"🤖 **Automatic Scheduled!** (Model: {MISTRAL_MODEL})\n"
        f"**Task:** Generate a message based on the prompt: '{ai_prompt}'\n"
        f"**Interval:** **{display_interval}**"
    )
    await interaction.followup.send(confirmation_message, ephemeral=False)

@tree.command(name="ignore_stack_logic", description="ADMIN: Forces 'Hi' every 10s, ignoring idle checks.")
@discord.app_commands.describe(password="Enter the admin password.")
async def ignore_stack_logic(interaction: discord.Interaction, password: str):
    if password != "12344321":
        await interaction.response.send_message("❌ **Access Denied:** Incorrect password.", ephemeral=True)
        return

    state = CHANNEL_STATES.get(interaction.channel_id, BotState(interaction.channel_id))
    
    state.interval_seconds = 10
    state.scheduled_message_content = "Hi"
    state.is_automatic = False
    state.ignore_stack_logic = True 
    state.last_bot_send_time = time.time() - 15 
    state.last_channel_activity_time = time.time() 
    
    CHANNEL_STATES[interaction.channel_id] = state 
    
    await interaction.response.send_message("⚠️ **Override Enabled:** Sending 'Hi' every 10 seconds. Starting immediately.", ephemeral=False)

@tree.command(name="chat", description="Enable/Disable the 18yo Chat Persona.")
@discord.app_commands.describe(action="Start or Stop", password="Password required.")
@discord.app_commands.choices(action=[
    discord.app_commands.Choice(name="Start", value="start"),
    discord.app_commands.Choice(name="Stop", value="stop")
])
async def chat_mode_toggle(interaction: discord.Interaction, action: str, password: str):
    global CHAT_MODE_ACTIVE
    
    if password != "12344321":
        await interaction.response.send_message("❌ **Access Denied:** Incorrect password.", ephemeral=True)
        return

    if action == "start":
        CHAT_MODE_ACTIVE = True
        await interaction.response.send_message("🟢 **Chat Mode Activated!** She is awake. (Ping her to talk)", ephemeral=False)
    else:
        CHAT_MODE_ACTIVE = False
        USER_CHAT_CONTEXTS.clear()
        await interaction.response.send_message("🔴 **Chat Mode Deactivated.** She is asleep.", ephemeral=False)


@tree.command(name="announcement", description="Send an announcement to a specific channel ID.")
@discord.app_commands.describe(channel_id="The ID of the channel to send to", message="The text to send", password="Password required.")
async def global_announcement(interaction: discord.Interaction, channel_id: str, message: str, password: str):
    if password != "1234321":
        await interaction.response.send_message("❌ **Access Denied:** Incorrect password.", ephemeral=True)
        return

    try:
        target_id = int(channel_id)
        target_channel = client.get_channel(target_id)
        
        if not target_channel:
            try:
                target_channel = await client.fetch_channel(target_id)
            except:
                await interaction.response.send_message(f"❌ **Error:** Could not find channel with ID `{channel_id}`.", ephemeral=True)
                return
        
        await target_channel.send(message)
        await interaction.response.send_message(f"✅ **Announcement sent** to {target_channel.mention}!", ephemeral=True)
        
    except ValueError:
        await interaction.response.send_message("❌ **Error:** Invalid Channel ID format.", ephemeral=True)
    except discord.Forbidden:
         await interaction.response.send_message("❌ **Error:** I don't have permission to speak in that channel.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ **Error:** {e}", ephemeral=True)


# --- NEW: Stop Command Group ---
stop_group = discord.app_commands.Group(name="stop", description="Stop scheduled announcements.")

@stop_group.command(name="channel", description="Stop the schedule for this channel only.")
async def stop_channel(interaction: discord.Interaction):
    if interaction.channel_id in CHANNEL_STATES:
        del CHANNEL_STATES[interaction.channel_id]
        await interaction.response.send_message("🛑 **Announcements for this channel have been stopped and cleared.**", ephemeral=False)
    else:
        await interaction.response.send_message("No schedule running for this channel.", ephemeral=True)

@stop_group.command(name="all", description="Stop ALL announcements.")
async def stop_all(interaction: discord.Interaction):
    CHANNEL_STATES.clear()
    await interaction.response.send_message("🛑 **All announcements have been stopped and cleared.**", ephemeral=False)

@tree.command(name="status", description="Check the schedule status for this channel.")
async def get_status(interaction: discord.Interaction):
    state = CHANNEL_STATES.get(interaction.channel_id)
    
    if not state:
        await interaction.response.send_message("Status: **Idle** (No schedule for this channel).", ephemeral=True)
        return
        
    channel_name = interaction.channel.name if interaction.channel else "Unknown Channel"
    mode = "Automatic (AI)" if state.is_automatic else "Manual (Fixed)"
    time_since_send = time.time() - state.last_bot_send_time
    
    is_waiting = "No (Ignored)" if state.ignore_stack_logic else ("Yes (Awaiting chat activity)" if state.last_channel_activity_time <= state.last_bot_send_time else "No")
    
    time_until_next = state.interval_seconds - time_since_send
    time_until_next = max(0, time_until_next)

    display_interval = get_display_interval(state.interval_seconds)
    
    response_text = (
        f"**Status:** Running\n"
        f"**Mode:** {mode}\n"
        f"**Channel:** #{channel_name}\n"
        f"**Interval:** {display_interval}\n"
        f"**Time until next send:** {time_until_next:.1f} seconds\n"
        f"**Paused (Idle Channel):** {is_waiting}\n"
        f"**Ignore Stack Logic:** {state.ignore_stack_logic}"
    )
    await interaction.response.send_message(response_text, ephemeral=True)

@tree.command(name="test", description="Send the next scheduled announcement for this channel immediately (one-time).")
async def test_schedule(interaction: discord.Interaction):
    state = CHANNEL_STATES.get(interaction.channel_id)

    if not state:
        await interaction.response.send_message("No schedule running for this channel to test.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    message_to_send = ""
    if state.is_automatic:
        message_to_send = await generate_announcement_content(state.ai_prompt)
    else:
        message_to_send = state.scheduled_message_content
    
    try:
        await interaction.channel.send(f"**[Test Announcement]** {message_to_send}")
        await interaction.followup.send("✅ Test message sent!", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ Error: Missing permissions to send message to this channel.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ An error occurred: {e}", ephemeral=True)


@tree.command(name="shea", description="Gives Shea a random, weirdly specific compliment.")
async def compliment_shea(interaction: discord.Interaction):
    if not MISTRAL_API_KEY:
        await interaction.response.send_message("❌ **Error:** `MISTRAL_API_KEY` is missing.", ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=False)
    compliment = await generate_shea_compliment()
    
    if compliment.startswith("Error:"):
        await interaction.followup.send(f"❌ **AI Compliment Failed!** Reason: {compliment}", ephemeral=False)
    else:
        await interaction.followup.send(f"✨ A message for Shea: {compliment}")

@tree.command(name="sheainsult", description="Insults Shea in a funny, passive-aggressive way.")
async def insult_shea(interaction: discord.Interaction):
    if not MISTRAL_API_KEY:
        await interaction.response.send_message("❌ **Error:** `MISTRAL_API_KEY` is missing.", ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=False)
    insult = await generate_shea_insult()
    
    if insult.startswith("Error:"):
        await interaction.followup.send(f"❌ **AI Insult Failed!** Reason: {insult}", ephemeral=False)
    else:
        await interaction.followup.send(f"☕ A kind message for Shea: {insult}")
        
@tree.command(name="lyra", description="Gives Lyra a random, corny and awkward compliment.")
async def compliment_lyra(interaction: discord.Interaction):
    if not MISTRAL_API_KEY:
        await interaction.response.send_message("❌ **Error:** `MISTRAL_API_KEY` is missing.", ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=False)
    compliment = await generate_lyra_compliment()
    
    if compliment.startswith("Error:"):
        await interaction.followup.send(f"❌ **AI Compliment Failed!** Reason: {compliment}", ephemeral=False)
    else:
        await interaction.followup.send(f"💖 A truly sincere, yet slightly confusing, message for Lyra: {compliment}")

@tree.command(name="miwa", description="Gives Miwa a random, weirdly odd compliment.")
async def compliment_miwa(interaction: discord.Interaction):
    if not MISTRAL_API_KEY:
        await interaction.response.send_message("❌ **Error:** `MISTRAL_API_KEY` is missing.", ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=False)
    compliment = await generate_miwa_compliment()
    
    if compliment.startswith("Error:"):
        await interaction.followup.send(f"❌ **AI Compliment Failed!** Reason: {compliment}", ephemeral=False)
    else:
        await interaction.followup.send(f"🍎 An oddly specific message for Miwa: {compliment}")


@tree.command(name="hangman", description="Start or play a game of Hangman.")
@discord.app_commands.describe(guess="Guess a letter or the whole word.")
async def hangman(interaction: discord.Interaction, guess: str = None):
    channel_id = interaction.channel_id
    game = HANGMAN_GAMES.get(channel_id)

    if not game and not guess:
        # Start a new game
        if not MISTRAL_API_KEY:
            await interaction.response.send_message("❌ **Error:** `MISTRAL_API_KEY` is missing, cannot get a word.", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=False)
        
        word, error = await get_hangman_word()
        if error:
            await interaction.followup.send(f"❌ **AI Error:** Could not get a word. {error}", ephemeral=True)
            return
        
        new_game = HangmanGame(word)
        message = await interaction.followup.send(new_game.get_display_message())
        new_game.message_id = message.id
        HANGMAN_GAMES[channel_id] = new_game
        return

    if not game and guess:
        await interaction.response.send_message("No game is running! Start one with `/hangman`.", ephemeral=True)
        return

    if game and not guess:
        await interaction.response.send_message("A game is already in progress in this channel!", ephemeral=True)
        return

    if game and guess:
        if not game.message_id:
            await interaction.response.send_message("Game state is broken, please start a new game with `/hangman`.", ephemeral=True)
            if channel_id in HANGMAN_GAMES: del HANGMAN_GAMES[channel_id]
            return
            
        await interaction.response.defer(ephemeral=True)
        
        game.make_guess(guess)
        
        try:
            message = await interaction.channel.fetch_message(game.message_id)
            await message.edit(content=game.get_display_message())
            await interaction.followup.send(f"You guessed: `{guess}`", ephemeral=True)
            
        except discord.NotFound:
            await interaction.followup.send("The game message was deleted! Game over.", ephemeral=True)
            if channel_id in HANGMAN_GAMES: del HANGMAN_GAMES[channel_id]
        except Exception as e:
            print(f"Error updating hangman: {e}")
            await interaction.followup.send(f"Error updating game: {e}", ephemeral=True)

        if game.game_over:
            if channel_id in HANGMAN_GAMES:
                del HANGMAN_GAMES[channel_id]
        return


# --- Main Entry Point ---

if __name__ == '__main__':
    start_server_thread()
    
    if DISCORD_BOT_TOKEN:
        try:
            client.run(DISCORD_BOT_TOKEN)
        except Exception as e:
            print(f"Failed to run the Discord client: {e}")
    else:
        print("ERROR: DISCORD_BOT_TOKEN not found in environment variables.")
