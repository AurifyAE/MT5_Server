import os
import json
import logging
from datetime import datetime, timedelta, timezone
from flask import Flask, request
from flask_cors import CORS
import MetaTrader5 as mt5
from werkzeug.exceptions import HTTPException
from pytz import timezone as pytz_timezone
from flask_socketio import SocketIO
from dotenv import load_dotenv
from collections import defaultdict

# Load environment variables from .env file
load_dotenv()

# Initialize Flask application
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', os.urandom(24))
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Initialize Socket.IO
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Your pre-defined secret key
SERVER_SECRET_KEY = "aurify@123"

# MT5 Credentials (from environment variables)
login = int(os.getenv("MT5_LOGIN"))
password = os.getenv("MT5_PASSWORD")
server = os.getenv("MT5_SERVER")

# Symbol mapping
SYMBOL_MAP = {
    "GOLD": "XAUUSD",
    "SILVER": "XAGUSD",
    "PLATINUM": "XPTUSD"
}
REVERSE_SYMBOL_MAP = {v: k for k, v in SYMBOL_MAP.items()}

# Caches
rates_cache = {}
high_low_cache = {}
last_market_update_cache = {}

# Global variable to track MT5 initialization state
mt5_initialized = False

# Set of active symbols per client
client_sessions = defaultdict(set)


def normalize_symbol(symbol):
    """Normalize the symbol to uppercase and map it using SYMBOL_MAP."""
    symbol = symbol.upper()
    return SYMBOL_MAP.get(symbol, symbol)


def get_market_status(symbol):
    if not initialize_mt5():
        logger.error("MT5 is not initialized. Cannot get market status.")
        return "unknown"

    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        logger.error(f"Failed to get symbol info for {symbol}")
        return "unknown"

    trade_mode = symbol_info.trade_mode
    if trade_mode == mt5.SYMBOL_TRADE_MODE_FULL:
        return "open"
    elif trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
        return "closed"
    else:
        return "unknown"


def get_high_low(symbol):
    try:
        now = datetime.now()
        midnight = datetime.combine(now.date(), datetime.min.time())
        rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_D1, midnight, now)
        if not rates:
            logger.error(f"Error: Could not retrieve high/low data for {symbol}. Last error: {mt5.last_error()}")
            return None, None
        high, low = rates[0]['high'], rates[0]['low']
        high_low_cache[symbol] = {'high': high, 'low': low}
        return high, low
    except Exception as e:
        logger.error(f"Exception occurred while retrieving high/low for {symbol}: {e}")
        return None, None


def store_last_closing_values(symbol):
    rates = mt5.copy_rates_from(symbol, mt5.TIMEFRAME_D1, 1, 1)
    if rates is not None and len(rates) > 0:
        last_close = rates[0]['close']
        last_high = rates[0]['high']
        last_low = rates[0]['low']
        last_market_update_cache[symbol] = {
            "close": last_close,
            "high": last_high,
            "low": last_low,
            "stored_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        logger.info(f"Stored last closing values for {symbol}: Close: {last_close}, High: {last_high}, Low: {last_low}")
    else:
        logger.error(f"Failed to retrieve last closing values for {symbol}")


def initialize_mt5():
    global mt5_initialized
    if not mt5_initialized:
        if mt5.initialize() and mt5.login(login, password, server):
            logger.info("MT5 login successful")
            mt5_initialized = True
            # Store last closing values for all symbols
            for symbol in SYMBOL_MAP.values():
                store_last_closing_values(symbol)
        else:
            logger.error(f"MT5 login failed, error: {mt5.last_error()}")
            mt5.shutdown()
            return False
    return mt5_initialized


if not initialize_mt5():
    logger.error("Failed to initialize MT5. Exiting.")
    quit()


def update_rates_cache():
    if not initialize_mt5():
        logger.error("MT5 is not initialized. Cannot update rates cache.")
        return

    for sid, symbols in client_sessions.items():
        for symbol in symbols:
            try:
                mt5_symbol = normalize_symbol(symbol)
                if not mt5.symbol_select(mt5_symbol, True):
                    logger.error(f"Error: Symbol {mt5_symbol} is not available.")
                    continue

                market_status = get_market_status(mt5_symbol)

                if market_status == "closed":
                    # If market is closed, use last stored values
                    if mt5_symbol not in last_market_update_cache:
                        store_last_closing_values(mt5_symbol)

                    last_data = last_market_update_cache.get(mt5_symbol, {})
                    data = {
                        "symbol": REVERSE_SYMBOL_MAP.get(mt5_symbol, symbol).title(),
                        "bid": last_data.get('close', 0),
                        "high": last_data.get('high', 0),
                        "low": last_data.get('low', 0),
                        "marketStatus": market_status,
                    }
                else:
                    # If market is open, use live data
                    tick = mt5.symbol_info_tick(mt5_symbol)
                    if not tick:
                        logger.error(f"Error: Could not retrieve data for {mt5_symbol}. Last error: {mt5.last_error()}")
                        continue

                    high, low = get_high_low(mt5_symbol)
                    data = {
                        "symbol": REVERSE_SYMBOL_MAP.get(mt5_symbol, symbol).title(),
                        "bid": tick.bid,
                        "high": high or 0,
                        "low": low or 0,
                        "marketStatus": market_status,
                    }

                socketio.emit('market-data', data, room=sid)
            except Exception as e:
                logger.error(f"Exception occurred while updating rates for {symbol}: {e}")


def continuous_update():
    while True:
        update_rates_cache()
        socketio.sleep(0.1)  # 100 milliseconds delay


@socketio.on('connect')
def handle_connect():
    client_secret_key = request.args.get('secret')
    if client_secret_key != SERVER_SECRET_KEY:
        logger.warning(f"Unauthorized connection attempt from client {request.sid}. Invalid secret key.")
        socketio.emit('error', {'message': 'Unauthorized: Invalid secret key.'}, room=request.sid)
        return False
    logger.info(f"Client {request.sid} connected with valid secret key.")
    socketio.emit('connected', {'message': 'Connection established with valid secret key.'}, room=request.sid)


@socketio.on('request-data')
def handle_request_data(symbols):
    if not isinstance(symbols, list):
        symbols = [symbols]
    normalized_symbols = set(normalize_symbol(symbol) for symbol in symbols)
    client_sessions[request.sid].update(normalized_symbols)
    logger.info(
        f"Client {request.sid} subscribed to symbols: {[REVERSE_SYMBOL_MAP.get(s, s) for s in normalized_symbols]}")


@socketio.on('stop-data')
def handle_stop_data(symbols):
    if not isinstance(symbols, list):
        symbols = [symbols]
    normalized_symbols = set(normalize_symbol(symbol) for symbol in symbols)
    client_sessions[request.sid].difference_update(normalized_symbols)
    logger.info(
        f"Client {request.sid} unsubscribed from symbols: {[REVERSE_SYMBOL_MAP.get(s, s) for s in normalized_symbols]}")


@socketio.on('disconnect')
def handle_disconnect():
    client_sessions.pop(request.sid, None)
    logger.info(f"Client {request.sid} disconnected and session data cleared.")


@app.route('/')
def index():
    return "Welcome to the MT5 API"


@app.route('/favicon.ico')
def favicon():
    return '', 204


@app.errorhandler(HTTPException)
def handle_http_exception(e):
    response = e.get_response()
    response.data = json.dumps({
        "code": e.code,
        "name": e.name,
        "description": e.description
    })
    response.content_type = "application/json"
    return response


if __name__ == "__main__":
    socketio.start_background_task(continuous_update)
    socketio.run(app, host="0.0.0.0", port=8000, debug=False)