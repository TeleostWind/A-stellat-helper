# Start from a base image with Python
FROM python:3.10-slim

# Set the working directory inside the container
WORKDIR /usr/src/app

# Copy the requirements file and install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY . .

# Set the command to run your bot
CMD ["python", "your_main_bot_file.py"]
# NOTE: Replace 'your_main_bot_file.py' with your actual bot's entry file (e.g., bot.py)
