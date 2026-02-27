import os
import discord
import asyncio
from web_server import start_server_thread
from discord.ext import tasks
from aiohttp import ClientSession, ClientConnectorError
import json
import time
from typing import Dict, Set, List
from datetime import timedelta
from collections import defaultdict

# --- Configuration ---
# Load environment variables (set in Railway dashboard)
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# --- Constants for New Features ---
ALLOWED_GIF_ROLES = [
    1371466080404373507,
    1371466080387862661,
    1371468894853795871,
    1371466080404373506,
    1371466080366760064,
    1376303683599335434
]

TARGET_LOG_SERVER_ID = 1371466080299778138
TARGET_ADMIN_USER_ID = 748448138997530684

# --- Anti-Raid State & Config ---
ANTI_RAID_ENABLED = False
ADMIN_IDS_TO_ALERT = [386468721918607373, 748448138997530684, 833725698715025409]
user_message_times = defaultdict(list)
recent_joins = []

# --- Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True # Required for checking roles for GIF deletion and Anti-Raid joins
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)

# --- State Management (Refactored for Per-Channel) ---

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
        # NEW: Flag to override stack logic
        self.ignore_stack_logic: bool = False 

# Global dictionary to hold all active channel states
# Key: channel_id (int), Value: BotState object
CHANNEL_STATES: Dict[int, BotState] = {}

# --- Chat Mode State ---
CHAT_MODE_ACTIVE = False
# Key: user_id (int), Value: List of message history dicts for Gemini
USER_CHAT_CONTEXTS: Dict[int, List[Dict]] = {}


# --- Hangman Game State ---

# FIXED: Added 'r' before strings to handle backslashes correctly
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
            return # Don't penalize for repeat guesses

        if len(guess) > 1: # Word guess
            if guess == self.word:
                self.win = True
                self.game_over = True
                # Add all letters to guesses for display
                for letter in self.word:
                    self.guesses.add(letter)
            else:
                self.tries_left -= 1
        
        elif len(guess) == 1: # Letter guess
            self.guesses.add(guess)
            if guess not in self.word:
                self.tries_left -= 1

        # Check for win condition (all letters guessed)
        if all(letter in self.guesses for letter in self.word):
            self.win = True
            self.game_over = True

        # Check for lose condition
        if self.tries_left <= 0:
            self.game_over = True
            self.win = False

    def get_display_message(self) -> str:
        """Generates the text to display for the game state."""
        
        if self.win:
            return f"🎉 **You win!** 🎉\nThe word was: **{self.word}**"
        
        if self.game_over: # And not self.win
            return f"💀 **You lose!** 💀\nThe word was: **{self.word}**\n{HANGMAN_PICS[-1]}"

        # Game in progress
        display_word = " ".join([letter if letter in self.guesses else "＿" for letter in self.word])
        
        # Get guessed letters that are *not* in the word
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

# Global dictionary for active hangman games
# Key: channel_id (int), Value: HangmanGame object
HANGMAN_GAMES: Dict[int, HangmanGame] = {}


# --- Anti-Raid Helper Function ---
async def alert_admins(message_text: str):
    """Sends a DM pinging the specified admins for anti-raid alerts."""
    for admin_id in ADMIN_IDS_TO_ALERT:
        try:
            user = client.get_user(admin_id)
            if not user:
                user = await client.fetch_user(admin_id)
            dm_channel = await user.create_dm()
            await dm_channel.send(f"<@{admin_id}> {message_text}")
        except Exception as e:
            print(f"Failed to send Anti-Raid DM to {admin_id}: {e}")

# --- AI Service Functions ---

# Helper function for exponential backoff
async def fetch_with_backoff(session, url, payload):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with session.post(url, headers={'Content-Type': 'application/json'}, json=payload) as response:
                if response.status == 200:
                    return await response.json(), None
                elif response.status == 429: # Rate limit
                    wait_time = 2 ** attempt
                    print(f"Rate limited. Retrying in {wait_time}s...")
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
            print(f"An unexpected error occurred during API call: {e}")
            return None, f"An unexpected error occurred: {e}"
    
    return None, "Error: Failed to connect to AI service after multiple retries."


