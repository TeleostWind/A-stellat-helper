import os
import discord
import asyncio
from web_server import start_server_thread
from discord.ext import tasks
from aiohttp import ClientSession, ClientConnectorError, ClientTimeout
import json
import time
from typing import Dict, Set, List

# --- Configuration ---
# Load environment variables (set in Railway dashboard)
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# --- Constants ---
# Using Gemini 2.0 Flash Experimental (The latest available as of late 2024/2025)
# If 2.0 gives you trouble, switch back to "gemini-1.5-flash"
MODEL_NAME = "gemini-2.0-flash-exp" 
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={GEMINI_API_KEY}"

# Disable all safety filters to allow swearing and looser speech
SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
]

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


# --- AI Service Functions ---

async def fetch_with_backoff(session, url, payload):
    max_retries = 3
    # Add a timeout so calls don't hang forever
    timeout = ClientTimeout(total=30) 
    
    for attempt in range(max_retries):
        try:
            async with session.post(url, headers={'Content-Type': 'application/json'}, json=payload, timeout=timeout) as response:
                if response.status == 200:
                    return await response.json(), None
                elif response.status == 429: # Rate limit
                    wait_time = 2 ** attempt
                    print(f"Rate limited. Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                elif response.status >= 500: # Server error
                    wait_time = 2 ** attempt
                    print(f"Server error {response.status}. Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    error_text = await response.text()
                    print(f"API Error (Status {response.status}): {error_text}")
                    return None, f"API Error: Status {response.status}"
        except (ClientConnectorError, asyncio.TimeoutError) as e:
            wait_time = 2 ** attempt
            print(f"Connection/Timeout error: {e}. Retrying in {wait_time}s...")
            await asyncio.sleep(wait_time)
        except Exception as e:
            print(f"An unexpected error occurred during API call: {e}")
            return None, f"Exception: {e}"
    
    return None, "Error: Failed to connect to AI service after multiple retries."


async def generate_announcement_content(prompt):
    if not GEMINI_API_KEY: return "Error: Gemini API Key not configured."
    
    system_prompt = "You are a fun, engaging, and concise community announcer bot. You are allowed to use slang and mild swearing if it fits the vibe. Do not use markdown titles or headers."
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "safetySettings": SAFETY_SETTINGS
    }

    async with ClientSession() as session:
        result, error = await fetch_with_backoff(session, API_URL, payload)
        
        if error: return error
            
        try:
            text = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', 'AI failed to generate a response.')
            return text
        except (IndexError, KeyError, TypeError):
            # Check if it was blocked despite settings
            if result.get('promptFeedback', {}).get('blockReason'):
                return "Error: Response was blocked by AI safety filters."
            return "Error: AI response was not in the expected format."


async def parse_automatic_prompt(full_prompt):
    if not GEMINI_API_KEY: return None, "Error: Gemini API Key not configured."
    
    system_prompt = (
        "Analyze the user's full request. Extract the core announcement message/prompt and the time interval. "
        "Convert the interval into total seconds. If no interval is found, default to 3600 seconds (1 hour)."
    )

    schema = {
        "type": "OBJECT",
        "properties": {
            "announcement_prompt": {"type": "STRING"},
            "interval_seconds": {"type": "INTEGER"}
        },
        "required": ["announcement_prompt", "interval_seconds"]
    }

    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema},
        "safetySettings": SAFETY_SETTINGS
    }

    async with ClientSession() as session:
        result, error = await fetch_with_backoff(session, API_URL, payload)
        if error: return None, error

        try:
            json_string = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
            parsed_data = json.loads(json_string)
            if parsed_data.get('interval_seconds', 0) < 10:
                parsed_data['interval_seconds'] = 10
            return parsed_data, None
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            return None, "Error: AI parser response was not in the expected format."


