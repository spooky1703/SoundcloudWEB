# app.py
"""
Robust SoundCloud downloader (Flask + Flask-SocketIO + yt-dlp)
Estrategia:
 - Usar yt-dlp solo para descargar el audio (sin postprocesamiento).
 - Localizar el archivo descargado y convertirlo con ffmpeg (subprocess).
 - Si la conversión falla, devolver el archivo raw como fallback.
 - Emitir mensajes y logs detallados para depuración.
"""

# monkey_patch temprano para eventlet si está disponible
async_mode = None
try:
    import eventlet  # type: ignore
    eventlet.monkey_patch()
    async_mode = "eventlet"
    print("[info] eventlet detectado y monkey_patch aplicado. async_mode = 'eventlet'")
except Exception:
    try:
        import gevent  # type: ignore
        async_mode = "gevent"
        print("[info] gevent detectado. async_mode = 'gevent'")
    except Exception:
        async_mode = "threading"
        print("[info] eventlet/gevent no detectados; usando 'threading' como fallback.")

import os
import json
import uuid
import threading
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room

import yt_dlp
from pathlib import Path

# ----- Configuración -----
BASE_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = BASE_DIR / "config.json"
# CAMBIO AQUÍ: Cambiar Downloads por Documents
DOWNLOADS_DIR = Path.home() / "Documents"
LOG_FILE = BASE_DIR / "app.log"

DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("sc-downloader")

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config['SECRET_KEY'] = "cambiar_por_una_clave_segura"

socketio = SocketIO(app, cors_allowed_origins="*", async_mode=async_mode)

# Estado en memoria
downloads: Dict[str, Dict[str, Any]] = {}
ip_to_room: Dict[str, str] = {}
sid_to_room: Dict[str, str] = {}

DEFAULT_CONFIG = {
    "format": "mp3",
    "bitrate": "192",
    "template": "%(artist)s - %(title)s.%(ext)s",
    "create_artist_folders": True,
    "skip_existing": True,
    "add_metadata": True,
    "embed_thumbnail": True,
    "cover_format": "jpg",
    "cover_size": "original"
}

# ----- Utilidades -----
def has_executable(name: str) -> bool:
    return shutil.which(name) is not None

def ffmpeg_path() -> Optional[str]:
    return shutil.which("ffmpeg")

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                logger.info("Configuración cargada desde config.json")
                return cfg
        except Exception:
            logger.exception("Error leyendo config.json, usando defaults")
            return DEFAULT_CONFIG.copy()
    else:
        return DEFAULT_CONFIG.copy()

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    logger.info("Configuración guardada en config.json")

def get_room_for_request(req) -> Optional[str]:
    ip = req.remote_addr or "unknown"
    return ip_to_room.get(ip)

def emit_progress(download_id: str, type_: str, data: Any, room: str = None):
    payload = {"download_id": download_id, "type": type_, "data": data}
    try:
        if room:
            socketio.emit("download_progress", payload, room=room)
        else:
            socketio.emit("download_progress", payload)
    except Exception:
        logger.exception("Error al emitir progreso de descarga")

# Buscar archivos creados/ modificados después de `since_ts` en `directory`
def find_recent_file(directory: Path, since_ts: float) -> Optional[Path]:
    """
    Encuentra el archivo más reciente creado/modificado >= since_ts.
    Prioriza extensiones de audio conocidas.
    """
    if not directory.exists():
        return None

    audio_exts = {'.mp3', '.m4a', '.webm', '.opus', '.aac', '.wav', '.flac', '.ogg', '.m4b'}
    candidates = []
    for p in directory.rglob("*"):
        if p.is_file():
            try:
                mtime = p.stat().st_mtime
                if mtime >= since_ts:
                    candidates.append((mtime, p))
            except Exception:
                continue
    if not candidates:
        return None

    # Priorizar audio
    audio_candidates = [c for c in candidates if c[1].suffix.lower() in audio_exts]
    if audio_candidates:
        audio_candidates.sort(key=lambda x: (x[0], x[1].stat().st_size if x[1].exists() else 0), reverse=True)
        return audio_candidates[0][1]

    candidates.sort(key=lambda x: (x[0], x[1].stat().st_size if x[1].exists() else 0), reverse=True)
    return candidates[0][1]

