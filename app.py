import os
import time
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, render_template, request

APP = Flask(__name__)
DATA_DIR = os.environ.get('DATA_DIR', '/data')
DB_PATH = os.path.join(DATA_DIR, 'usage.sqlite3')
POLL_SECONDS = max(1, int(os.environ.get('POLL_SECONDS', '2')))
TZ_NAME = os.environ.get('TZ', 'Asia/Kolkata')
LOCK = threading.RLock()
RATE_RX = 0.0
RATE_TX = 0.0
LAST_RATE_TS = None
ACTIVE_INTERFACE = os.environ.get('INTERFACE', 'eth0')
DISPLAY_NAME = os.environ.get('DISPLAY_NAME', 'Internet Usage')


def local_now():
    try:
        return datetime.now(ZoneInfo(TZ_NAME))
    except Exception:
        return datetime.now()


def available_interfaces():
    try:
        return sorted(x for x in os.listdir('/sys/class/net') if os.path.isdir(f'/sys/class/net/{x}/statistics'))
    except Exception:
        return [ACTIVE_INTERFACE]


def read_interface_counters(interface=None):
    interface = interface or ACTIVE_INTERFACE
    root = f'/sys/class/net/{interface}/statistics'
    with open(os.path.join(root, 'rx_bytes')) as f:
        rx = int(f.read().strip())
    with open(os.path.join(root, 'tx_bytes')) as f:
        tx = int(f.read().strip())
    return rx, tx


@contextmanager
def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    global ACTIVE_INTERFACE, DISPLAY_NAME
    with connect() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)''')
        con.execute('''CREATE TABLE IF NOT EXISTS usage_state (
            interface TEXT PRIMARY KEY, last_rx INTEGER NOT NULL, last_tx INTEGER NOT NULL,
            total_rx INTEGER NOT NULL DEFAULT 0, total_tx INTEGER NOT NULL DEFAULT 0,
            reset_rx INTEGER NOT NULL DEFAULT 0, reset_tx INTEGER NOT NULL DEFAULT 0,
            month_key TEXT NOT NULL, month_rx INTEGER NOT NULL DEFAULT 0,
            month_tx INTEGER NOT NULL DEFAULT 0, updated_at TEXT)''')
        con.execute('''CREATE TABLE IF NOT EXISTS usage_monthly (
            interface TEXT NOT NULL, month_key TEXT NOT NULL,
            rx_bytes INTEGER NOT NULL DEFAULT 0, tx_bytes INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(interface, month_key))''')
        # Preserve data from the original single-interface database on upgrade.
        old = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='state'").fetchone()
        if old:
            row = con.execute('SELECT * FROM state WHERE id=1').fetchone()
            if row:
                con.execute('''INSERT OR IGNORE INTO usage_state
                    (interface,last_rx,last_tx,total_rx,total_tx,reset_rx,reset_tx,month_key,month_rx,month_tx,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                    (ACTIVE_INTERFACE, row['last_rx'], row['last_tx'], row['total_rx'], row['total_tx'],
                     row['reset_rx'], row['reset_tx'], row['month_key'], row['month_rx'], row['month_tx'], row['updated_at']))
            old_months = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='monthly'").fetchone()
            if old_months:
                for m in con.execute('SELECT * FROM monthly').fetchall():
                    con.execute('INSERT OR IGNORE INTO usage_monthly(interface,month_key,rx_bytes,tx_bytes) VALUES(?,?,?,?)',
                                (ACTIVE_INTERFACE, m['month_key'], m['rx_bytes'], m['tx_bytes']))
        for key, value in [('interface', ACTIVE_INTERFACE), ('display_name', DISPLAY_NAME)]:
            con.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', (key, value))
        ACTIVE_INTERFACE = con.execute("SELECT value FROM settings WHERE key='interface'").fetchone()['value']
        DISPLAY_NAME = con.execute("SELECT value FROM settings WHERE key='display_name'").fetchone()['value']
        ensure_interface_state(con, ACTIVE_INTERFACE)


def ensure_interface_state(con, interface):
    row = con.execute('SELECT interface FROM usage_state WHERE interface=?', (interface,)).fetchone()
    if row is None:
        try:
            rx, tx = read_interface_counters(interface)
        except Exception:
            rx, tx = 0, 0
        mk = local_now().strftime('%Y-%m')
        con.execute('''INSERT INTO usage_state(interface,last_rx,last_tx,month_key,updated_at)
                       VALUES(?,?,?,?,?)''', (interface, rx, tx, mk, local_now().isoformat()))
        con.execute('INSERT OR IGNORE INTO usage_monthly(interface,month_key) VALUES(?,?)', (interface, mk))