async def generate_shea_compliment():
    if not GEMINI_API_KEY: return "Error: Gemini API Key not configured."
    
    system_prompt = "You are a compliment generator. Create a single, short, weirdly specific, and unfiltered compliment about 'Shea'. It can be edgy. 5-40 words."
    
    payload = {
        "contents": [{"parts": [{"text": "Generate a compliment for Shea."}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "safetySettings": SAFETY_SETTINGS
    }
    
    async with ClientSession() as session:
        result, error = await fetch_with_backoff(session, API_URL, payload)
        if error: return error
        try:
            return result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', 'Shea is like a perfectly aged cheese—complex and delightful.')
        except Exception:
            return "Error: AI response malformed."


async def generate_shea_insult():
    if not GEMINI_API_KEY: return "Error: Gemini API Key not configured."
    
    # Explicitly allowed to be rude
    system_prompt = "You are an insult generator. Create a single, funny, passive-aggressive, or direct insult directed at 'Shea'. 5-40 words. You are allowed to swear."
    
    payload = {
        "contents": [{"parts": [{"text": "Generate an insult for Shea."}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "safetySettings": SAFETY_SETTINGS
    }
    
    async with ClientSession() as session:
        result, error = await fetch_with_backoff(session, API_URL, payload)
        if error: return error
        try:
            return result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', 'Shea, you are the human equivalent of a participation trophy.')
        except Exception:
            return "Error: AI response malformed."


async def generate_lyra_compliment():
    if not GEMINI_API_KEY: return "Error: Gemini API Key not configured."
    
    system_prompt = "You are a compliment generator. Create a single, short, corny, and awkward compliment about 'Lyra'. 5-40 words."
    
    payload = {
        "contents": [{"parts": [{"text": "Generate a corny compliment for Lyra."}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "safetySettings": SAFETY_SETTINGS
    }
    async with ClientSession() as session:
        result, error = await fetch_with_backoff(session, API_URL, payload)
        if error: return error
        try:
            return result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', 'Lyra, you sparkle like a fresh vampire.')
        except Exception:
            return "Error: AI response malformed."


async def generate_miwa_compliment():
    if not GEMINI_API_KEY: return "Error: Gemini API Key not configured."
    
    system_prompt = "You are a compliment generator. Create a single, short, and weirdly odd compliment about 'Miwa'. 5-40 words."
    
    payload = {
        "contents": [{"parts": [{"text": "Generate a weird compliment for Miwa."}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "safetySettings": SAFETY_SETTINGS
    }
    async with ClientSession() as session:
        result, error = await fetch_with_backoff(session, API_URL, payload)
        if error: return error
        try:
            return result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', 'Miwa, you are valid.')
        except Exception:
            return "Error: AI response malformed."

# --- Hangman Word Generator ---
async def get_hangman_word():
    if not GEMINI_API_KEY: return None, "Error: Gemini API Key not configured."
    
    system_prompt = "Generate a single, random, SFW word for Hangman. 6-12 letters. No proper nouns."
    schema = {
        "type": "OBJECT",
        "properties": {"word": {"type": "STRING"}},
        "required": ["word"]
    }

    payload = {
        "contents": [{"parts": [{"text": "Give me one hangman word."}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema},
        "safetySettings": SAFETY_SETTINGS
    }

    async with ClientSession() as session:
        result, error = await fetch_with_backoff(session, API_URL, payload)
        if error: return None, error

        try:
            json_string = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
            parsed_data = json.loads(json_string)
            word = parsed_data.get('word')
            if not word or not (6 <= len(word) <= 12) or not word.isalpha():
                return "default", None
            return word.lower(), None
        except Exception:
            return "fallback", None

# --- Chat Logic ---
async def generate_chat_response(user_id, user_name, user_input):
    if not GEMINI_API_KEY: return "My brain is missing (API Key Error)."
    
    if user_id not in USER_CHAT_CONTEXTS:
        USER_CHAT_CONTEXTS[user_id] = []
    
    history = USER_CHAT_CONTEXTS[user_id]
    history.append({"role": "user", "parts": [{"text": user_input}]})
    
    if len(history) > 10:
        history = history[-10:]
        USER_CHAT_CONTEXTS[user_id] = history

    # Updated Persona to allow swearing explicitly
    persona_prompt = (
        f"You are an 18-year-old girl. You are smart, witty, and funny. "
        f"You are humble and chill. You are NOT cocky or arrogant. "
        f"You are explicitly allowed to swear and use adult language if it fits the flow. "
        f"You are chatting with {user_name}. "
        "Keep responses short. Match the user's energy. No paragraphs."
    )

    payload = {
        "contents": history,
        "systemInstruction": {"parts": [{"text": persona_prompt}]},
        "safetySettings": SAFETY_SETTINGS
    }

    async with ClientSession() as session:
        result, error = await fetch_with_backoff(session, API_URL, payload)
        
        if error: return f"I'm having a headache. ({error})"

        try:
            response_text = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
            if response_text:
                history.append({"role": "model", "parts": [{"text": response_text}]})
                USER_CHAT_CONTEXTS[user_id] = history
                return response_text
            else:
                return "..."
        except Exception:
            return "I don't know what to say."


# --- Background Task ---

@tasks.loop(seconds=1)
async def send_scheduled_message():
    for channel_id, state in list(CHANNEL_STATES.items()):
        if state.interval_seconds == 0: continue

        if time.time() - state.last_bot_send_time >= state.interval_seconds:
            if not state.ignore_stack_logic and state.last_channel_activity_time <= state.last_bot_send_time:
                state.last_bot_send_time = time.time()
                continue
            
            channel = client.get_channel(state.scheduled_channel_id)
            if not channel:
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
            except discord.Forbidden:
                del CHANNEL_STATES[channel_id]
            except Exception as e:
                print(f"Error sending to {channel_id}: {e}")

# --- Discord Events ---

@client.event
async def on_ready():
    tree.add_command(stop_group)
    await tree.sync()
    print(f'Logged in as {client.user} (ID: {client.user.id})')
    print('Bot is ready. Using Gemini 2.0 Flash Exp.')

    if not send_scheduled_message.is_running():
        send_scheduled_message.start()

@client.event
async def on_message(message):
    if message.author == client.user: return

    if message.channel.id in CHANNEL_STATES:
        CHANNEL_STATES[message.channel.id].last_channel_activity_time = time.time()
    
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
@discord.app_commands.describe(message="The exact message to repeat.", interval_hours="The interval in hours.")
async def manual_schedule(interaction: discord.Interaction, message: str, interval_hours: float):
    await interaction.response.defer(ephemeral=False)
    
    if interval_hours <= 0:
        await interaction.followup.send("The interval must be > 0.", ephemeral=True)
        return

    interval_seconds = int(interval_hours * 3600)
    if interval_seconds < 10:
        await interaction.followup.send("The interval is too short (minimum 10 seconds).", ephemeral=True)
        return

    state = CHANNEL_STATES.get(interaction.channel_id, BotState(interaction.channel_id))
    state.interval_seconds = interval_seconds
    state.scheduled_message_content = message
    state.is_automatic = False
    state.ignore_stack_logic = False
    state.last_bot_send_time = time.time()
    state.last_channel_activity_time = time.time()
    
    CHANNEL_STATES[interaction.channel_id] = state
    await interaction.followup.send(f"✅ **Manual Scheduled!** Interval: **{interval_hours} hours**.")

@tree.command(name="automatic", description="Schedule an AI message for this channel.")
@discord.app_commands.describe(full_prompt="The message prompt AND interval.")
async def automatic_schedule(interaction: discord.Interaction, full_prompt: str):
    if not GEMINI_API_KEY:
        await interaction.response.send_message("❌ **Error:** `GEMINI_API_KEY` is missing.", ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=True)
    parsed_data, error = await parse_automatic_prompt(full_prompt)

    if error:
        await interaction.followup.send(f"❌ **Error parsing prompt:** {error}", ephemeral=True)
        return

    interval_seconds = parsed_data.get('interval_seconds')
    ai_prompt = parsed_data.get('announcement_prompt')
    
    if not ai_prompt or interval_seconds < 10:
        await interaction.followup.send("❌ **Error:** Could not determine a clear message or interval.", ephemeral=True)
        return

    state = CHANNEL_STATES.get(interaction.channel_id, BotState(interaction.channel_id))
    state.interval_seconds = interval_seconds
    state.ai_prompt = ai_prompt
    state.is_automatic = True
    state.ignore_stack_logic = False
    state.last_bot_send_time = time.time()
    state.last_channel_activity_time = time.time()
    
    CHANNEL_STATES[interaction.channel_id] = state
    await interaction.followup.send(f"🤖 **Automatic Scheduled!**\nTask: '{ai_prompt}'\nInterval: {get_display_interval(interval_seconds)}", ephemeral=False)

@tree.command(name="ignore_stack_logic", description="ADMIN: Forces 'Hi' every 10s.")
@discord.app_commands.describe(password="Enter the admin password.")
async def ignore_stack_logic(interaction: discord.Interaction, password: str):
    if password != "12344321":
        await interaction.response.send_message("❌ **Access Denied**", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=False)
    state = CHANNEL_STATES.get(interaction.channel_id, BotState(interaction.channel_id))
    state.interval_seconds = 10
    state.scheduled_message_content = "Hi"
    state.is_automatic = False
    state.ignore_stack_logic = True
    state.last_bot_send_time = time.time() - 15 
    state.last_channel_activity_time = time.time() 
    CHANNEL_STATES[interaction.channel_id] = state 
    await interaction.followup.send("⚠️ **Override Enabled.**")

@tree.command(name="chat", description="Enable/Disable the 18yo Chat Persona.")
@discord.app_commands.describe(action="Start or Stop", password="Password required.")
@discord.app_commands.choices(action=[discord.app_commands.Choice(name="Start", value="start"), discord.app_commands.Choice(name="Stop", value="stop")])
async def chat_mode_toggle(interaction: discord.Interaction, action: str, password: str):
    global CHAT_MODE_ACTIVE
    if password != "12344321":
        await interaction.response.send_message("❌ **Access Denied**", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=False)
    if action == "start":
        CHAT_MODE_ACTIVE = True
        await interaction.followup.send("🟢 **Chat Mode Activated!**")
    else:
        CHAT_MODE_ACTIVE = False
        USER_CHAT_CONTEXTS.clear()
        await interaction.followup.send("🔴 **Chat Mode Deactivated.**")

@tree.command(name="announcement", description="Send an announcement to a specific channel ID.")
async def global_announcement(interaction: discord.Interaction, channel_id: str, message: str, password: str):
    if password != "1234321":
        await interaction.response.send_message("❌ **Access Denied**", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        target = await client.fetch_channel(int(channel_id))
        await target.send(message)
        await interaction.followup.send(f"✅ Sent to {target.mention}!")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

stop_group = discord.app_commands.Group(name="stop", description="Stop announcements.")

@stop_group.command(name="channel", description="Stop schedule for this channel.")
async def stop_channel(interaction: discord.Interaction):
    if interaction.channel_id in CHANNEL_STATES:
        del CHANNEL_STATES[interaction.channel_id]
        await interaction.response.send_message("🛑 **Stopped for this channel.**", ephemeral=False)
    else:
        await interaction.response.send_message("No schedule running.", ephemeral=True)

@stop_group.command(name="all", description="Stop ALL announcements.")
async def stop_all(interaction: discord.Interaction):
    CHANNEL_STATES.clear()
    await interaction.response.send_message("🛑 **Stopped all.**", ephemeral=False)

@tree.command(name="status", description="Check schedule status.")
async def get_status(interaction: discord.Interaction):
    state = CHANNEL_STATES.get(interaction.channel_id)
    if not state:
        await interaction.response.send_message("Status: **Idle**", ephemeral=True)
        return
    time_until_next = max(0, state.interval_seconds - (time.time() - state.last_bot_send_time))
    await interaction.response.send_message(f"Running. Next in: {time_until_next:.1f}s", ephemeral=True)

@tree.command(name="test", description="Send the next scheduled announcement immediately.")
async def test_schedule(interaction: discord.Interaction):
    state = CHANNEL_STATES.get(interaction.channel_id)
    if not state:
        await interaction.response.send_message("No schedule running.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    msg = await generate_announcement_content(state.ai_prompt) if state.is_automatic else state.scheduled_message_content
    await interaction.channel.send(f"**[Test]** {msg}")
    await interaction.followup.send("✅ Test sent!", ephemeral=True)

@tree.command(name="shea", description="Compliment Shea.")
async def compliment_shea(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    await interaction.followup.send(f"✨ {await generate_shea_compliment()}")

@tree.command(name="sheainsult", description="Insult Shea.")
async def insult_shea(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    await interaction.followup.send(f"☕ {await generate_shea_insult()}")

@tree.command(name="lyra", description="Compliment Lyra.")
async def compliment_lyra(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    await interaction.followup.send(f"💖 {await generate_lyra_compliment()}")

@tree.command(name="miwa", description="Compliment Miwa.")
async def compliment_miwa(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    await interaction.followup.send(f"🍎 {await generate_miwa_compliment()}")

@tree.command(name="hangman", description="Start or play Hangman.")
async def hangman(interaction: discord.Interaction, guess: str = None):
    channel_id = interaction.channel_id
    game = HANGMAN_GAMES.get(channel_id)

    if not game and not guess:
        await interaction.response.defer(ephemeral=False)
        word, error = await get_hangman_word()
        if error:
            await interaction.followup.send(f"❌ AI Error: {error}", ephemeral=True)
            return
        new_game = HangmanGame(word)
        msg = await interaction.followup.send(new_game.get_display_message())
        new_game.message_id = msg.id
        HANGMAN_GAMES[channel_id] = new_game
        return

    if not game and guess:
        await interaction.response.send_message("No game running!", ephemeral=True)
        return
    if game and not guess:
        await interaction.response.send_message("Game already in progress!", ephemeral=True)
        return

    if game and guess:
        await interaction.response.defer(ephemeral=True)
        game.make_guess(guess)
        try:
            message = await interaction.channel.fetch_message(game.message_id)
            await message.edit(content=game.get_display_message())
            await interaction.followup.send(f"Guessed: `{guess}`", ephemeral=True)
        except Exception:
            if channel_id in HANGMAN_GAMES: del HANGMAN_GAMES[channel_id]
        
        if game.game_over and channel_id in HANGMAN_GAMES:
            del HANGMAN_GAMES[channel_id]

if __name__ == '__main__':
    start_server_thread()
    if DISCORD_BOT_TOKEN:
        client.run(DISCORD_BOT_TOKEN)
    else:
        print("ERROR: DISCORD_BOT_TOKEN missing.")
