# Use an official lightweight Python image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file first to leverage Docker cache
COPY requirements.txt .

# Install the Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code into the container
# This copies main.py, web_server.py, etc.
COPY . .

# This command will be run when the container starts
# It correctly executes your main.py script
CMD ["python", "main.py"]