def sample_once():
    global RATE_RX, RATE_TX, LAST_RATE_TS
    interface = ACTIVE_INTERFACE
    try:
        rx, tx = read_interface_counters(interface)
    except Exception:
        return
    now = local_now()
    now_ts = time.monotonic()
    mk = now.strftime('%Y-%m')
    with LOCK:
        with connect() as con:
            ensure_interface_state(con, interface)
            row = con.execute('SELECT * FROM usage_state WHERE interface=?', (interface,)).fetchone()
            drx = rx - int(row['last_rx']) if rx >= int(row['last_rx']) else rx
            dtx = tx - int(row['last_tx']) if tx >= int(row['last_tx']) else tx
            elapsed = now_ts - LAST_RATE_TS if LAST_RATE_TS is not None else 0
            if elapsed > 0:
                RATE_RX = max(0, drx) / elapsed
                RATE_TX = max(0, dtx) / elapsed
            LAST_RATE_TS = now_ts
            if row['month_key'] != mk:
                month_rx = month_tx = 0
            else:
                month_rx, month_tx = int(row['month_rx']), int(row['month_tx'])
            month_rx += max(0, drx)
            month_tx += max(0, dtx)
            con.execute('''UPDATE usage_state SET last_rx=?,last_tx=?,total_rx=total_rx+?,total_tx=total_tx+?,
                month_key=?,month_rx=?,month_tx=?,updated_at=? WHERE interface=?''',
                (rx, tx, max(0, drx), max(0, dtx), mk, month_rx, month_tx, now.isoformat(), interface))
            con.execute('''INSERT INTO usage_monthly(interface,month_key,rx_bytes,tx_bytes) VALUES(?,?,?,?)
                ON CONFLICT(interface,month_key) DO UPDATE SET rx_bytes=rx_bytes+excluded.rx_bytes,
                tx_bytes=tx_bytes+excluded.tx_bytes''', (interface, mk, max(0, drx), max(0, dtx)))


def sampler():
    while True:
        sample_once()
        time.sleep(POLL_SECONDS)


@APP.get('/')
def index():
    return render_template('index.html', interface=ACTIVE_INTERFACE, display_name=DISPLAY_NAME)


@APP.get('/api/interfaces')
def interfaces():
    return jsonify({'interfaces': available_interfaces(), 'selected': ACTIVE_INTERFACE})


@APP.post('/api/settings')
def save_settings():
    global ACTIVE_INTERFACE, DISPLAY_NAME, RATE_RX, RATE_TX, LAST_RATE_TS
    data = request.get_json(silent=True) or {}
    new_interface = str(data.get('interface', ACTIVE_INTERFACE)).strip()
    new_name = str(data.get('display_name', DISPLAY_NAME)).strip()[:48] or 'Internet Usage'
    if new_interface not in available_interfaces():
        return jsonify({'error': 'Selected network interface is not available'}), 400
    with LOCK:
        # Sample the old interface before switching so its accumulated totals are retained.
        sample_once()
        with connect() as con:
            ensure_interface_state(con, new_interface)
            con.execute("INSERT INTO settings(key,value) VALUES('interface',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (new_interface,))
            con.execute("INSERT INTO settings(key,value) VALUES('display_name',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (new_name,))
        ACTIVE_INTERFACE = new_interface
        DISPLAY_NAME = new_name
        RATE_RX = RATE_TX = 0.0
        LAST_RATE_TS = None
    return jsonify({'ok': True, 'interface': ACTIVE_INTERFACE, 'display_name': DISPLAY_NAME})


@APP.get('/api/status')
def status():
    interface = ACTIVE_INTERFACE
    with LOCK:
        with connect() as con:
            row = con.execute('SELECT * FROM usage_state WHERE interface=?', (interface,)).fetchone()
            months = con.execute('SELECT month_key,rx_bytes,tx_bytes FROM usage_monthly WHERE interface=? ORDER BY month_key DESC LIMIT 12', (interface,)).fetchall()
    if row is None:
        return jsonify({'error': 'Usage database is not initialized'}), 500
    month_key = local_now().strftime('%Y-%m')
    month_rx, month_tx = (int(row['month_rx']), int(row['month_tx'])) if row['month_key'] == month_key else (0, 0)
    return jsonify({'interface': interface, 'display_name': DISPLAY_NAME,
        'download_bps': RATE_RX, 'upload_bps': RATE_TX,
        'download_since_reset': max(0, int(row['total_rx']) - int(row['reset_rx'])),
        'upload_since_reset': max(0, int(row['total_tx']) - int(row['reset_tx'])),
        'download_total': int(row['total_rx']), 'upload_total': int(row['total_tx']),
        'download_month': month_rx, 'upload_month': month_tx, 'month': month_key,
        'updated_at': row['updated_at'], 'monthly_history': [dict(m) for m in months]})


@APP.post('/api/reset/<direction>')
def reset(direction):
    if direction not in ('download', 'upload', 'both'):
        return jsonify({'error': 'Unknown reset direction'}), 400
    sample_once()
    with LOCK:
        with connect() as con:
            row = con.execute('SELECT total_rx,total_tx FROM usage_state WHERE interface=?', (ACTIVE_INTERFACE,)).fetchone()
            if direction in ('download', 'both'):
                con.execute('UPDATE usage_state SET reset_rx=? WHERE interface=?', (int(row['total_rx']), ACTIVE_INTERFACE))
            if direction in ('upload', 'both'):
                con.execute('UPDATE usage_state SET reset_tx=? WHERE interface=?', (int(row['total_tx']), ACTIVE_INTERFACE))
    return jsonify({'ok': True, 'reset': direction})


@APP.get('/health')
def health():
    try:
        read_interface_counters()
        return jsonify({'ok': True, 'interface': ACTIVE_INTERFACE})
    except Exception as e:
        return jsonify({'ok': False, 'interface': ACTIVE_INTERFACE, 'error': str(e)}), 503


if __name__ == '__main__':
    init_db()
    threading.Thread(target=sampler, daemon=True).start()
    APP.run(host='0.0.0.0', port=8787, threaded=True)
