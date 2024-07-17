import os
import json
import time
import logging
from datetime import datetime, timedelta
from threading import Thread, Lock
from dotenv import load_dotenv
from flask import Flask, jsonify, Response
from flask_cors import CORS, cross_origin
import MetaTrader5 as mt5
from werkzeug.exceptions import HTTPException

# Load environment variables from .env file
load_dotenv()

# Initialize Flask application
app = Flask(__name__)

# Set secret key from environment variable, with a default value
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', os.urandom(24))

# Enable CORS with dynamic origin determination
cors = CORS(app, resources={r"/api/*": {"origins": "*"}})

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Read environment variables (MT5 credentials)
login = int(os.getenv("MT5_LOGIN"))
password = os.getenv("MT5_PASSWORD")
server = os.getenv("MT5_SERVER")

# Specify symbols directly in the code
symbols = ['XAUUSD', 'XAGUSD']

# Cache for rates and high/low prices
rates_cache = {}
high_low_cache = {}
cache_lock = Lock()


# Function to initialize and login to MetaTrader 5
def initialize_mt5():
    if not mt5.initialize():
        logger.error("initialize() failed, error code = %s", mt5.last_error())
        quit()

    if not mt5.login(login, password, server):
        logger.error("Login failed, error code = %s", mt5.last_error())
        mt5.shutdown()
        quit()
    else:
        logger.info("Login successful")

    for symbol in symbols:
        if not mt5.symbol_select(symbol, True):
            logger.error("Error: Symbol %s is not available in the MetaTrader 5 platform", symbol)
            mt5.shutdown()
            quit()


initialize_mt5()


# Function to get high and low prices for the day
def get_high_low(symbol):
    now = datetime.now()
    date_key = now.strftime("%Y-%m-%d")

    if symbol in high_low_cache and high_low_cache[symbol]['date'] == date_key:
        return high_low_cache[symbol]['high'], high_low_cache[symbol]['low']

    midnight = datetime.combine(now.date(), datetime.min.time())
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_D1, midnight, now)

    if rates is None or len(rates) == 0:
        logger.error("Error: Could not retrieve high/low data for %s. Last error: %s", symbol, mt5.last_error())
        return None, None

    high, low = rates[0]['high'], rates[0]['low']
    high_low_cache[symbol] = {'date': date_key, 'high': high, 'low': low}

    return high, low


# Function to update rates cache
def update_rates_cache():
    while True:
        new_rates = {}
        for symbol in symbols:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                logger.error("Error: Could not retrieve data for %s. Last error: %s", symbol, mt5.last_error())
                continue

            high, low = get_high_low(symbol)
            new_rates[symbol] = {
                "bid": tick.bid,
                "ask": tick.ask,
                "high": high,
                "low": low,
                "time": tick.time
            }

        with cache_lock:
            rates_cache.update(new_rates)

        time.sleep(0.1)  # Update interval


# Start the background thread for updating rates cache
update_thread = Thread(target=update_rates_cache, daemon=True)
update_thread.start()


# Generator function to stream live data
def stream_live_data():
    while True:
        with cache_lock:
            rates = json.dumps({
                "gold": {
                    "bid": rates_cache.get('XAUUSD', {}).get('bid'),
                    "ask": rates_cache.get('XAUUSD', {}).get('ask'),
                    "high": rates_cache.get('XAUUSD', {}).get('high'),
                    "low": rates_cache.get('XAUUSD', {}).get('low'),
                    "time": rates_cache.get('XAUUSD', {}).get('time')
                },
                "silver": {
                    "bid": rates_cache.get('XAGUSD', {}).get('bid'),
                    "ask": rates_cache.get('XAGUSD', {}).get('ask'),
                    "high": rates_cache.get('XAGUSD', {}).get('high'),
                    "low": rates_cache.get('XAGUSD', {}).get('low'),
                    "time": rates_cache.get('XAGUSD', {}).get('time')
                }
            })
        yield f"data: {rates}\n\n"
        time.sleep(0.1)  # Update interval


# API endpoint to serve live data
@app.route('/api/rates', methods=['GET'])
@cross_origin()
def api_rates():
    return Response(stream_live_data(), mimetype='text/event-stream')


# Error handler for HTTP errors
@app.errorhandler(HTTPException)
def handle_http_exception(e):
    response = e.get_response()
    response.data = json.dumps({
        "code": e.code,
        "name": e.name,
        "description": e.description,
    })
    response.content_type = "application/json"
    logger.error(f"HTTP Exception: {e}")
    return response


# General error handler
@app.errorhandler(Exception)
def handle_exception(e):
    response = jsonify({
        "code": 500,
        "name": "Internal Server Error",
        "description": str(e),
    })
    response.content_type = "application/json"
    logger.error(f"Exception: {e}")
    return response, 500


# Run the Flask application
if __name__ == '__main__':
    # Run Flask app
    app.run(debug=False)