def convert_with_ffmpeg(src: Path, target: Path, fmt: str, bitrate_k: str) -> Tuple[bool, Optional[Path], str, str]:
    """
    Convierte src -> target con ffmpeg, retornando (ok, target_path, stdout, stderr).
    """
    ff = ffmpeg_path()
    if not ff:
        return False, None, "", "ffmpeg not found in PATH"
    codec_args = []
    if fmt == "mp3":
        codec_args = ["-acodec", "libmp3lame", "-b:a", f"{bitrate_k}k"]
    elif fmt in ("m4a", "aac"):
        codec_args = ["-c:a", "aac", "-b:a", f"{bitrate_k}k"]
    elif fmt == "flac":
        codec_args = ["-c:a", "flac"]
    elif fmt == "wav":
        codec_args = ["-c:a", "pcm_s16le"]
    else:
        codec_args = ["-c:a", "copy"]

    cmd = [ff, "-y", "-i", str(src), "-vn"] + codec_args + [str(target)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=300)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        if proc.returncode == 0 and target.exists():
            return True, target, stdout, stderr
        else:
            return False, None, stdout, stderr
    except Exception as e:
        return False, None, "", str(e)

# Logger adapter para yt-dlp (envía mensajes a nuestro logger)
class YTDLPLogger:
    def debug(self, msg):
        logger.debug("yt-dlp: %s", msg)
    def info(self, msg):
        logger.info("yt-dlp: %s", msg)
    def warning(self, msg):
        logger.warning("yt-dlp: %s", msg)
    def error(self, msg):
        logger.error("yt-dlp: %s", msg)

# ----- Socket.IO handlers -----
@socketio.on("connect")
def handle_connect():
    sid = request.sid
    room_id = str(uuid.uuid4())
    emit("connected", {"room_id": room_id})
    sid_to_room[sid] = room_id
    ip = request.remote_addr or "unknown"
    ip_to_room[ip] = room_id
    logger.info(f"Socket conectado: sid={sid}, ip={ip}, room={room_id}")

@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid
    room = sid_to_room.pop(sid, None)
    logger.info(f"Socket desconectado: sid={sid}, room={room}")

@socketio.on("join_room")
def handle_join(data):
    room_id = data.get("room_id")
    if not room_id:
        return
    join_room(room_id)
    ip = request.remote_addr or "unknown"
    ip_to_room[ip] = room_id
    sid_to_room[request.sid] = room_id
    logger.info(f"Sid {request.sid} se unió a la sala {room_id}")

@socketio.on("leave_room")
def handle_leave(data):
    room_id = data.get("room_id")
    if room_id:
        leave_room(room_id)
        logger.info(f"Sid {request.sid} dejó la sala {room_id}")

# ----- HTTP endpoints -----
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        return jsonify(load_config())
    else:
        try:
            payload = request.get_json(force=True)
            cfg = DEFAULT_CONFIG.copy()
            cfg.update(payload or {})
            save_config(cfg)
            return jsonify({"status": "success"})
        except Exception as e:
            logger.exception("Error guardando configuración")
            return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/download/start", methods=["POST"])
def api_download_start():
    payload = request.get_json(force=True)
    url = payload.get("url")
    cfg = payload.get("config", {})

    if not url:
        return jsonify({"status": "error", "message": "URL vacía"}), 400

    room = get_room_for_request(request)
    download_id = str(uuid.uuid4())
    downloads[download_id] = {"thread": None, "cancel": False, "room": room, "meta": {}}

    thread = threading.Thread(target=_download_worker, args=(download_id, url, cfg, room), daemon=True)
    downloads[download_id]["thread"] = thread
    thread.start()

    logger.info(f"Iniciada descarga {download_id} para URL: {url} (room={room})")
    return jsonify({"status": "success", "download_id": download_id})

@app.route("/api/download/cancel/<download_id>", methods=["POST"])
def api_download_cancel(download_id):
    info = downloads.get(download_id)
    if not info:
        return jsonify({"status": "error", "message": "Descarga no encontrada"}), 404
    info["cancel"] = True
    logger.info(f"Se solicitó cancelación de descarga {download_id}")
    emit_progress(download_id, "status", "Cancelando descarga...", room=info.get("room"))
    return jsonify({"status": "success"})

# CAMBIO AQUÍ: Esta función ahora sirve archivos desde Documents
@app.route("/downloads/<path:filename>")
def serve_download(filename):
    return send_from_directory(DOWNLOADS_DIR, filename, as_attachment=True)

