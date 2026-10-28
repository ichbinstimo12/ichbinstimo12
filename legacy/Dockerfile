# Use a slim python image for the smallest footprint on your VPS
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Run the agent
# We use a shell script to load .env variables
CMD ["python", "-c", "import sys; sys.path.append('.'); import main; main.start_agent()"]
