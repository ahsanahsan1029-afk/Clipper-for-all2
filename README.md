# Clip Maker

YouTube link paste karo -> best moments ke clips, caption ke saath.

## Kaise chalta hai
yt-dlp (download) -> faster-whisper (transcript) -> best moments choose -> ffmpeg (cut + caption burn)

## Deploy (sabse aasan: Render ya Railway)
1. Ye folder GitHub repo me daalo.
2. Render/Railway par "New Web Service" -> repo select -> "Docker" choose karo.
3. Deploy hone par mile URL ko mobile me kholo.
(Dockerfile me ffmpeg aur fonts apne aap install hote hain.)

## Local / Replit
    sudo apt install ffmpeg fonts-noto-core
    pip install -r requirements.txt
    python app.py     # http://localhost:8000

## Settings (environment variables)
- WHISPER_MODEL: tiny / base (default) / small. Hindi ke liye "small" better hai, par slow.
- MAX_MINUTES: max video length (default 60)
- COOKIES_FILE: cookies.txt ka path

## Dhyan rakho
- Cloud servers ke IP par YouTube aksar download block karta hai ("Sign in to confirm you're not a bot"). Fix: browser se cookies.txt export karke COOKIES_FILE me do, ya ghar ke PC/laptop par chalao.
- Free servers par (CPU only) 10 min ka video ~3-6 min leta hai.
- Clips 6 ghante baad apne aap delete ho jate hain.
- Sirf apne ya permission wale videos use karo.
