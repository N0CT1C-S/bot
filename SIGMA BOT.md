# SIGMA BOT

Bot WhatsApp berbasis Python dan Neonize.

## Fitur

- `.menu`
- `.ai <pertanyaan>` dengan Gemini
- `.stiker` / `.s`
- `.gif`
- `.vid`
- `.vn <teks>`

WhatsApp tidak menyimpan audio langsung di dalam stiker. Perintah `.sa` mengirim stiker dan Voice Note sebagai dua pesan terpisah.

## Instalasi

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
```

Isi `.env` dengan API key Gemini:

```env
GEMINI_API_KEY=API_KEY_GEMINI_ANDA
```

Jalankan:

```bash
set -a
source .env
set +a
python bot.py
```

Jangan pernah mengunggah `.env` atau file `*.sqlite3` ke GitHub. File `*.sqlite3` menyimpan sesi WhatsApp.

## Menjalankan 24 jam di VPS

Buat service systemd:

```bash
sudo nano /etc/systemd/system/sigma-bot.service
```

Isi, sesuaikan `User` dan path jika berbeda:

```ini
[Unit]
Description=SIGMA BOT WhatsApp
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/home/botuser/sigma-bot
EnvironmentFile=/home/botuser/sigma-bot/.env
ExecStart=/home/botuser/sigma-bot/venv/bin/python /home/botuser/sigma-bot/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Aktifkan:

```bash
sudo systemctl daemon-reload
sudo systemctl enable sigma-bot
sudo systemctl start sigma-bot
sudo systemctl status sigma-bot
```

Lihat log:

```bash
sudo journalctl -u sigma-bot -f
```