# ----- Worker principal -----
def _download_worker(download_id: str, url: str, cfg: dict, room: str):
    start_ts = time.time()
    try:
        config = DEFAULT_CONFIG.copy()
        config.update(cfg or {})

        ffmpeg_ok = has_executable("ffmpeg")
        ffprobe_ok = has_executable("ffprobe")
        if not ffmpeg_ok or not ffprobe_ok:
            emit_progress(download_id, "status", "ffmpeg/ffprobe no detectados: descarga sin conversión automática (fallback).", room=room)
            logger.warning("ffmpeg/ffprobe no detectados; la conversión automática no estará disponible.")

        # Extraer info sin descargar
        ydl_opts_info = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": YTDLPLogger()}
        with yt_dlp.YoutubeDL(ydl_opts_info) as ydl_info:
            try:
                info = ydl_info.extract_info(url, download=False)
            except Exception as e:
                logger.exception("Error extrayendo info")
                emit_progress(download_id, "error", f"Error al obtener información: {str(e)}", room=room)
                downloads.pop(download_id, None)
                return

        info_payload = {
            "title": info.get("title"),
            "artist": info.get("uploader") or info.get("creator") or "",
            "duration": int(info.get("duration")) if info.get("duration") else None,
            "thumbnail": info.get("thumbnail"),
            "webpage_url": info.get("webpage_url") or url,
            "id": info.get("id"),
            "ext": info.get("ext")
        }
        downloads[download_id]["meta"]["info"] = info_payload
        emit_progress(download_id, "info", info_payload, room=room)
        emit_progress(download_id, "status", "Información obtenida", room=room)

        if downloads[download_id].get("cancel"):
            emit_progress(download_id, "canceled", "Descarga cancelada antes de comenzar", room=room)
            downloads.pop(download_id, None)
            return

        # Preparar salida
        fmt = config.get("format", "mp3")
        bitrate = config.get("bitrate", "192")
        template = config.get("template") or DEFAULT_CONFIG["template"]

        if config.get("create_artist_folders"):
            uploader = info.get("uploader") or "unknown_artist"
            out_dir = DOWNLOADS_DIR / sanitize_filename(uploader)
        else:
            out_dir = DOWNLOADS_DIR
        out_dir.mkdir(parents=True, exist_ok=True)

        # Importante: asegurarnos de que outtmpl incluya %(ext)s para que yt-dlp agregue extensión real
        # Si la plantilla del usuario no contiene %(ext)s, la completamos aquí.
        user_template = template
        if "%(ext)s" not in user_template:
            # si usuario escribió una plantilla sin ext, añadir .%(ext)s
            user_template = user_template.rstrip() + ".%(ext)s"

        outtmpl = str(out_dir / user_template)

        # Hook de progreso para yt-dlp
        def progress_hook(d):
            try:
                if downloads.get(download_id, {}).get("cancel"):
                    raise Exception("cancelled_by_user")
                status = d.get("status")
                if status == "downloading":
                    downloaded = d.get("downloaded_bytes") or 0
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    speed = d.get("speed") or 0
                    eta = d.get("eta") or None
                    percent = 0.0
                    if total and total > 0:
                        try:
                            percent = (downloaded / total) * 100.0
                        except Exception:
                            percent = 0.0
                    progress_data = {
                        "percent": percent,
                        "downloaded": downloaded,
                        "total": total,
                        "speed": speed,
                        "eta": eta,
                    }
                    emit_progress(download_id, "progress", progress_data, room=room)
                elif status == "finished":
                    emit_progress(download_id, "status", "Descargado (raw). Preparando conversión...", room=room)
                elif status == "error":
                    emit_progress(download_id, "error", "Error durante la descarga", room=room)
            except Exception as e:
                if str(e) == "cancelled_by_user":
                    raise e
                else:
                    logger.exception("Error en progress_hook")

        # ydl_opts: SIN postprocessors para evitar fallos de yt-dlp
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [progress_hook],
            "writethumbnail": bool(config.get("embed_thumbnail")),
            "ignoreerrors": False,
            "logger": YTDLPLogger(),
            # no postprocessors -> conversion manual
            "postprocessors": [],
        }

        # Si skip_existing está activo, comprobar candidato final posible
        if config.get("skip_existing"):
            try:
                with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "logger": YTDLPLogger()}) as probe:
                    suggested = probe.prepare_filename(info)
                    if suggested:
                        suggested = Path(suggested)
                        # candidate final raw (actual ext) unknown; check common output ext candidates for raw
                        candidates_to_check = [suggested.with_suffix(ext) for ext in ['.m4a', '.aac', '.webm', '.opus', '.mp3', '.wav', '.flac', '.ogg']]
                        for c in candidates_to_check:
                            if c.exists():
                                try:
                                    rel = c.relative_to(DOWNLOADS_DIR)
                                    download_url = f"/downloads/{rel.as_posix()}"
                                except Exception:
                                    download_url = f"/downloads/{c.name}"
                                emit_progress(download_id, "status", "Archivo ya existe, saltando descarga", room=room)
                                emit_progress(download_id, "complete", {"message": f"Ya existe: {c.name}", "download_url": download_url}, room=room)
                                downloads.pop(download_id, None)
                                return
            except Exception:
                pass

        # Ejecutar descarga (solo raw)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                emit_progress(download_id, "status", "Descargando (raw audio)...", room=room)
                ydl.download([url])

            # localizar archivo raw reciente en out_dir
            recent = find_recent_file(out_dir, start_ts - 1.0)
            if not recent:
                emit_progress(download_id, "error", "No se encontró ningún archivo descargado. Revisa app.log para detalles.", room=room)
                logger.error("No se encontró archivo descargado en %s tras yt-dlp", out_dir)
                downloads.pop(download_id, None)
                return

            logger.info("Archivo raw encontrado: %s", recent)
            # decidir objetivo final
            target = None
            try:
                with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "logger": YTDLPLogger()}) as probe:
                    suggested = probe.prepare_filename(info)
            except Exception:
                suggested = None

            if suggested:
                target = Path(suggested).with_suffix(f".{fmt}")
                # ensure same directory as suggested (which uses user's template/outtmpl)
                if not target.parent.exists():
                    target = recent.with_suffix(f".{fmt}")
            else:
                target = recent.with_suffix(f".{fmt}")

            # Si el usuario pidió el mismo formato que el raw, evitamos recodificar (si coinciden ext)
            if recent.suffix.lower().lstrip('.') == fmt.lower():
                # no recodificar
                try:
                    rel = recent.relative_to(DOWNLOADS_DIR)
                    download_url = f"/downloads/{rel.as_posix()}"
                except Exception:
                    download_url = f"/downloads/{recent.name}"
                emit_progress(download_id, "complete", {"message": "Descargado (raw) en formato solicitado", "download_url": download_url}, room=room)
                downloads.pop(download_id, None)
                return

            # Intentar conversión si ffmpeg disponible
            if has_executable("ffmpeg"):
                emit_progress(download_id, "status", f"Convirtiendo {recent.name} -> {target.name} (ffmpeg)...", room=room)
                ok, target_path, stdout, stderr = convert_with_ffmpeg(recent, target, fmt, str(bitrate))
                logger.info("ffmpeg stdout: %s", stdout)
                logger.info("ffmpeg stderr: %s", stderr)
                if ok and target_path and target_path.exists():
                    try:
                        rel = target_path.relative_to(DOWNLOADS_DIR)
                        download_url = f"/downloads/{rel.as_posix()}"
                    except Exception:
                        download_url = f"/downloads/{target_path.name}"
                    emit_progress(download_id, "complete", {"message": f"Conversión completada: {target_path.name}", "download_url": download_url}, room=room)
                    downloads.pop(download_id, None)
                    return
                else:
                    # conversión falló: devolver raw como fallback
                    try:
                        rel = recent.relative_to(DOWNLOADS_DIR)
                        download_url = f"/downloads/{rel.as_posix()}"
                    except Exception:
                        download_url = f"/downloads/{recent.name}"
                    emit_progress(download_id, "complete", {"message": f"Conversión falló; entregando archivo original: {recent.name}. Revisa app.log para detalles.", "download_url": download_url}, room=room)
                    downloads.pop(download_id, None)
                    return
            else:
                # ffmpeg no instalado -> devolver raw
                try:
                    rel = recent.relative_to(DOWNLOADS_DIR)
                    download_url = f"/downloads/{rel.as_posix()}"
                except Exception:
                    download_url = f"/downloads/{recent.name}"
                emit_progress(download_id, "complete", {"message": f"ffmpeg no disponible; entregando archivo original: {recent.name}", "download_url": download_url}, room=room)
                downloads.pop(download_id, None)
                return

        except Exception as e:
            logger.exception("Error ejecutando yt-dlp")
            emit_progress(download_id, "error", f"Error ejecutando descarga: {str(e)}", room=room)
            downloads.pop(download_id, None)
            return

    finally:
        downloads.pop(download_id, None)

# ----- Helpers -----
def sanitize_filename(name: str) -> str:
    if not name:
        return "unknown"
    invalid = r'<>:"/\\|?*'
    safe = "".join(c for c in name if c not in invalid)
    return safe.strip()[:200]

# ----- Main -----
if __name__ == "__main__":
    cfg = load_config()
    logger.info(f"Iniciando servidor (config: {json.dumps(cfg)}) - async_mode={async_mode}")
    if not has_executable("ffmpeg") or not has_executable("ffprobe"):
        logger.warning("ffmpeg y/o ffprobe no detectados en PATH. La conversión automatizada no funcionará hasta que se instalen.")
        logger.warning("macOS (brew): brew install ffmpeg")
        logger.warning("Ubuntu/Debian: sudo apt install ffmpeg")

    if async_mode in ("eventlet", "gevent"):
        socketio.run(app, host="0.0.0.0", port=5003, debug=False)
    else:
        socketio.run(app, host="0.0.0.0", port=5004, debug=True)