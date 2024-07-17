# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Install necessary packages for Wine
RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    wine32 \
    xvfb \
    wget \
    unzip

# Download and install MetaTrader 5 terminal
RUN wget -q https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe -O /tmp/mt5setup.exe && \
    wine /tmp/mt5setup.exe /silent && \
    rm /tmp/mt5setup.exe

# Copy the current directory contents into the container
COPY . /app

# Set the working directory to /app
WORKDIR /app

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Set environment variables for the display (necessary for Wine)
ENV DISPLAY=:99

# Run the X virtual framebuffer in the background and the Flask app
CMD ["sh", "-c", "Xvfb :99 -screen 0 1024x768x16 & python app.py"]