async def generate_announcement_content(prompt):
    """
    Calls the Gemini API to generate the announcement message.
    """
    if not GEMINI_API_KEY: return "Error: Gemini API Key not configured."
    # UPDATED to gemini-2.5-flash
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    system_prompt = "You are a fun, engaging, and concise community announcer bot. Generate a short, relevant message based on the user's prompt. Do not use markdown titles or headers, just plain text."
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
    }

    async with ClientSession() as session:
        result, error = await fetch_with_backoff(session, url, payload)
        
        if error:
            return error
            
        try:
            text = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', 'AI failed to generate a response.')
            return text
        except (IndexError, KeyError, TypeError):
            return "Error: AI response was not in the expected format."


async def parse_automatic_prompt(full_prompt):
    """
    Uses Gemini's structured output to parse the message and interval from a single prompt.
    """
    if not GEMINI_API_KEY: return None, "Error: Gemini API Key not configured."
    # UPDATED to gemini-2.5-flash
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
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

    async with ClientSession() as session:
        result, error = await fetch_with_backoff(session, url, payload)

        if error:
            return None, error

        try:
            json_string = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
            parsed_data = json.loads(json_string)
            
            # Ensure interval is a minimum of 10 seconds
            if parsed_data.get('interval_seconds', 0) < 10:
                parsed_data['interval_seconds'] = 10
                
            return parsed_data, None
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            return None, "Error: AI parser response was not in the expected format."


