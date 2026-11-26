import io
import logging
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
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
    "Kamu adalah SIGMA BOT, asisten AI berbahasa Indonesia yang santai, "
    "ringkas, akurat, dan jelas."
)

MENU_TEXT = """*SIGMA BOT*

*Daftar Perintah:*
- `.menu` — Menampilkan daftar perintah
- `.ai <pertanyaan>` — Tanya jawab dengan Gemini AI
- `.stiker` / `.s` — Ubah gambar atau video menjadi stiker
- `.sa` — Buat stiker dan audio terpisah dari video
- `.gif` — Ubah video menjadi GIF tanpa suara
- `.vid` — Kirim ulang video dengan suara
- `.vn <teks>` — Ubah teks menjadi Voice Note

Catatan: WhatsApp tidak menyimpan suara di dalam stiker. Perintah `.sa`
akan mengirim stiker dan Voice Note sebagai dua pesan terpisah."""


class SoundStickerError(Exception):
    """Kesalahan yang aman ditampilkan kepada pengguna."""


@dataclass
class SoundStickerResult:
    sticker_webp: bytes
    audio_ogg: bytes


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Menjalankan perintah dan mengembalikan hasilnya."""
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
    )


def tanya_ai(prompt: str) -> str:
    """Memakai model Gemini yang tersedia pada API key tersebut."""
    preferred_models = [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
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

    last_error = ""

    for model_name in models_to_try:
        try:
            logger.info("Mencoba model Gemini: %s", model_name)
            response = ai_client.models.generate_content(
                model=model_name,
                contents=prompt,
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


def has_audio_stream(media_path: str) -> bool:
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            media_path,
        ]
    )

    if result.returncode != 0:
        logger.error("ffprobe gagal: %s", result.stderr)
        return False

    return "audio" in result.stdout.lower()


def gambar_ke_webp(raw_bytes: bytes) -> bytes:
    try:
        image = Image.open(io.BytesIO(raw_bytes)).convert("RGBA")
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)

        canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        position = ((512 - image.width) // 2, (512 - image.height) // 2)
        canvas.paste(image, position, image)

        output = io.BytesIO()
        canvas.save(output, format="WEBP", quality=80)
        return output.getvalue()

    except Exception as error:
        raise SoundStickerError(f"Gagal memproses gambar: {error}") from error


def render_animated_webp(
    video_path: str,
    output_path: str,
    max_duration: int = 5,
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
                "fps=10,scale=320:320:force_original_aspect_ratio=decrease,"
                "pad=320:320:(ow-iw)/2:(oh-ih)/2:color=0x00000000"
            ),
            "-lossless",
            "0",
            "-compression_level",
            "6",
            "-q:v",
            "35",
            "-loop",
            "0",
            "-preset",
            "default",
            output_path,
        ]
    )

    if result.returncode != 0 or not os.path.exists(output_path):
        logger.error("FFmpeg WebP error: %s", result.stderr)
        raise SoundStickerError("FFmpeg gagal mengubah video menjadi stiker.")

    data = Path(output_path).read_bytes()

    if len(data) > 490 * 1024:
        raise SoundStickerError(
            "Ukuran stiker terlalu besar. Gunakan video yang lebih pendek."
        )

    return data


def render_voice_note_ogg(
    media_path: str,
    output_path: str,
    max_duration: int,
) -> bytes:
    result = run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            media_path,
            "-map",
            "0:a:0",
            "-t",
            str(max_duration),
            "-vn",
            "-ac",
            "1",
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            "-ar",
            "48000",
            "-application",
            "voip",
            "-f",
            "ogg",
            output_path,
        ]
    )

    if result.returncode != 0 or not os.path.exists(output_path):
        logger.error("FFmpeg audio error: %s", result.stderr)
        raise SoundStickerError("FFmpeg gagal mengambil suara dari media.")

    return Path(output_path).read_bytes()


def build_sound_sticker_from_video(
    video_bytes: bytes,
    max_duration: int = 5,
) -> SoundStickerResult:
    temporary_files: list[str] = []

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as file:
            file.write(video_bytes)
            input_path = file.name

        webp_path = f"{input_path}.webp"
        ogg_path = f"{input_path}.ogg"
        temporary_files.extend([input_path, webp_path, ogg_path])

        if not has_audio_stream(input_path):
            raise SoundStickerError(
                "Video ini tidak memiliki suara. Gunakan video yang memiliki audio."
            )

        return SoundStickerResult(
            sticker_webp=render_animated_webp(
                input_path, webp_path, max_duration
            ),
            audio_ogg=render_voice_note_ogg(
                input_path, ogg_path, max_duration
            ),
        )

    finally:
        for path in temporary_files:
            try:
                os.remove(path)
            except OSError:
                pass


def build_sound_sticker_from_image_and_audio(
    image_bytes: bytes,
    audio_bytes: bytes,
    max_audio_duration: int = 10,
) -> SoundStickerResult:
    temporary_files: list[str] = []

    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as file:
            file.write(audio_bytes)
            audio_path = file.name

        output_audio_path = f"{audio_path}_output.ogg"
        temporary_files.extend([audio_path, output_audio_path])

        if not has_audio_stream(audio_path):
            raise SoundStickerError("File audio rusak atau tidak memiliki suara.")

        return SoundStickerResult(
            sticker_webp=gambar_ke_webp(image_bytes),
            audio_ogg=render_voice_note_ogg(
                audio_path,
                output_audio_path,
                max_audio_duration,
            ),
        )

    finally:
        for path in temporary_files:
            try:
                os.remove(path)
            except OSError:
                pass


def video_ke_webp_animasi(raw_bytes: bytes) -> bytes:
    temporary_files: list[str] = []

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as file:
            file.write(raw_bytes)
            input_path = file.name

        output_path = f"{input_path}.webp"
        temporary_files.extend([input_path, output_path])
        return render_animated_webp(input_path, output_path, 5)

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
                "6",
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
            raise SoundStickerError(f"FFmpeg GIF gagal: {result.stderr}")

        return Path(output_path).read_bytes()

    finally:
        for path in temporary_files:
            try:
                os.remove(path)
            except OSError:
                pass


def teks_ke_ogg_opus(teks: str) -> bytes:
    temporary_files: list[str] = []

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as file:
            mp3_path = file.name

        ogg_path = f"{mp3_path}.ogg"
        temporary_files.extend([mp3_path, ogg_path])
        gTTS(text=teks, lang="id").save(mp3_path)

        result = run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                mp3_path,
                "-c:a",
                "libopus",
                "-b:a",
                "48k",
                "-ar",
                "48000",
                "-application",
                "voip",
                "-f",
                "ogg",
                ogg_path,
            ]
        )

        if result.returncode != 0:
            raise SoundStickerError(f"FFmpeg TTS gagal: {result.stderr}")

        return Path(ogg_path).read_bytes()

    finally:
        for path in temporary_files:
            try:
                os.remove(path)
            except OSError:
                pass


def ambil_target_media(message):
    if (
        message.HasField("extendedTextMessage")
        and message.extendedTextMessage.contextInfo.HasField("quotedMessage")
    ):
        return message.extendedTextMessage.contextInfo.quotedMessage
    return message


def kirim_audio_dari_bytes(c, chat, audio_bytes: bytes, event) -> None:
    """Kirim audio melalui path sementara agar format/durasi terbaca Neonize."""
    audio_path = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as file:
            file.write(audio_bytes)
            audio_path = file.name

        logger.info(
            "Mengirim audio %s, ukuran=%d byte",
            audio_path,
            len(audio_bytes),
        )

        response = c.send_audio(chat, audio_path, ptt=True)
        logger.info("Audio berhasil dikirim: %s", response)

    finally:
        if audio_path:
            try:
                os.remove(audio_path)
            except OSError:
                pass


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
        or ""
    ).strip()

    if not text:
        return

    command = text.lower()

    if command in (".menu", "!menu", "menu"):
        c.reply_message(MENU_TEXT, event)
        return

    if command.startswith((".ai ", "!ai ")):
        question = text[4:].strip()
        if not question:
            c.reply_message("Contoh: `.ai jelaskan cara kerja subnetting`", event)
            return
        c.reply_message(tanya_ai(question), event)
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

    if command in (".sa", ".sv", "!soundsticker", ".soundsticker"):
        has_quoted = (
            message.HasField("extendedTextMessage")
            and message.extendedTextMessage.contextInfo.HasField("quotedMessage")
        )
        quoted_message = (
            message.extendedTextMessage.contextInfo.quotedMessage
            if has_quoted
            else None
        )

        image_with_audio = (
            message.HasField("imageMessage")
            and message.imageMessage.contextInfo.HasField("quotedMessage")
            and message.imageMessage.contextInfo.quotedMessage.HasField("audioMessage")
        )

        target_video = None
        if message.HasField("videoMessage"):
            target_video = message
        elif quoted_message is not None and quoted_message.HasField("videoMessage"):
            target_video = quoted_message

        if not image_with_audio and target_video is None:
            c.reply_message(
                "Format tidak sesuai. Kirim/reply video bersuara dengan `.sa`, "
                "atau kirim gambar sambil me-reply audio. Stiker dan audio "
                "akan dikirim sebagai dua pesan terpisah.",
                event,
            )
            return

        c.reply_message("Sedang membuat stiker dan mengambil audionya...", event)

        try:
            if image_with_audio:
                audio_message = message.imageMessage.contextInfo.quotedMessage
                result = build_sound_sticker_from_image_and_audio(
                    image_bytes=c.download_any(message),
                    audio_bytes=c.download_any(audio_message),
                    max_audio_duration=10,
                )
            else:
                result = build_sound_sticker_from_video(
                    video_bytes=c.download_any(target_video),
                    max_duration=5,
                )

            c.send_sticker(chat, result.sticker_webp, quoted=event)
            kirim_audio_dari_bytes(c, chat, result.audio_ogg, event)

        except SoundStickerError as error:
            c.reply_message(f"Gagal membuat stiker suara: {error}", event)
        except Exception as error:
            logger.exception("Kesalahan pada perintah .sa")
            c.reply_message(
                f"Terjadi kesalahan teknis: {type(error).__name__}: {error}",
                event,
            )
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

    if command in (".vid", "!vid"):
        target = ambil_target_media(message)
        try:
            if target.HasField("videoMessage"):
                c.send_video(chat, c.download_any(target), gifplayback=False, quoted=event)
            else:
                c.reply_message("Kirim atau reply video dengan caption `.vid`.", event)
        except Exception as error:
            c.reply_message(f"Gagal mengirim video: {error}", event)
        return

    if command.startswith((".vn ", "!vn ")):
        sentence = text[4:].strip()
        if not sentence:
            c.reply_message("Contoh: `.vn halo semuanya`", event)
            return
        try:
            kirim_audio_dari_bytes(c, chat, teks_ke_ogg_opus(sentence), event)
        except Exception as error:
            logger.exception("Gagal membuat Voice Note")
            c.reply_message(f"Gagal membuat Voice Note: {error}", event)


if __name__ == "__main__":
    logger.info("Memulai SIGMA BOT...")
    client.connect()
