# FallCore

Lekki, samodzielnie hostowany pulpit monitorujący zbudowany w języku Python i Flask, zaprojektowany z myślą o uruchomieniu lokalnie na ekranie kiosk Raspberry Pi oraz zapewnieniu wygodnego dostępu z urządzeń mobilnych w tej samej sieci.

## Główne funkcje

- **Śledzenie statusu w czasie rzeczywistym:** Automatycznie monitoruje dostępność (uptime), czas odpowiedzi oraz strony statusu skonfigurowanych stron i serwisów.
- **Praca w tle:** Przeprowadza okresowe testy zdrowia w osobnym wątku w tle, nie blokując głównej aplikacji internetowej.
- **Interfejs w trybie Kiosk:** Zoptymalizowany pod stałe ekrany naścienne lub biurkowe przy użyciu Raspberry Pi, trybu kiosk przeglądarki Chromium oraz narzędzia automatycznie ukrywającego kursor (`unclutter`).
- **Przyjazny dla urządzeń mobilnych:** Łatwo dostępny ze smartfonów za pośrednictwem lokalnego adresu IP sieci w celu dodawania lub zarządzania monitorowanymi serwisami „w biegu”.
- **Dynamiczne wyświetlanie IP:** Automatycznie pobiera i prezentuje lokalny adres IP malinki bezpośrednio w interfejsie, umożliwiając szybki dostęp z telefonu.

---

## Stos technologiczny (Tech Stack)

### **Backend i rdzeń**
- **Python 3:** Główny język programowania[cite: 3].
- **Flask:** Lekki framework internetowy WSGI obsługujący routing i renderowanie pulpitu[cite: 3].
- **SQLite:** Wbudowana, plikowa baza danych SQL (`database.db`) służąca do przechowywania serwisów i historii sprawdzeń[cite: 3].
- **Threading:** Wbudowany w Pythona moduł `threading` do asynchronicznego sprawdzania statusów w tle[cite: 3].

### **Frontend i interfejs**
- **HTML5 i CSS3:** Czysty, responsywny interfejs budowany za pomocą szablonów Flask/Jinja2[cite: 3].
- **Responsywny układ:** Dostosowany zarówno do dużych ekranów pulpitowych/kioskowych, jak i ekranów urządzeń mobilnych.

### **Sprzęt i środowisko**
- **Raspberry Pi OS (Bookworm):** System operacyjny działający na Raspberry Pi.
- **Przeglądarka Chromium:** Skonfigurowana w trybie `--kiosk` z wyłączonymi oknami błędów i podstawowym magazynem haseł.
- **Systemd:** Zarządza aplikacją Flask jako usługą systemową działającą w tle (`statusmonitor.service`), zapewniając wysoką dostępność i autostart po uruchomieniu.
- **X11 & Unclutter:** Konfiguracja serwera okien zapewniająca automatyczne ukrywanie kursora myszy po okresie bezczynności.

---

## Struktura projektu

```text
FallCore/
│
├── status_board/
│   ├── templates/
│   │   └── index.html      # Widok pulpitu frontendowego
│   ├── app.py              # Główna aplikacja Flask i trasy[cite: 3]
│   ├── monitor.py          # Logika sprawdzania statusu w tle[cite: 3]
│   └── database.db         # Baza danych SQLite przechowująca serwisy[cite: 3]
│
├── venv/                   # Wirtualne środowisko Pythona
└── README.md