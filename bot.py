import io
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from google import genai
from google.genai import types
from gtts import gTTS
from neonize.client import NewClient
from neonize.events import ConnectedEv, MessageEv
from PIL import Image


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sigma_bot")


GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

if not GEMINI_API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY belum diatur. Salin .env.example menjadi .env "
        "atau export GEMINI_API_KEY terlebih dahulu."
    )


ai_client = genai.Client(api_key=GEMINI_API_KEY)
client = NewClient("sesi_wa_qwen.sqlite3")

SYSTEM_PROMPT = (
    "Kamu adalah SIGMA BOT, asisten AI berbahasa Indonesia dan yang bisa berbahasa apapun, santai, "
    "ringkas, akurat, dan jelas.\n"
    "ATURAN FORMAT WHATSAPP (WAJIB):\n"
    "1. JANGAN PERNAH menggunakan format LaTeX atau tanda dolar seperti $, $$, \\frac, \\times, \\sqrt, atau simbol backslash lainnya.\n"
    "2. Tulis semua rumus dan perhitungan matematika dengan teks biasa yang mudah dibaca di WhatsApp "
    "(contoh: tulis '12! / (7! × 5!)' atau '(12 × 11 × 10 × 9 × 8) / 120 = 792', gunakan simbol ×, ÷, =, √, ^ atau pangkat biasa).\n"
    "3. Gunakan format WhatsApp asli: satu tanda bintang (*teks*) untuk huruf tebal, bukan dua tanda bintang (**teks**), dan hindari header ### atau garis ---.\n"
    "4. Jika ada yang bertanya Siapakah penciptamu? jawablah Muhammad Sabilillah Ramadhan. ingat jika ada yang menanyakan nya saja!"
)

MENU_TEXT = """*SIGMA BOT*

*Daftar Perintah:*
- `.menu` — Menampilkan daftar perintah
- `.ai <pertanyaan>` — Tanya AI (teks, gambar, video, PDF, atau reply VN)
- `.dl <link>` — Download video TikTok, IG Reels, atau YT Shorts
- `.play` / `.ytmp3 <judul/link>` — Cari & download lagu MP3 dari YouTube
- `.tts <teks>` — Ubah teks menjadi Voice Note (VN)
- `.stiker` / `.s` — Ubah gambar atau video menjadi stiker
- `.gif` — Ubah video menjadi GIF tanpa suara
- `.mp3` — Ekstrak audio MP3 dari video yang di-reply
- `.bkp` — video bkp pirall
- *Kirim video langsung* — Otomatis ekstrak menjadi file audio (MP3)"""