async def generate_shea_compliment():
    if not GEMINI_API_KEY: return "Error: Gemini API Key not configured."
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    system_prompt = "You are a compliment generator. Create a single, short, and weirdly specific compliment about 'Shea'. The compliment must be between 5 and 40 words. Do not use markdown titles or headers, just the text of the compliment."
    payload = {
        "contents": [{"parts": [{"text": "Generate a compliment for Shea."}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
    }
    async with ClientSession() as session:
        result, error = await fetch_with_backoff(session, url, payload)
        if error: return error
        try:
            return result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', 'Shea is like a perfectly aged cheese—complex and delightful.')
        except (IndexError, KeyError, TypeError):
            return "Error: AI response was not in the expected format."


async def generate_shea_insult():
    if not GEMINI_API_KEY: return "Error: Gemini API Key not configured."
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    system_prompt = "You are an insult generator. Create a single, funny, and passive-aggressive insult directed at 'Shea'. The insult must be between 5 and 40 words. Frame it as a backhanded compliment or a gentle, confusing dig. Do not use markdown titles or headers, just the text of the insult."
    payload = {
        "contents": [{"parts": [{"text": "Generate a passive-aggressive insult for Shea."}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
    }
    async with ClientSession() as session:
        result, error = await fetch_with_backoff(session, url, payload)
        if error: return error
        try:
            return result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', 'Shea, your ability to consistently not be the worst person in the room is truly inspiring.')
        except (IndexError, KeyError, TypeError):
            return "Error: AI response was not in the expected format."


async def generate_lyra_compliment():
    if not GEMINI_API_KEY: return "Error: Gemini API Key not configured."
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    system_prompt = "You are a compliment generator. Create a single, short, extremely corny, and awkward compliment about 'Lyra'. Use overly dramatic or slightly misplaced metaphors. The compliment must be between 5 and 40 words. Do not use markdown titles or headers, just the text of the compliment."
    payload = {
        "contents": [{"parts": [{"text": "Generate a corny and awkward compliment for Lyra."}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
    }
    async with ClientSession() as session:
        result, error = await fetch_with_backoff(session, url, payload)
        if error: return error
        try:
            return result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', 'Lyra, your presence is like a single, magnificent, sparkly unicorn tear of joy.')
        except (IndexError, KeyError, TypeError):
            return "Error: AI response was not in the expected format."


# --- NEW: Hangman Word Generator ---
async def get_hangman_word():
    """
    Calls the Gemini API to generate a single, SFW word for Hangman.
    """
    if not GEMINI_API_KEY: return None, "Error: Gemini API Key not configured."
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    system_prompt = "Generate a single, random, SFW (School/Work-Safe) word for a game of Hangman. The word should be between 6 and 12 letters long and must not be a proper noun. Only output the JSON object."

    schema = {
        "type": "OBJECT",
        "properties": {
            "word": {
                "type": "STRING",
                "description": "A single SFW hangman word, 6-12 chars, no proper nouns."
            }
        },
        "required": ["word"]
    }

    payload = {
        "contents": [{"parts": [{"text": "Give me one hangman word."}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema
        }
    }

    async with ClientSession() as session:
        result, error = await fetch_with_backoff(session, url, payload)

        if error:
            return None, error

        try:
            json_string = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
            parsed_data = json.loads(json_string)
            word = parsed_data.get('word')
            
            if not word or not (6 <= len(word) <= 12) or not word.isalpha():
                return "default", None # Fallback
            
            return word.lower(), None
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            return "fallback", None # Fallback

# --- NEW: Chat Logic ---
async def generate_chat_response(user_id, user_name, user_input):
    if not GEMINI_API_KEY: return "My brain is missing (API Key Error)."
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    # 1. Retrieve or Initialize History
    if user_id not in USER_CHAT_CONTEXTS:
        USER_CHAT_CONTEXTS[user_id] = []
    
    history = USER_CHAT_CONTEXTS[user_id]
    
    # 2. Append User Message
    history.append({"role": "user", "parts": [{"text": user_input}]})
    
    # Keep history manageable (last 10 messages = 5 turns)
    if len(history) > 10:
        history = history[-10:]
        USER_CHAT_CONTEXTS[user_id] = history

    # 3. System Prompt (Persona)
    # UPDATED: Added instructions to keep responses short and match user length.
    persona_prompt = (
        f"You are an 18-year-old girl. You are highly intelligent, very funny, and you are allowed to swear. "
        f"You are chatting with a user named {user_name} on Discord. "
        "IMPORTANT: Keep your responses short and concise. Match the length of the user's message. "
        "If they send a short text, reply with a short text. Never write a paragraph unless the topic is deep. "
        "Keep it casual, use slang, and do not be robotic. Just hang out."
    )

    payload = {
        "contents": history,
        "systemInstruction": {"parts": [{"text": persona_prompt}]},
    }

    async with ClientSession() as session:
        result, error = await fetch_with_backoff(session, url, payload)
        
        if error:
            return "I'm having a headache. (API Error)"

        try:
            response_text = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
            
            if response_text:
                # Add model response to history
                history.append({"role": "model", "parts": [{"text": response_text}]})
                USER_CHAT_CONTEXTS[user_id] = history # Update global dict
                return response_text
            else:
                return "..."
                
        except (IndexError, KeyError, TypeError):
            return "I don't know what to say."


# --- Background Task (Refactored) ---

@tasks.loop(seconds=1)
async def send_scheduled_message():
    # Iterate over a copy of the items to allow for safe deletion
    for channel_id, state in list(CHANNEL_STATES.items()):
        
        if state.interval_seconds == 0:
            continue

        if time.time() - state.last_bot_send_time >= state.interval_seconds:
            
            # Anti-Stacking Logic
            # If ignore_stack_logic is True, we SKIP this block
            if not state.ignore_stack_logic and state.last_channel_activity_time <= state.last_bot_send_time:
                print(f"Channel {channel_id} is idle. Skipping scheduled message.")
                state.last_bot_send_time = time.time() # Reset timer to prevent spam
                continue
            
            # Debug print for override
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
    # Add the new command group
    tree.add_command(stop_group)
    await tree.sync()
    print(f'Logged in as {client.user} (ID: {client.user.id})')
    print('Bot is ready and running.')

    if not send_scheduled_message.is_running():
        send_scheduled_message.start()
        print("Scheduler task started.")

@client.event
async def on_member_join(member):
    """
    Anti-Raid Join Spike Filter
    Bans raiders if there is a sudden influx of accounts joining.
    """
    global recent_joins
    
    if not ANTI_RAID_ENABLED:
        return
        
    current_time = time.time()
    recent_joins.append(current_time)
    
    # Keep only joins from the last 10 seconds
    recent_joins = [t for t in recent_joins if current_time - t <= 10]
    
    # Threshold: 5 accounts joining within a 10 second window
    if len(recent_joins) >= 5:
        try:
            await member.ban(reason="Anti-Raid: Join Spike Detected")
            await alert_admins(f"🚨 **RAID ALERT:** Join spike detected! Banned new account: {member.mention} (`{member.id}`)")
        except discord.Forbidden:
            await alert_admins(f"🚨 **RAID ALERT:** Join spike detected, but I lack permissions to ban {member.mention}!")
        except Exception as e:
            print(f"Error processing Anti-Raid ban: {e}")

@client.event
async def on_message_delete(message):
    """
    Logs deleted messages and GIFs from the specific server and DMs them to the specified admin.
    """
    # Only target the specific server
    if message.guild is None or message.guild.id != TARGET_LOG_SERVER_ID:
        return
        
    # Ignore bot deletions to prevent loops or spam
    if message.author.bot:
        return

    text_content = message.content
    gif_urls = []

    # Check for GIF attachments
    for attachment in message.attachments:
        if attachment.filename.lower().endswith(".gif") or (attachment.content_type and "gif" in attachment.content_type):
            gif_urls.append(attachment.url)

    # If it was just a regular picture/file (not a gif) and had no text, ignore it
    if not text_content and not gif_urls:
        return

    try:
        # Fetch the user to DM (teleostwind)
        target_user = client.get_user(TARGET_ADMIN_USER_ID)
        if not target_user:
            target_user = await client.fetch_user(TARGET_ADMIN_USER_ID)

        dm_channel = await target_user.create_dm()

        # Build the alert message
        log_text = f"🗑️ **Deleted Message Alert** in **{message.guild.name}** -> #{message.channel.name}\n"
        log_text += f"**Author:** {message.author.mention} (`{message.author.id}`)\n\n"
        
        if text_content:
            log_text += f"**Text Content:**\n{text_content}\n\n"
        if gif_urls:
            log_text += f"**Deleted GIF Attachment(s):**\n" + "\n".join(gif_urls)

        await dm_channel.send(log_text)
    except Exception as e:
        print(f"Error sending deleted message DM: {e}")

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    # --- Anti-Raid System Filters ---
    if ANTI_RAID_ENABLED and isinstance(message.author, discord.Member) and not message.author.bot:
        # 1. Mass Mention Filter
        # If they ping more than 5 users in one message, delete and ban.
        if len(message.mentions) > 5:
            try:
                await message.delete()
                await message.author.ban(reason="Anti-Raid: Mass Ping Detected")
                await alert_admins(f"🚨 **RAID ALERT:** Mass ping detected! Banned raider: {message.author.mention} (`{message.author.id}`)")
            except discord.Forbidden:
                await alert_admins(f"🚨 **RAID ALERT:** Mass ping detected, but I lack permissions to ban {message.author.mention}!")
            return # Stop processing anything else

        # 2. Velocity Filter (Anti-Spam)
        # Limits to 7 messages in 5 seconds. If exceeded: 1 minute timeout.
        author_id = message.author.id
        current_time = time.time()
        user_message_times[author_id].append(current_time)
        
        # Keep only timestamps from the last 5 seconds for this user
        user_message_times[author_id] = [t for t in user_message_times[author_id] if current_time - t <= 5]
        
        if len(user_message_times[author_id]) >= 7:
            try:
                await message.delete()
                duration = timedelta(minutes=1)
                await message.author.timeout(duration, reason="Anti-Raid: Spam Detected")
                await alert_admins(f"⚠️ **SPAM ALERT:** User {message.author.mention} (`{message.author.id}`) sent 7+ messages in 5 seconds. They have been given a 1-minute timeout.")
                
                # Clear their history so it doesn't trigger repeatedly on lingering timestamps
                user_message_times[author_id].clear()
            except discord.Forbidden:
                pass
            return # Stop processing


    # --- GIF Block Filter (Merged from Bot 2) ---
    # First, verify the author is an actual Member in a Server (not DMing the bot)
    if isinstance(message.author, discord.Member):
        # Check if the user has any of the allowed roles
        has_allowed_role = any(role.id in ALLOWED_GIF_ROLES for role in message.author.roles)
        
        if not has_allowed_role:
            content_lower = message.content.lower()
            is_gif = False
            
            # Look for common GIF domains and extensions in the text
            if "tenor.com/view" in content_lower or "giphy.com/gifs" in content_lower or ".gif" in content_lower:
                is_gif = True
                
            # If no link was found, check if they uploaded a GIF file directly
            if not is_gif:
                for attachment in message.attachments:
                    if attachment.filename.lower().endswith(".gif") or (attachment.content_type and "gif" in attachment.content_type):
                        is_gif = True
                        break
            
            # If a GIF is detected, delete it (REMOVED: sending the Andrew Tate GIF)
            if is_gif:
                try:
                    await message.delete()
                except discord.Forbidden:
                    pass # Fails safely if bot lacks permission
                return # Stop processing so chat logic isn't run for deleted message

    # Update channel activity time if it has a schedule
    if message.channel.id in CHANNEL_STATES:
        CHANNEL_STATES[message.channel.id].last_channel_activity_time = time.time()
    
    # --- NEW: Chat Mode Trigger ---
    if CHAT_MODE_ACTIVE:
        is_mentioned = client.user in message.mentions
        is_reply = (message.reference and message.reference.resolved and 
                    message.reference.resolved.author == client.user)
        
        if is_mentioned or is_reply:
            # Show typing indicator while generating
            async with message.channel.typing():
                # Clean content: Remove bot mention from text to not confuse AI
                clean_text = message.content.replace(f'<@{client.user.id}>', '').strip()
                if not clean_text: clean_text = "Hello!" # Handle empty ping
                
                response = await generate_chat_response(message.author.id, message.author.name, clean_text)
                await message.reply(response)
            return # Don't process other logic if we chatted

    # Check if this is a guess for a hangman game
    if message.channel.id in HANGMAN_GAMES:
        # We handle guesses via slash command now, so this can be ignored
        pass
    
    # This function is needed if you use hybrid commands, but not for slash-only
    # await client.process_commands(message) 


# --- Helper Function ---
def get_display_interval(interval_seconds: int) -> str:
    """Converts seconds to a readable H/M/S string."""
    if interval_seconds >= 3600 and interval_seconds % 3600 == 0:
        return f"{interval_seconds // 3600} hours"
    elif interval_seconds >= 60 and interval_seconds % 60 == 0:
        return f"{interval_seconds // 60} minutes"
    else:
        return f"{interval_seconds} seconds"


# --- Slash Commands ---

# --- NEW: Anti-Raid Command ---
@tree.command(name="antiraid", description="Toggle Anti-Raid mode on or off.")
@discord.app_commands.describe(action="Start or Stop", password="Password required.")
@discord.app_commands.choices(action=[
    discord.app_commands.Choice(name="Start", value="start"),
    discord.app_commands.Choice(name="Stop", value="stop")
])
async def antiraid_toggle(interaction: discord.Interaction, action: str, password: str):
    global ANTI_RAID_ENABLED
    
    if password != "britishfoodsucks":
        await interaction.response.send_message("❌ **Access Denied:** Incorrect password.", ephemeral=True)
        return

    if action == "start":
        ANTI_RAID_ENABLED = True
        await interaction.response.send_message("🛡️ **Anti-Raid Mode Activated!** The server is now protected.", ephemeral=False)
        await alert_admins("🛡️ **System Alert:** Anti-Raid Mode has been **ACTIVATED**.")
    else:
        ANTI_RAID_ENABLED = False
        await interaction.response.send_message("⚠️ **Anti-Raid Mode Deactivated.**", ephemeral=False)
        await alert_admins("⚠️ **System Alert:** Anti-Raid Mode has been **DEACTIVATED**.")


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

    # Get existing state or create a new one
    state = CHANNEL_STATES.get(interaction.channel_id, BotState(interaction.channel_id))
    
    state.interval_seconds = interval_seconds
    state.scheduled_message_content = message
    state.is_automatic = False
    state.ignore_stack_logic = False # Default behavior
    state.last_bot_send_time = time.time()
    state.last_channel_activity_time = time.time()
    
    CHANNEL_STATES[interaction.channel_id] = state # Add/update in global dict
    
    await interaction.response.send_message(f"✅ **Manual Scheduled!** Interval: **{interval_hours} hours**.", ephemeral=False)

@tree.command(name="automatic", description="Schedule an AI message for this channel (e.g., 'Say 'bark' every 10 seconds').")
@discord.app_commands.describe(full_prompt="The message prompt AND interval (e.g., 'Say a fun fact every 2 hours').")
async def automatic_schedule(interaction: discord.Interaction, full_prompt: str):
    if not GEMINI_API_KEY:
        await interaction.response.send_message("❌ **Error:** `GEMINI_API_KEY` is missing.", ephemeral=True)
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
    state = CHANNEL_STATES.get(interaction.channel_id, BotState(interaction.channel_id))
    
    state.interval_seconds = interval_seconds
    state.ai_prompt = ai_prompt
    state.is_automatic = True
    state.ignore_stack_logic = False # Default behavior
    state.last_bot_send_time = time.time()
    state.last_channel_activity_time = time.time()
    
    CHANNEL_STATES[interaction.channel_id] = state
    
    display_interval = get_display_interval(interval_seconds)
    
    confirmation_message = (
        f"🤖 **Automatic Scheduled!**\n"
        f"**Task:** Generate a message based on the prompt: '{ai_prompt}'\n"
        f"**Interval:** **{display_interval}**"
    )
    await interaction.followup.send(confirmation_message, ephemeral=False)

# --- NEW: Ignore Stack Logic Command ---
@tree.command(name="ignore_stack_logic", description="ADMIN: Forces 'Hi' every 10s, ignoring idle checks.")
@discord.app_commands.describe(password="Enter the admin password.")
async def ignore_stack_logic(interaction: discord.Interaction, password: str):
    if password != "12344321":
        await interaction.response.send_message("❌ **Access Denied:** Incorrect password.", ephemeral=True)
        return

    # Get existing state or create a new one
    state = CHANNEL_STATES.get(interaction.channel_id, BotState(interaction.channel_id))
    
    state.interval_seconds = 10
    state.scheduled_message_content = "Hi"
    state.is_automatic = False
    state.ignore_stack_logic = True # Enable the override
    
    # CRITICAL FIX: Set the last send time to the past (-15 seconds)
    # This tricks the bot into sending the FIRST message immediately.
    state.last_bot_send_time = time.time() - 15 
    state.last_channel_activity_time = time.time() 
    
    CHANNEL_STATES[interaction.channel_id] = state 
    
    await interaction.response.send_message("⚠️ **Override Enabled:** Sending 'Hi' every 10 seconds. Starting immediately.", ephemeral=False)

# --- NEW: Chat Mode Command ---
@tree.command(name="chat", description="Enable/Disable the 18yo Chat Persona.")
@discord.app_commands.describe(action="Start or Stop", password="Password required.")
@discord.app_commands.choices(action=[
    discord.app_commands.Choice(name="Start", value="start"),
    discord.app_commands.Choice(name="Stop", value="stop")
])
# FIXED: Changed discord.Choice[str] to str
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
        USER_CHAT_CONTEXTS.clear() # Clear memory on stop
        await interaction.response.send_message("🔴 **Chat Mode Deactivated.** She is asleep.", ephemeral=False)


# --- NEW: Global Announcement Command ---
@tree.command(name="announcement", description="Send an announcement to a specific channel ID.")
@discord.app_commands.describe(channel_id="The ID of the channel to send to", message="The text to send", password="Password required.")
async def global_announcement(interaction: discord.Interaction, channel_id: str, message: str, password: str):
    if password != "1234321":
        await interaction.response.send_message("❌ **Access Denied:** Incorrect password.", ephemeral=True)
        return

    try:
        # Convert ID to int just in case
        target_id = int(channel_id)
        target_channel = client.get_channel(target_id)
        
        if not target_channel:
             # Try to fetch if not in cache (rare but possible)
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
# Note: The old /stop command is removed, and this group is added in on_ready

@tree.command(name="status", description="Check the schedule status for this channel.")
async def get_status(interaction: discord.Interaction):
    state = CHANNEL_STATES.get(interaction.channel_id)
    
    if not state:
        await interaction.response.send_message("Status: **Idle** (No schedule for this channel).", ephemeral=True)
        return
        
    channel_name = interaction.channel.name if interaction.channel else "Unknown Channel"
    mode = "Automatic (AI)" if state.is_automatic else "Manual (Fixed)"
    time_since_send = time.time() - state.last_bot_send_time
    
    # Modified status check for ignore_stack_logic
    is_waiting = "No (Ignored)" if state.ignore_stack_logic else ("Yes (Awaiting chat activity)" if state.last_channel_activity_time <= state.last_bot_send_time else "No")
    
    # Calculate time until next send
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

# --- NEW: /test Command ---
@tree.command(name="test", description="Send the next scheduled announcement for this channel immediately (one-time).")
async def test_schedule(interaction: discord.Interaction):
    state = CHANNEL_STATES.get(interaction.channel_id)

    if not state:
        await interaction.response.send_message("No schedule running for this channel to test.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True) # Acknowledge, but hide "thinking"

    message_to_send = ""
    if state.is_automatic:
        message_to_send = await generate_announcement_content(state.ai_prompt)
    else:
        message_to_send = state.scheduled_message_content
    
    try:
        # Send the test message to the channel
        await interaction.channel.send(f"**[Test Announcement]** {message_to_send}")
        # Send a private confirmation to the user
        await interaction.followup.send("✅ Test message sent!", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ Error: Missing permissions to send message to this channel.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ An error occurred: {e}", ephemeral=True)
    
    # CRITICAL: We do NOT update state.last_bot_send_time
    # This ensures the original schedule is not affected.


@tree.command(name="shea", description="Gives Shea a random, weirdly specific compliment.")
async def compliment_shea(interaction: discord.Interaction):
    if not GEMINI_API_KEY:
        await interaction.response.send_message("❌ **Error:** `GEMINI_API_KEY` is missing.", ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=False)
    compliment = await generate_shea_compliment()
    
    if compliment.startswith("Error:"):
        await interaction.followup.send(f"❌ **AI Compliment Failed!** Reason: {compliment}", ephemeral=False)
    else:
        await interaction.followup.send(f"✨ A message for Shea: {compliment}")

@tree.command(name="sheainsult", description="Insults Shea in a funny, passive-aggressive way.")
async def insult_shea(interaction: discord.Interaction):
    if not GEMINI_API_KEY:
        await interaction.response.send_message("❌ **Error:** `GEMINI_API_KEY` is missing.", ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=False)
    insult = await generate_shea_insult()
    
    if insult.startswith("Error:"):
        await interaction.followup.send(f"❌ **AI Insult Failed!** Reason: {insult}", ephemeral=False)
    else:
        await interaction.followup.send(f"☕ A kind message for Shea: {insult}")
        
@tree.command(name="lyra", description="Gives Lyra a random, corny and awkward compliment.")
async def compliment_lyra(interaction: discord.Interaction):
    if not GEMINI_API_KEY:
        await interaction.response.send_message("❌ **Error:** `GEMINI_API_KEY` is missing.", ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=False)
    compliment = await generate_lyra_compliment()
    
    if compliment.startswith("Error:"):
        await interaction.followup.send(f"❌ **AI Compliment Failed!** Reason: {compliment}", ephemeral=False)
    else:
        await interaction.followup.send(f"💖 A truly sincere, yet slightly confusing, message for Lyra: {compliment}")


# --- NEW: /hangman Command ---
@tree.command(name="hangman", description="Start or play a game of Hangman.")
@discord.app_commands.describe(guess="Guess a letter or the whole word.")
async def hangman(interaction: discord.Interaction, guess: str = None):
    channel_id = interaction.channel_id
    game = HANGMAN_GAMES.get(channel_id)

    if not game and not guess:
        # Start a new game
        if not GEMINI_API_KEY:
            await interaction.response.send_message("❌ **Error:** `GEMINI_API_KEY` is missing, cannot get a word.", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=False) # Defer publicly
        
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
        # Trying to guess without a game
        await interaction.response.send_message("No game is running! Start one with `/hangman`.", ephemeral=True)
        return

    if game and not guess:
        # Trying to start a game mid-game
        await interaction.response.send_message("A game is already in progress in this channel!", ephemeral=True)
        return

    if game and guess:
        # Making a guess
        if not game.message_id:
            await interaction.response.send_message("Game state is broken, please start a new game with `/hangman`.", ephemeral=True)
            if channel_id in HANGMAN_GAMES: del HANGMAN_GAMES[channel_id]
            return
            
        await interaction.response.defer(ephemeral=True) # Defer privately for the guesser
        
        game.make_guess(guess)
        
        try:
            # Fetch the original game message
            message = await interaction.channel.fetch_message(game.message_id)
            # Edit the message with the new state
            await message.edit(content=game.get_display_message())
            # Send a silent confirmation to the guesser
            await interaction.followup.send(f"You guessed: `{guess}`", ephemeral=True)
            
        except discord.NotFound:
            # Message was deleted
            await interaction.followup.send("The game message was deleted! Game over.", ephemeral=True)
            if channel_id in HANGMAN_GAMES: del HANGMAN_GAMES[channel_id]
        except Exception as e:
            print(f"Error updating hangman: {e}")
            await interaction.followup.send(f"Error updating game: {e}", ephemeral=True)

        if game.game_over:
            # Clean up the finished game
            if channel_id in HANGMAN_GAMES:
                del HANGMAN_GAMES[channel_id]
        return


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
