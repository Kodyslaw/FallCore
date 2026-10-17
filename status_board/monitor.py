from datetime import datetime
import time
import requests
import sqlite3

DB_NAME = "database.db"

def check_service(service_id, url, service_type):
    """Sprawdza pojedynczy serwis (HTTP lub Statuspage) i zapisuje wynik do bazy"""
    start_time = time.time()
    status = "DOWN"
    response_time = 0
    
    try:
        if service_type == "statuspage":
            # Statuspage API zwraca zazwyczaj JSON z polem status.indicator
            response = requests.get(url, timeout=5)
            response_time = round((time.time() - start_time) * 1000)
            if response.status_code == 200:
                data = response.json()
                indicator = data.get("status", {}).get("indicator", "unknown")
                if indicator == "none":
                    status = "UP"
                else:
                    status = f"Degraded ({indicator})"
            else:
                status = "DOWN"
        else:
            # Standardowy test kodu odpowiedzi HTTP
            response = requests.get(url, timeout=5)
            response_time = round((time.time() - start_time) * 1000)
            if 200 <= response.status_code < 400:
                status = "UP"
            else:
                status = f"HTTP {response.status_code}"
                
    except requests.RequestException:
        status = "DOWN"
        response_time = 0

    # Pobranie aktualnego czasu w czytelnym formacie
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Aktualizacja rekordu w bazie danych
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE services 
        SET status = ?, response_time = ?, last_checked = ?
        WHERE id = ?
    """, (status, response_time, now, service_id))
    conn.commit()
    conn.close()

def run_checks():
    """Pobiera wszystkie serwisy z bazy i uruchamia dla nich testy"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    services = cursor.execute("SELECT id, url, type FROM services").fetchall()
    conn.close()
    
    for service in services:
        check_service(service["id"], service["url"], service["type"])