class MediaProcessError(Exception):
    """Kesalahan yang aman ditampilkan kepada pengguna."""


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Menjalankan perintah dan mengembalikan hasilnya."""
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
    )


def tanya_ai(
    prompt: str,
    media_bytes: bytes | None = None,
    mime_type: str | None = None,
) -> str:
    """Memakai model Gemini untuk menjawab pertanyaan teks, gambar, video, audio, atau PDF."""
    preferred_models = [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
        "gemini-3.5-flash",
    ]

    available_models: list[str] = []

    try:
        for model in ai_client.models.list():
            name = getattr(model, "name", "") or ""
            if name.startswith("models/"):
                name = name[7:]

            if "gemini" in name.lower() and name not in available_models:
                available_models.append(name)
    except Exception as error:
        logger.warning("Tidak bisa mengambil daftar model Gemini: %s", error)

    models_to_try: list[str] = []

    for model in preferred_models + available_models:
        if model not in models_to_try:
            models_to_try.append(model)

    if not models_to_try:
        return "Tidak menemukan model Gemini yang tersedia untuk API key ini."

    contents: list | str
    if media_bytes and mime_type:
        contents = [
            types.Part.from_bytes(data=media_bytes, mime_type=mime_type),
            prompt,
        ]
    else:
        contents = prompt

    last_error = ""

    for model_name in models_to_try:
        try:
            logger.info("Mencoba model Gemini: %s", model_name)
            response = ai_client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.3,
                ),
            )

            if response.text:
                return response.text.strip()

            return "Gemini tidak memberikan jawaban."

        except Exception as error:
            last_error = str(error)
            logger.warning("Model %s gagal: %s", model_name, error)

            if not any(
                code in last_error.lower()
                for code in ("404", "429", "503", "timeout", "not found")
            ):
                break

    return f"Gagal terhubung ke Gemini API: {last_error}"


def download_video_sosmed(url: str) -> bytes:
    """Mengunduh video TikTok/IG/Shorts dan mengonversinya hingga batas maksimal WA (63 MB)."""
    max_wa_bytes = 63 * 1024 * 1024

    with tempfile.TemporaryDirectory() as tmp_dir:
        raw_template = os.path.join(tmp_dir, "raw.%(ext)s")
        final_mp4 = os.path.join(tmp_dir, "final.mp4")
        compact_mp4 = os.path.join(tmp_dir, "compact.mp4")

        result = run_command(
            [
                "yt-dlp",
                "--no-playlist",
                "--max-filesize",
                "200M",
                "-f",
                "bv*+ba/b",
                "--merge-output-format",
                "mp4",
                "-o",
                raw_template,
                url,
            ]
        )

        files = list(Path(tmp_dir).glob("raw.*"))
        if result.returncode != 0 or not files:
            err_msg = (result.stderr or result.stdout or "").strip()
            if "File is larger than max-filesize" in err_msg:
                raise MediaProcessError("Ukuran video mentah melebihi batas 200 MB.")
            raise MediaProcessError(
                "Gagal mengunduh video. Pastikan link valid dan bersifat publik."
            )

        raw_file = str(files[0])

        conv = run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                raw_file,
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-vf",
                "scale='min(720,iw)':-2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "25",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                final_mp4,
            ]
        )

        if conv.returncode != 0 or not os.path.exists(final_mp4):
            raise MediaProcessError(
                "Link ini tidak memiliki track video (kemungkinan postingan slide foto/audio saja)."
            )

        data = Path(final_mp4).read_bytes()
        if len(data) <= max_wa_bytes:
            return data

        conv_compact = run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                raw_file,
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-vf",
                "scale='min(480,iw)':-2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "30",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                compact_mp4,
            ]
        )

        if conv_compact.returncode == 0 and os.path.exists(compact_mp4):
            compact_data = Path(compact_mp4).read_bytes()
            if len(compact_data) <= max_wa_bytes:
                return compact_data

        raise MediaProcessError(
            "Durasi/ukuran video terlalu besar untuk batas maksimal WhatsApp (63 MB)."
        )


def download_audio_youtube(query_or_url: str) -> bytes:
    """Mencari judul lagu atau mengunduh audio dari link YouTube menjadi MP3."""
    target = (
        query_or_url
        if query_or_url.startswith(("http://", "https://"))
        else f"ytsearch1:{query_or_url}"
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        output_template = os.path.join(tmp_dir, "lagu.%(ext)s")

        result = run_command(
            [
                "yt-dlp",
                "--no-playlist",
                "--max-filesize",
                "60M",
                "-x",
                "--audio-format",
                "mp3",
                "--audio-quality",
                "2",
                "-o",
                output_template,
                target,
            ]
        )

        mp3_files = list(Path(tmp_dir).glob("lagu*.mp3"))
        if result.returncode != 0 or not mp3_files:
            raise MediaProcessError(
                "Gagal mengunduh lagu. Coba kata kunci atau link YouTube lain."
            )

        mp3_file = mp3_files[0]
        if mp3_file.stat().st_size > 60 * 1024 * 1024:
            raise MediaProcessError("Ukuran file audio melebihi batas 60 MB.")

        return mp3_file.read_bytes()

def teks_ke_vn_mp3(text: str, lang: str = "id") -> bytes:
    """Mengubah teks menjadi MP3 dengan tag ID3 agar terbaca neonize sebagai Voice Note."""
    temporary_files: list[str] = []

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            raw_mp3 = f.name

        final_mp3 = f"{raw_mp3}_vn.mp3"
        temporary_files.extend([raw_mp3, final_mp3])

        tts = gTTS(text=text, lang=lang, slow=False)
        tts.save(raw_mp3)

        result = run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                raw_mp3,
                "-c:a",
                "libmp3lame",
                "-b:a",
                "64k",
                "-ar",
                "24000",
                "-ac",
                "1",
                "-id3v2_version",
                "3",
                "-metadata",
                "title=SIGMA BOT",
                final_mp3,
            ]
        )

        if result.returncode != 0 or not os.path.exists(final_mp3):
            raise MediaProcessError(f"Gagal memproses audio TTS: {result.stderr}")

        return Path(final_mp3).read_bytes()

    finally:
        for path in temporary_files:
            try:
                os.remove(path)
            except OSError:
                pass

def gambar_ke_webp(raw_bytes: bytes) -> bytes:
    try:
        image = Image.open(io.BytesIO(raw_bytes)).convert("RGBA")
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)

        canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        position = ((512 - image.width) // 2, (512 - image.height) // 2)
        canvas.paste(image, position, image)

        output = io.BytesIO()
        canvas.save(output, format="WEBP", quality=90)
        return output.getvalue()

    except Exception as error:
        raise MediaProcessError(f"Gagal memproses gambar: {error}") from error


def render_animated_webp(
    video_path: str,
    output_path: str,
    max_duration: int = 6,
) -> bytes:
    result = run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            video_path,
            "-t",
            str(max_duration),
            "-map",
            "0:v:0",
            "-an",
            "-vcodec",
            "libwebp",
            "-vf",
            (
                "fps=12,scale=320:320:force_original_aspect_ratio=decrease,"
                "pad=320:320:(ow-iw)/2:(oh-ih)/2:color=0x00000000"
            ),
            "-lossless",
            "0",
            "-compression_level",
            "6",
            "-q:v",
            "40",
            "-loop",
            "0",
            "-preset",
            "default",
            output_path,
        ]
    )

    if result.returncode != 0 or not os.path.exists(output_path):
        logger.error("FFmpeg WebP error: %s", result.stderr)
        raise MediaProcessError("FFmpeg gagal mengubah video menjadi stiker.")

    data = Path(output_path).read_bytes()

    if len(data) > 499 * 1024:
        raise MediaProcessError(
            "Ukuran stiker melebihi batas maksimal WhatsApp (500 KB). Gunakan video lebih pendek."
        )

    return data


def video_ke_webp_animasi(raw_bytes: bytes) -> bytes:
    temporary_files: list[str] = []

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as file:
            file.write(raw_bytes)
            input_path = file.name

        output_path = f"{input_path}.webp"
        temporary_files.extend([input_path, output_path])
        return render_animated_webp(input_path, output_path, 6)

    finally:
        for path in temporary_files:
            try:
                os.remove(path)
            except OSError:
                pass


def video_ke_gif_wa(raw_bytes: bytes) -> bytes:
    temporary_files: list[str] = []

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as file:
            file.write(raw_bytes)
            input_path = file.name

        output_path = f"{input_path}_gif.mp4"
        temporary_files.extend([input_path, output_path])

        result = run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                input_path,
                "-t",
                "8",
                "-an",
                "-vf",
                (
                    "fps=15,scale=480:480:force_original_aspect_ratio=decrease,"
                    "pad=480:480:(ow-iw)/2:(oh-ih)/2:color=black"
                ),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                output_path,
            ]
        )

        if result.returncode != 0:
            raise MediaProcessError(f"FFmpeg GIF gagal: {result.stderr}")

        return Path(output_path).read_bytes()

    finally:
        for path in temporary_files:
            try:
                os.remove(path)
            except OSError:
                pass


def video_ke_mp3(raw_bytes: bytes) -> bytes:
    """Mengekstrak seluruh audio dari video ke format MP3 kualitas tinggi."""
    temporary_files: list[str] = []

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as file:
            file.write(raw_bytes)
            input_path = file.name

        output_path = f"{input_path}.mp3"
        temporary_files.extend([input_path, output_path])

        result = run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                input_path,
                "-vn",
                "-acodec",
                "libmp3lame",
                "-q:a",
                "2",
                output_path,
            ]
        )

        if result.returncode != 0 or not os.path.exists(output_path):
            raise MediaProcessError("Video ini tidak memiliki audio atau gagal diekstrak.")

        return Path(output_path).read_bytes()

    finally:
        for path in temporary_files:
            try:
                os.remove(path)
            except OSError:
                pass


def kirim_audio_bytes(
    c: NewClient,
    chat,
    audio_bytes: bytes,
    suffix: str,
    ptt: bool,
    event: MessageEv,
) -> None:
    audio_tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio_bytes)
            audio_tmp = f.name

        c.send_audio(chat, audio_tmp, ptt=ptt, quoted=event)
    finally:
        if audio_tmp and os.path.exists(audio_tmp):
            try:
                os.remove(audio_tmp)
            except OSError:
                pass


def ambil_target_media(message):
    if (
        message.HasField("extendedTextMessage")
        and message.extendedTextMessage.contextInfo.HasField("quotedMessage")
    ):
        return message.extendedTextMessage.contextInfo.quotedMessage
    return message


@client.event(ConnectedEv)
def on_connected(c: NewClient, _: ConnectedEv):
    try:
        c.set_profile_name("SIGMA BOT")
    except Exception:
        pass
    logger.info("SIGMA BOT berhasil terhubung dan siap menerima perintah.")


@client.event(MessageEv)
def on_message(c: NewClient, event: MessageEv):
    threading.Thread(
        target=handle_message,
        args=(c, event),
        daemon=True,
    ).start()


def handle_message(c: NewClient, event: MessageEv):
    message = event.Message
    chat = event.Info.MessageSource.Chat

    text = (
        message.conversation
        or message.extendedTextMessage.text
        or message.imageMessage.caption
        or message.videoMessage.caption
        or message.documentMessage.caption
        or ""
    ).strip()

    # Otomatis ubah video menjadi audio MP3 jika dikirim tanpa caption
    if not text:
        if message.HasField("videoMessage"):
            try:
                mp3_bytes = video_ke_mp3(c.download_any(message))
                kirim_audio_bytes(c, chat, mp3_bytes, ".mp3", False, event)
            except Exception as error:
                logger.exception("Gagal mengekstrak audio video")
                c.reply_message(f"Gagal mengekstrak suara: {error}", event)
        return

    command = text.lower()

    if command in (".menu", "!menu", "menu"):
        c.reply_message(MENU_TEXT, event)
        return

    if command == ".bkp" or command.startswith((".bkp ", "!bkp ", ".bokep")):
        vid_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prank.mp4")
        try:
            if os.path.exists(vid_path):
                c.send_video(
                    chat,
                    vid_path,
                    caption="bkp pirall 4dtk",
                    quoted=event,
                )
            else:
                c.reply_message("File prank.mp4 belum ada di folder bot!", event)
        except Exception as e:
            c.reply_message(f"Gagal kirim video prank: {e}", event)
        return

    if command in (".dl", "!dl") or command.startswith((".dl ", "!dl ")):
        url = text[3:].strip()
        if not url.startswith(("http://", "https://")):
            c.reply_message(
                "Contoh: `.dl https://vt.tiktok.com/...` atau link IG Reels / YT Shorts.",
                event,
            )
            return

        try:
            c.reply_message("Sedang mengunduh dan memproses video...", event)
            video_bytes = download_video_sosmed(url)
            c.send_video(chat, video_bytes, gifplayback=False, quoted=event)
        except Exception as error:
            logger.exception("Gagal download video sosmed")
            c.reply_message(f"{error}", event)
        return

    if command in (".play", "!play", ".ytmp3", "!ytmp3") or command.startswith(
        (".play ", "!play ", ".ytmp3 ", "!ytmp3 ")
    ):
        query = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
        if not query:
            c.reply_message(
                "Contoh:\n- `.play judul lagu`\n- `.ytmp3 https://youtu.be/...`",
                event,
            )
            return

        try:
            c.reply_message("Sedang mencari dan mengunduh lagu...", event)
            mp3_bytes = download_audio_youtube(query)
            kirim_audio_bytes(c, chat, mp3_bytes, ".mp3", False, event)
        except Exception as error:
            logger.exception("Gagal download audio YouTube")
            c.reply_message(f"{error}", event)
        return

    if command in (".tts", "!tts") or command.startswith((".tts ", "!tts ")):
        tts_text = text[4:].strip()
        if not tts_text:
            c.reply_message("Contoh: `.tts halo bro apa kabar`", event)
            return

        if len(tts_text) > 500:
            c.reply_message("Teks TTS terlalu panjang (maksimal 500 karakter).", event)
            return

        try:
            vn_bytes = teks_ke_vn_mp3(tts_text, lang="id")
            kirim_audio_bytes(c, chat, vn_bytes, ".mp3", True, event)
        except Exception as error:
            logger.exception("Gagal membuat VN TTS")
            c.reply_message(f"Gagal membuat Voice Note: {error}", event)

    if command in (".ai", "!ai") or command.startswith((".ai ", "!ai ")):
        question = text[3:].strip()
        target = ambil_target_media(message)

        try:
            if target.HasField("imageMessage"):
                media_bytes = c.download_any(target)
                if len(media_bytes) > 20 * 1024 * 1024:
                    c.reply_message("Ukuran gambar melebihi batas maksimal 20 MB.", event)
                    return
                mime = target.imageMessage.mimetype or "image/jpeg"
                prompt = question or "Jelaskan secara ringkas dan jelas apa yang ada di gambar ini."
                c.reply_message(tanya_ai(prompt, media_bytes=media_bytes, mime_type=mime), event)
                return

            if target.HasField("videoMessage"):
                media_bytes = c.download_any(target)
                if len(media_bytes) > 20 * 1024 * 1024:
                    c.reply_message("Ukuran video melebihi batas maksimal Gemini API (20 MB).", event)
                    return
                mime = target.videoMessage.mimetype or "video/mp4"
                prompt = question or "Jelaskan isi, kejadian, dan suara yang ada di video ini secara ringkas."
                c.reply_message(tanya_ai(prompt, media_bytes=media_bytes, mime_type=mime), event)
                return

            if target.HasField("audioMessage"):
                media_bytes = c.download_any(target)
                if len(media_bytes) > 20 * 1024 * 1024:
                    c.reply_message("Ukuran audio melebihi batas maksimal Gemini API (20 MB).", event)
                    return
                mime = target.audioMessage.mimetype or "audio/ogg"
                prompt = question or (
                    "Tuliskan transkrip isi pesan suara ini dengan rapi, lalu buat ringkasan "
                    "poin pentingnya secara singkat dan jelas."
                )
                c.reply_message(tanya_ai(prompt, media_bytes=media_bytes, mime_type=mime), event)
                return

            if target.HasField("documentMessage"):
                mime = target.documentMessage.mimetype or ""
                file_name = (target.documentMessage.fileName or "").lower()
                if "pdf" not in mime.lower() and not file_name.endswith(".pdf"):
                    c.reply_message("Saat ini perintah `.ai` hanya mendukung dokumen berformat PDF.", event)
                    return

                media_bytes = c.download_any(target)
                if len(media_bytes) > 20 * 1024 * 1024:
                    c.reply_message("Ukuran file PDF melebihi batas maksimal Gemini API (20 MB).", event)
                    return

                prompt = question or (
                    "Rangkum isi dokumen PDF ini secara terstruktur, jelas, dan mudah dipahami."
                )
                c.reply_message(
                    tanya_ai(prompt, media_bytes=media_bytes, mime_type="application/pdf"),
                    event,
                )
                return

            if not question:
                c.reply_message(
                    "Contoh:\n"
                    "- `.ai jelaskan cara kerja subnetting`\n"
                    "- Kirim/reply gambar, video, Voice Note, atau file PDF dengan `.ai`",
                    event,
                )
                return

            c.reply_message(tanya_ai(question), event)

        except Exception as error:
            logger.exception("Gagal memproses perintah .ai")
            c.reply_message(f"Gagal memproses permintaan AI: {error}", event)
        return

    if command in (".stiker", ".s", "!stiker", "!s"):
        target = ambil_target_media(message)

        try:
            if target.HasField("imageMessage"):
                c.send_sticker(chat, gambar_ke_webp(c.download_any(target)), quoted=event)
            elif target.HasField("videoMessage"):
                c.send_sticker(chat, video_ke_webp_animasi(c.download_any(target)), quoted=event)
            else:
                c.reply_message("Kirim atau reply gambar/video dengan `.stiker`.", event)
        except Exception as error:
            logger.exception("Gagal membuat stiker")
            c.reply_message(f"Gagal membuat stiker: {error}", event)
        return

    if command in (".gif", "!gif"):
        target = ambil_target_media(message)
        try:
            if target.HasField("videoMessage"):
                c.send_video(
                    chat,
                    video_ke_gif_wa(c.download_any(target)),
                    gifplayback=True,
                    quoted=event,
                )
            else:
                c.reply_message("Kirim atau reply video dengan caption `.gif`.", event)
        except Exception as error:
            c.reply_message(f"Gagal membuat GIF: {error}", event)
        return

    if command in (".mp3", ".audio", "!mp3", "!audio"):
        target = ambil_target_media(message)
        try:
            if target.HasField("videoMessage"):
                mp3_bytes = video_ke_mp3(c.download_any(target))
                kirim_audio_bytes(c, chat, mp3_bytes, ".mp3", False, event)
            else:
                c.reply_message("Kirim atau reply video untuk diubah menjadi audio MP3.", event)
        except Exception as error:
            logger.exception("Gagal mengekstrak audio video")
            c.reply_message(f"Gagal mengekstrak suara: {error}", event)
        return


if __name__ == "__main__":
    logger.info("Memulai SIGMA BOT...")
    client.connect()
