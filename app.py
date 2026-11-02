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

DEFAULT_APPEARANCE = {
    'primary_color': '#34d399', 'accent_color': '#60a5fa',
    'background_color': '#0b1220', 'card_color': '#172438',
    'text_color': '#e5edf7', 'title_alignment': 'center',
    'logo_data': '', 'logo_width': '120',
}
COLOR_KEYS = {'primary_color', 'accent_color', 'background_color', 'card_color', 'text_color'}


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
            month_tx INTEGER NOT NULL DEFAULT 0, updated_at TEXT, reset_at TEXT)''')
        con.execute('''CREATE TABLE IF NOT EXISTS usage_samples (
            interface TEXT NOT NULL, timestamp REAL NOT NULL,
            rx_bytes INTEGER NOT NULL DEFAULT 0, tx_bytes INTEGER NOT NULL DEFAULT 0)''')
        con.execute('CREATE INDEX IF NOT EXISTS idx_usage_samples_interface_time ON usage_samples(interface,timestamp)')
        con.execute('''CREATE TABLE IF NOT EXISTS usage_monthly (
            interface TEXT NOT NULL, month_key TEXT NOT NULL,
            rx_bytes INTEGER NOT NULL DEFAULT 0, tx_bytes INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(interface, month_key))''')
        columns = {r['name'] for r in con.execute('PRAGMA table_info(usage_state)').fetchall()}
        if 'reset_at' not in columns:
            con.execute('ALTER TABLE usage_state ADD COLUMN reset_at TEXT')
        con.execute('UPDATE usage_state SET reset_at=COALESCE(reset_at, updated_at, ?)', (local_now().isoformat(),))
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
        for key, value in [('interface', ACTIVE_INTERFACE), ('display_name', DISPLAY_NAME), *DEFAULT_APPEARANCE.items()]:
            con.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', (key, str(value)))
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
        con.execute('''INSERT INTO usage_state(interface,last_rx,last_tx,month_key,updated_at,reset_at)
                       VALUES(?,?,?,?,?,?)''', (interface, rx, tx, mk, local_now().isoformat(), local_now().isoformat()))
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
            con.execute('INSERT INTO usage_samples(interface,timestamp,rx_bytes,tx_bytes) VALUES(?,?,?,?)',
                        (interface, now.timestamp(), max(0, drx), max(0, dtx)))
            con.execute('DELETE FROM usage_samples WHERE interface=? AND timestamp<?',
                        (interface, now.timestamp() - 7 * 86400))


def sampler():
    while True:
        sample_once()
        time.sleep(POLL_SECONDS)


@APP.get('/')
def index():
    return render_template('index.html', interface=ACTIVE_INTERFACE, display_name=DISPLAY_NAME)


def read_appearance(con):
    values = {r['key']: r['value'] for r in con.execute('SELECT key,value FROM settings').fetchall()}
    result = dict(DEFAULT_APPEARANCE)
    result.update({k: values[k] for k in result if k in values})
    return result


@APP.get('/api/interfaces')
def interfaces():
    with connect() as con: appearance = read_appearance(con)
    return jsonify({'interfaces': available_interfaces(), 'selected': ACTIVE_INTERFACE, 'display_name': DISPLAY_NAME, 'appearance': appearance})


@APP.post('/api/settings')
def save_settings():
    global ACTIVE_INTERFACE, DISPLAY_NAME, RATE_RX, RATE_TX, LAST_RATE_TS
    data = request.get_json(silent=True) or {}
    new_interface = str(data.get('interface', ACTIVE_INTERFACE)).strip()
    new_name = str(data.get('display_name', DISPLAY_NAME)).strip()[:48] or 'Internet Usage'
    appearance = {}
    for key in COLOR_KEYS:
        if key in data:
            value = str(data[key]).strip()
            if len(value) != 7 or not value.startswith('#') or any(c not in '0123456789abcdefABCDEF' for c in value[1:]):
                return jsonify({'error': f'Invalid color value for {key}'}), 400
            appearance[key] = value
    if 'title_alignment' in data:
        alignment = str(data['title_alignment'])
        if alignment not in ('left', 'center', 'right'):
            return jsonify({'error': 'Invalid title alignment'}), 400
        appearance['title_alignment'] = alignment
    if 'logo_width' in data:
        try: width = max(40, min(320, int(data['logo_width'])))
        except (TypeError, ValueError): return jsonify({'error': 'Invalid logo width'}), 400
        appearance['logo_width'] = str(width)
    if 'logo_data' in data:
        logo = str(data['logo_data'] or '')
        allowed = ('data:image/png;base64,', 'data:image/jpeg;base64,', 'data:image/webp;base64,', 'data:image/svg+xml;base64,')
        if logo and (not logo.startswith(allowed) or len(logo) > 700000):
            return jsonify({'error': 'Logo must be PNG, JPG, WEBP or SVG and under 500 KB'}), 400
        appearance['logo_data'] = logo
    if new_interface not in available_interfaces():
        return jsonify({'error': 'Selected network interface is not available'}), 400
    with LOCK:
        # Sample the old interface before switching so its accumulated totals are retained.
        sample_once()
        with connect() as con:
            ensure_interface_state(con, new_interface)
            con.execute("INSERT INTO settings(key,value) VALUES('interface',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (new_interface,))
            con.execute("INSERT INTO settings(key,value) VALUES('display_name',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (new_name,))
            for key, value in appearance.items():
                con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        ACTIVE_INTERFACE = new_interface
        DISPLAY_NAME = new_name
        RATE_RX = RATE_TX = 0.0
        LAST_RATE_TS = None
    with connect() as con: saved_appearance = read_appearance(con)
    return jsonify({'ok': True, 'interface': ACTIVE_INTERFACE, 'display_name': DISPLAY_NAME, 'appearance': saved_appearance})


@APP.get('/api/status')
def status():
    interface = ACTIVE_INTERFACE
    with LOCK:
        with connect() as con:
            row = con.execute('SELECT * FROM usage_state WHERE interface=?', (interface,)).fetchone()
            months = con.execute('SELECT month_key,rx_bytes,tx_bytes FROM usage_monthly WHERE interface=? ORDER BY month_key DESC LIMIT 12', (interface,)).fetchall()
    if row is None:
        return jsonify({'error': 'Usage database is not initialized'}), 500
    reset_at = row['reset_at'] or row['updated_at']
    try:
        elapsed_since_reset = max(1.0, (local_now() - datetime.fromisoformat(reset_at)).total_seconds())
    except Exception:
        elapsed_since_reset = 1.0
    download_since_reset = max(0, int(row['total_rx']) - int(row['reset_rx']))
    upload_since_reset = max(0, int(row['total_tx']) - int(row['reset_tx']))
    return jsonify({'interface': interface, 'display_name': DISPLAY_NAME,
        'download_bps': RATE_RX, 'upload_bps': RATE_TX,
        'average_download_bps': download_since_reset / elapsed_since_reset,
        'average_upload_bps': upload_since_reset / elapsed_since_reset,
        'download_since_reset': download_since_reset, 'upload_since_reset': upload_since_reset,
        'reset_at': reset_at, 'updated_at': row['updated_at']})


@APP.get('/api/usage')
def usage_history():
    try:
        seconds = int(request.args.get('seconds', '0'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid time range'}), 400
    if seconds not in (0, 300, 600, 1800, 3600, 10800, 21600, 43200, 86400):
        return jsonify({'error': 'Unsupported time range'}), 400
    now = local_now().timestamp()
    interface = ACTIVE_INTERFACE
    with LOCK:
        with connect() as con:
            state = con.execute('SELECT reset_at FROM usage_state WHERE interface=?', (interface,)).fetchone()
            if state is None:
                return jsonify({'error': 'Usage database is not initialized'}), 500
            start = datetime.fromisoformat(state['reset_at']).timestamp() if seconds == 0 else now - seconds
            rows = con.execute('''SELECT timestamp,rx_bytes,tx_bytes FROM usage_samples
                WHERE interface=? AND timestamp>=? ORDER BY timestamp''', (interface, start)).fetchall()
    return jsonify({'interface': interface, 'start': start, 'end': now,
        'download_bytes': sum(int(x['rx_bytes']) for x in rows),
        'upload_bytes': sum(int(x['tx_bytes']) for x in rows),
        'samples': [{'time': float(x['timestamp']), 'download': int(x['rx_bytes']),
                     'upload': int(x['tx_bytes'])} for x in rows]})


@APP.post('/api/reset')
def reset():
    sample_once()
    reset_time = local_now().isoformat()
    with LOCK:
        with connect() as con:
            row = con.execute('SELECT total_rx,total_tx FROM usage_state WHERE interface=?', (ACTIVE_INTERFACE,)).fetchone()
            if row is None:
                return jsonify({'error': 'Usage database is not initialized'}), 500
            con.execute('''UPDATE usage_state SET reset_rx=?, reset_tx=?, reset_at=? WHERE interface=?''',
                        (int(row['total_rx']), int(row['total_tx']), reset_time, ACTIVE_INTERFACE))
    return jsonify({'ok': True, 'reset_at': reset_time})

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
