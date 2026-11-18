from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import os
import sqlite3
import time
import requests

basedir = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(basedir, 'database.db')


def check_service(service_id, url, service_type):
    """Sprawdzenie usługi; zapis pojedynczego wyniku do SQLite."""
    start_time = time.monotonic()
    status = 'DOWN'
    response_time = 0

    try:
        response = requests.get(url, timeout=(3, 5))
        response_time = round((time.monotonic() - start_time) * 1000)
        if service_type == 'statuspage':
            if response.status_code == 200:
                data = response.json()
                indicator = data.get('status', {}).get('indicator', 'unknown')
                status = 'UP' if indicator == 'none' else f'Degraded ({indicator})'
            else:
                status = f'HTTP {response.status_code}'
        else:
            status = 'UP' if 200 <= response.status_code < 400 else f'HTTP {response.status_code}'
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        status = 'DOWN'
        response_time = 0

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with sqlite3.connect(DB_PATH, timeout=15) as conn:
        conn.execute('PRAGMA foreign_keys = ON')
        cursor = conn.execute('''
            UPDATE services
            SET status = ?, response_time = ?, last_checked = ?
            WHERE id = ? AND url = ? AND type = ?
        ''', (status, response_time, now, service_id, url, service_type))
        if cursor.rowcount:
            health = ('up' if status == 'UP' else 'down' if status == 'DOWN' or status.startswith('HTTP ') else 'other')
            conn.execute('''
                INSERT INTO service_checks(service_id, checked_at, status, health, response_time)
                VALUES (?, ?, ?, ?, ?)
            ''', (service_id, int(time.time()), status, health, response_time))


def run_checks():
    """Testuje usługi równolegle, by pojedynczy timeout nie opóźniał pozostałych."""
    with sqlite3.connect(DB_PATH, timeout=15) as conn:
        services = conn.execute('SELECT id, url, type FROM services').fetchall()
    if not services:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(services))) as pool:
        futures = [pool.submit(check_service, *service) for service in services]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                print(f'Błąd zapisu wyniku monitoringu: {exc}')
