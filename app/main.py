from flask import Flask, send_from_directory, jsonify, request, Response
import subprocess
import os
import zipfile
import uuid
import shutil
import threading
import time
import re
import json
import requests

try:
    import patch_spotapi
    patch_spotapi.apply_patches()
except Exception as e:
    print(f"Failed to apply patch_spotapi on main startup: {e}")

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC_ROOT = os.path.join(APP_ROOT, 'web')
if not os.path.isdir(STATIC_ROOT):
    STATIC_ROOT = os.path.join(os.path.dirname(APP_ROOT), 'web')

# Copy sitecustomize to python site-packages if possible for all subprocesses
try:
    import site
    for sp in site.getsitepackages():
        target_sc = os.path.join(sp, 'sitecustomize.py')
        src_sc = os.path.join(APP_ROOT, 'sitecustomize.py')
        src_patch = os.path.join(APP_ROOT, 'patch_spotapi.py')
        if os.path.isfile(src_sc) and not os.path.isfile(target_sc):
            shutil.copy(src_sc, target_sc)
            shutil.copy(src_patch, os.path.join(sp, 'patch_spotapi.py'))
            print(f"Copied sitecustomize.py to {sp}")
            break
except Exception as e:
    print(f"Note on sitecustomize copy: {e}")

app = Flask(__name__, static_folder=STATIC_ROOT)
BASE_DOWNLOAD_FOLDER = os.getenv('BASE_DOWNLOAD_FOLDER', os.path.join(APP_ROOT, 'downloads'))
AUDIO_DOWNLOAD_PATH = os.getenv('AUDIO_DOWNLOAD_PATH', BASE_DOWNLOAD_FOLDER)
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'password')
ADMIN_DOWNLOAD_PATH = AUDIO_DOWNLOAD_PATH  # default to .env path
PORT = int(os.getenv('PORT', '5000'))
CLEANUP_INTERVAL = int(os.getenv('CLEANUP_INTERVAL', '86400'))  # Default 24 hours

os.makedirs(BASE_DOWNLOAD_FOLDER, exist_ok=True)

# ----------------- Persistent Sessions & Jobs -----------------
SESSIONS_FILE = os.path.join(BASE_DOWNLOAD_FOLDER, '.sessions.json')
JOBS_FILE = os.path.join(BASE_DOWNLOAD_FOLDER, '.jobs.json')
data_lock = threading.Lock()

sessions = {}
active_downloads = {}

def load_data():
    global sessions, active_downloads
    with data_lock:
        if os.path.isfile(SESSIONS_FILE):
            try:
                with open(SESSIONS_FILE, 'r') as f:
                    sessions = json.load(f)
            except Exception as e:
                print(f"Error loading sessions: {e}")
                sessions = {}

        if os.path.isfile(JOBS_FILE):
            try:
                with open(JOBS_FILE, 'r') as f:
                    active_downloads = json.load(f)
            except Exception as e:
                print(f"Error loading jobs: {e}")
                active_downloads = {}

def save_sessions():
    with data_lock:
        try:
            with open(SESSIONS_FILE, 'w') as f:
                json.dump(sessions, f)
        except Exception as e:
            print(f"Error saving sessions: {e}")

def save_jobs():
    with data_lock:
        try:
            with open(JOBS_FILE, 'w') as f:
                json.dump(active_downloads, f)
        except Exception as e:
            print(f"Error saving jobs: {e}")

load_data()


# ----------------- Auth Routes -----------------
@app.route('/')
def serve_index():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory(app.static_folder, path)

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    username = data.get('username')
    password = data.get('password')
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        session_id = str(uuid.uuid4())
        sessions[session_id] = username
        save_sessions()
        response = jsonify({"success": True})
        response.set_cookie(
            'session',
            session_id,
            max_age=86400 * 30,
            httponly=True,
            samesite='Lax',
            path='/'
        )
        return response
    return jsonify({"success": False, "message": "Invalid username or password"}), 401

def is_logged_in():
    session_id = request.cookies.get('session')
    return session_id in sessions

@app.route('/logout', methods=['POST'])
def logout():
    session_id = request.cookies.get('session')
    if session_id in sessions:
        del sessions[session_id]
        save_sessions()
    response = jsonify({"success": True})
    response.delete_cookie('session', path='/')
    return response

@app.route('/check-login')
def check_login():
    return jsonify({"loggedIn": is_logged_in()})


def resolve_media_link(raw_url: str) -> str:
    """
    Expands shortened Spotify links (e.g. open.spotify.com/s/..., spotify.link/..., spotify.app.link/...),
    YouTube shortlinks, or any redirecting links to ensure spotdl / yt-dlp receive
    the exact canonical resource URL.
    """
    url = raw_url.strip()
    if not url.startswith('http://') and not url.startswith('https://'):
        return url

    is_spotify_short = 'open.spotify.com/s/' in url or 'spotify.link' in url or 'spotify.app.link' in url
    is_general_short = is_spotify_short or any(short in url for short in ['youtu.be', 'tinyurl.com', 'bit.ly', 't.co'])

    if is_general_short or ('spotify.com' in url and '/s/' in url):
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5'
        }
        try:
            resp = requests.get(url, headers=headers, allow_redirects=True, timeout=12)
            final_url = resp.url
            if re.search(r'open\.spotify\.com/(?:album|track|playlist|artist)/[a-zA-Z0-9]+', final_url):
                return final_url
            
            # Check meta og:url in HTML
            match_og = re.search(r'<meta\s+(?:property|name)=[\"\']og:url[\"\']\s+content=[\"\']([^\"\']+)[\"\']', resp.text, re.I)
            if match_og and re.search(r'open\.spotify\.com/(?:album|track|playlist|artist)/[a-zA-Z0-9]+', match_og.group(1)):
                return match_og.group(1)

            # Check meta canonical in HTML
            match_canon = re.search(r'<link\s+rel=[\"\']canonical[\"\']\s+href=[\"\']([^\"\']+)[\"\']', resp.text, re.I)
            if match_canon and re.search(r'open\.spotify\.com/(?:album|track|playlist|artist)/[a-zA-Z0-9]+', match_canon.group(1)):
                return match_canon.group(1)

            # Look for direct entity links in HTML
            for entity in ['album', 'playlist', 'track', 'artist']:
                entity_match = re.search(rf'https://open\.spotify\.com/{entity}/[a-zA-Z0-9]+', resp.text)
                if entity_match:
                    return entity_match.group(0)

            if final_url != url:
                return final_url
        except Exception as e:
            print(f"⚠️ Error resolving short link {url}: {e}")

    return url


# ----------------- Download Management -----------------
@app.route('/download', methods=['GET', 'POST'])
def download_media():
    if request.method == 'POST':
        data = request.get_json() or {}
        raw_link = data.get('spotify_link') or data.get('link') or data.get('url') or request.args.get('spotify_link') or request.args.get('link') or request.args.get('url')
        custom_path = data.get('target_path') or data.get('custom_path')
    else:
        raw_link = request.args.get('spotify_link') or request.args.get('link') or request.args.get('url')
        custom_path = request.args.get('target_path') or request.args.get('custom_path')

    if not raw_link:
        return jsonify({"status": "error", "message": "No link provided"}), 400

    raw_link = raw_link.strip()
    spotify_link = resolve_media_link(raw_link)
    print(f"🔗 Query URL: {raw_link} -> Resolved: {spotify_link}")

    session_id = str(uuid.uuid4())
    temp_download_folder = os.path.join(BASE_DOWNLOAD_FOLDER, session_id)
    os.makedirs(temp_download_folder, exist_ok=True)

    is_admin = is_logged_in()
    target_admin_path = custom_path if (is_admin and custom_path) else ADMIN_DOWNLOAD_PATH

    # yt-dlp arguments optimized with nodejs JS runtime & compatible clients
    yt_dlp_common_args = "--js-runtimes node --extractor-args youtube:player_client=tv,android,web_creator,mweb"

    if "spotify" in spotify_link:
        command = ['spotdl', '--preload', '--threads', '4']
        
        # Audio providers
        audio_providers_env = os.getenv('SPOTDL_AUDIO_PROVIDERS', 'youtube-music youtube soundcloud')
        if audio_providers_env:
            command.extend(['--audio'] + audio_providers_env.strip().split())

        # Spotify API credentials
        client_id = os.getenv('SPOTIPY_CLIENT_ID')
        client_secret = os.getenv('SPOTIPY_CLIENT_SECRET')
        if client_id and client_secret:
            command.extend(['--client-id', client_id, '--client-secret', client_secret])

        # Modern yt-dlp arguments for spotdl
        command.extend(['--yt-dlp-args', yt_dlp_common_args])

        extra_args_env = os.getenv('SPOTDL_EXTRA_ARGS')
        if extra_args_env:
            command.extend(extra_args_env.strip().split())

        command.extend([
            '--output', f"{temp_download_folder}/{{artist}}/{{album}}/{{title}}.{{output-ext}}",
            '--',
            spotify_link
        ])
    else:
        command = [
            'yt-dlp',
            '--js-runtimes', 'node',
            '--extractor-args', 'youtube:player_client=tv,android,web_creator,mweb',
            '-x', '--audio-format', 'mp3',
            '--audio-quality', '0',
            '--embed-thumbnail',
            '--embed-metadata',
            '-o', f"{temp_download_folder}/%(uploader,artist|Unknown)s/%(album,title|Unknown)s/%(title)s.%(ext)s",
            '--',
            spotify_link
        ]

    initial_logs = []
    if raw_link != spotify_link:
        initial_logs.append(f"🔗 Expanded shortlink to: {spotify_link}")

    with data_lock:
        active_downloads[session_id] = {
            "status": "running",
            "logs": initial_logs,
            "download_path": None,
            "error": None,
            "completed_message": None,
            "spotify_link": spotify_link,
            "is_admin": is_admin,
            "target_path": target_admin_path if is_admin else None,
            "created_at": time.time(),
            "finished_at": None,
            "total_songs": 0,
            "downloaded_songs": 0,
            "current_song": "",
            "progress": 0
        }
    save_jobs()

    thread = threading.Thread(
        target=run_download_thread,
        args=(session_id, is_admin, command, temp_download_folder, target_admin_path)
    )
    thread.daemon = True
    thread.start()

    return jsonify({"status": "success", "session_id": session_id})


def strip_ansi(text):
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)


def run_download_thread(session_id, is_admin, command, temp_download_folder, target_admin_path):
    album_name = None
    total_songs = 0
    downloaded_songs = 0
    recent_errors = []

    try:
        print(f"🎧 Starting download session [{session_id}]: {' '.join(command)}")
        print(f"📁 Temp download folder: {temp_download_folder}")

        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'
        env['PYTHONPATH'] = f"{APP_ROOT}:{env.get('PYTHONPATH', '')}"

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env
        )

        for raw_line in process.stdout:
            # Clean up line (remove ansi color sequences and carriage return splits)
            clean_line = strip_ansi(raw_line).strip()
            if not clean_line:
                continue

            print(f"[{session_id}] ▶️ {clean_line}")

            if "error" in clean_line.lower() or "exception" in clean_line.lower() or "failed" in clean_line.lower():
                recent_errors.append(clean_line)
                if len(recent_errors) > 10:
                    recent_errors.pop(0)

            # Match total songs count from spotdl or yt-dlp playlist
            match_total = re.search(r'Found (\d+) songs in (.+?) \(', clean_line)
            if match_total:
                total_songs = int(match_total.group(1))
                album_name = match_total.group(2).strip()
            else:
                match_total_alt = re.search(r'Found (\d+) songs', clean_line)
                if match_total_alt:
                    total_songs = int(match_total_alt.group(1))

            # Match completed song download
            match_downloaded = re.search(r'Downloaded "(.+?)"', clean_line)
            if match_downloaded:
                downloaded_songs += 1
                current_title = match_downloaded.group(1).strip()
            else:
                current_title = ""

            # Calculate progress percentage
            progress = 0
            if total_songs > 0:
                progress = min(int((downloaded_songs / total_songs) * 98), 98)
            else:
                match_percent = re.search(r'\[download\]\s+([0-9\.]+)%', clean_line)
                if match_percent:
                    try:
                        progress = min(int(float(match_percent.group(1))), 98)
                    except ValueError:
                        pass

            with data_lock:
                if session_id in active_downloads:
                    job = active_downloads[session_id]
                    job["logs"].append(clean_line)
                    # Keep max 500 lines of logs per job
                    if len(job["logs"]) > 500:
                        job["logs"] = job["logs"][-500:]
                    if total_songs > 0:
                        job["total_songs"] = total_songs
                    if downloaded_songs > 0:
                        job["downloaded_songs"] = downloaded_songs
                    if current_title:
                        job["current_song"] = current_title
                    if progress > job.get("progress", 0):
                        job["progress"] = progress

        process.stdout.close()
        process.wait()

        # Gather all downloaded audio files
        downloaded_files = []
        for root, _, files in os.walk(temp_download_folder):
            for file in files:
                full_path = os.path.join(root, file)
                downloaded_files.append(full_path)

        valid_audio_files = [
            f for f in downloaded_files
            if f.lower().endswith(('.mp3', '.m4a', '.flac', '.wav', '.ogg', '.opus'))
        ]

        if process.returncode != 0 and not valid_audio_files:
            err_details = "\n".join(recent_errors) if recent_errors else f"Process exited with code {process.returncode}"
            err_msg = f"Download failed. Details: {err_details}"
            with data_lock:
                if session_id in active_downloads:
                    active_downloads[session_id]["status"] = "failed"
                    active_downloads[session_id]["error"] = err_msg
                    active_downloads[session_id]["finished_at"] = time.time()
                    active_downloads[session_id]["logs"].append(f"ERROR: {err_msg}")
            save_jobs()
            return

        if not valid_audio_files:
            err_details = "\n".join(recent_errors) if recent_errors else "No audio files were downloaded. Please check the URL."
            err_msg = f"No valid audio files found. {err_details}"
            with data_lock:
                if session_id in active_downloads:
                    active_downloads[session_id]["status"] = "failed"
                    active_downloads[session_id]["error"] = err_msg
                    active_downloads[session_id]["finished_at"] = time.time()
                    active_downloads[session_id]["logs"].append(f"ERROR: {err_msg}")
            save_jobs()
            return

        # ✅ ADMIN HANDLING: Move directly to server storage
        if is_admin:
            dest_dir = target_admin_path or ADMIN_DOWNLOAD_PATH
            for file_path in valid_audio_files:
                filename = os.path.basename(file_path)

                # Check if this is a General Conference talk
                norm_filename = filename.replace('｜', '|').replace('—', '-').replace('–', '-')
                is_gc = any(kw in norm_filename for kw in ['General Conference', 'General Women’s Session', 'General Womens Session']) or ('general_conference' in dest_dir.lower())

                speaker_name = None
                if is_gc and '|' in norm_filename:
                    parts = [p.strip() for p in norm_filename.split('|') if p.strip()]
                    if len(parts) >= 3:
                        # Format: Title | Speaker | Conference Session Date
                        speaker_name = parts[1]
                    elif len(parts) == 2:
                        # Format: Speaker | Conference Session Date
                        if 'General Conference' not in parts[0] and not any(m in parts[0] for m in ['April', 'October', 'Session']):
                            speaker_name = parts[0]
                        else:
                            speaker_name = parts[1]

                    if speaker_name and speaker_name.lower().endswith(('.mp3', '.m4a', '.flac', '.wav', '.ogg', '.opus')):
                        speaker_name = os.path.splitext(speaker_name)[0].strip()

                if is_gc and speaker_name:
                    # If dest_dir is general Music root, ensure it goes into General_Conference folder
                    gc_dest = dest_dir
                    if not gc_dest.rstrip('/').endswith('General_Conference') and os.path.isdir(os.path.join(AUDIO_DOWNLOAD_PATH, 'General_Conference')):
                        gc_dest = os.path.join(AUDIO_DOWNLOAD_PATH, 'General_Conference')
                    target_path = os.path.join(gc_dest, speaker_name, filename)
                else:
                    relative_path = os.path.relpath(file_path, temp_download_folder)
                    target_path = os.path.join(dest_dir, relative_path)

                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                try:
                    shutil.move(file_path, target_path)
                    print(f"🚚 Moved {filename} -> {target_path}")
                except Exception as move_error:
                    print(f"❌ Failed to move {file_path} to {target_path}: {move_error}")

            shutil.rmtree(temp_download_folder, ignore_errors=True)
            with data_lock:
                if session_id in active_downloads:
                    active_downloads[session_id]["status"] = "completed"
                    active_downloads[session_id]["progress"] = 100
                    active_downloads[session_id]["finished_at"] = time.time()
                    msg = f"Download completed! {len(valid_audio_files)} file(s) saved directly to server directory: {dest_dir}"
                    active_downloads[session_id]["completed_message"] = msg
                    active_downloads[session_id]["logs"].append(msg)
            save_jobs()
            return

        # ✅ PUBLIC USER HANDLING: Create ZIP or single file download link
        if len(valid_audio_files) > 1:
            safe_album_name = re.sub(r'[^\w\s-]', '', album_name).strip() if album_name else "playlist"
            safe_album_name = safe_album_name or "playlist"
            zip_filename = f"{safe_album_name}.zip"
            zip_path = os.path.join(temp_download_folder, zip_filename)

            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for file_path in valid_audio_files:
                    arcname = os.path.relpath(file_path, start=temp_download_folder)
                    zipf.write(file_path, arcname=arcname)

            download_path = f"{session_id}/{zip_filename}"
        else:
            from urllib.parse import quote
            relative_path = os.path.relpath(valid_audio_files[0], start=temp_download_folder)
            encoded_path = quote(relative_path)
            download_path = f"{session_id}/{encoded_path}"

        with data_lock:
            if session_id in active_downloads:
                active_downloads[session_id]["status"] = "completed"
                active_downloads[session_id]["progress"] = 100
                active_downloads[session_id]["download_path"] = download_path
                active_downloads[session_id]["finished_at"] = time.time()
                active_downloads[session_id]["logs"].append(f"DOWNLOAD: {download_path}")
        save_jobs()

    except Exception as e:
        err_msg = f"Unexpected download error: {str(e)}"
        print(f"❌ [{session_id}] {err_msg}")
        with data_lock:
            if session_id in active_downloads:
                active_downloads[session_id]["status"] = "failed"
                active_downloads[session_id]["error"] = err_msg
                active_downloads[session_id]["finished_at"] = time.time()
                active_downloads[session_id]["logs"].append(f"ERROR: {err_msg}")
        save_jobs()


@app.route('/status/<session_id>')
def get_status(session_id):
    with data_lock:
        job = active_downloads.get(session_id)
    if not job:
        return jsonify({"status": "not_found"}), 404

    return jsonify({
        "status": job.get("status"),
        "progress": job.get("progress", 0),
        "total_songs": job.get("total_songs", 0),
        "downloaded_songs": job.get("downloaded_songs", 0),
        "current_song": job.get("current_song", ""),
        "download_path": job.get("download_path"),
        "error": job.get("error"),
        "completed_message": job.get("completed_message"),
        "logs": job.get("logs", [])
    })


@app.route('/stream/<session_id>')
def stream_logs(session_id):
    def event_generator():
        idx = 0
        last_ping = time.time()
        while True:
            with data_lock:
                job = active_downloads.get(session_id)

            if not job:
                yield "data: Error: Session not found.\n\n"
                break

            # Stream all accumulated new logs
            logs = job.get("logs", [])
            while idx < len(logs):
                yield f"data: {logs[idx]}\n\n"
                idx += 1
                last_ping = time.time()

            if job.get("status") in ("completed", "failed"):
                # One last pass to ensure all logs were sent
                break

            # Send periodic keepalive comment every 10 seconds to keep proxies from dropping connection
            now = time.time()
            if now - last_ping > 10:
                yield ": keepalive\n\n"
                last_ping = now

            time.sleep(0.5)

    return Response(
        event_generator(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )


def emergency_cleanup_container_downloads():
    now = time.time()
    print("🚨 Running periodic cleanup check in /app/downloads")
    
    # 1. Clean folders
    for item in os.listdir(BASE_DOWNLOAD_FOLDER):
        if item.startswith('.'):
            continue
        folder_path = os.path.join(BASE_DOWNLOAD_FOLDER, item)
        if not os.path.isdir(folder_path):
            continue

        with data_lock:
            job = active_downloads.get(item)

        # NEVER delete folder if job is currently running!
        if job and job.get("status") == "running":
            continue

        try:
            mtime = os.path.getmtime(folder_path)
            if now - mtime > CLEANUP_INTERVAL:
                shutil.rmtree(folder_path, ignore_errors=True)
                print(f"🗑️ Cleaned expired folder: {folder_path}")
        except Exception as e:
            print(f"⚠️ Could not delete {folder_path}: {e}")

    # 2. Clean old job records from memory & file
    with data_lock:
        to_delete = []
        for sid, job in active_downloads.items():
            if job.get("status") != "running":
                finished = job.get("finished_at") or job.get("created_at", now)
                if now - finished > CLEANUP_INTERVAL:
                    to_delete.append(sid)
        for sid in to_delete:
            del active_downloads[sid]
            print(f"🗑️ Cleaned expired job record: {sid}")

    if to_delete:
        save_jobs()


def schedule_emergency_cleanup(interval_seconds=3600):
    def loop():
        while True:
            time.sleep(interval_seconds)
            try:
                emergency_cleanup_container_downloads()
            except Exception as e:
                print(f"Cleanup error: {e}")

    threading.Thread(target=loop, daemon=True).start()


@app.route('/set-download-path', methods=['POST'])
def set_download_path():
    global ADMIN_DOWNLOAD_PATH
    if not is_logged_in():
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    data = request.get_json() or {}
    new_path = data.get('path')

    if not new_path:
        return jsonify({"success": False, "message": "Path cannot be empty."}), 400

    new_path = new_path.strip()
    if not os.path.isdir(new_path):
        try:
            os.makedirs(new_path, exist_ok=True)
        except Exception as e:
            return jsonify({"success": False, "message": f"Cannot create directory: {str(e)}"}), 500

    ADMIN_DOWNLOAD_PATH = new_path
    return jsonify({"success": True, "new_path": ADMIN_DOWNLOAD_PATH})


@app.route('/download-options')
def get_download_options():
    if not is_logged_in():
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    options_str = os.getenv('DOWNLOAD_OPTIONS', '')
    options = []
    for opt in options_str.split(','):
        opt = opt.strip()
        if not opt:
            continue
        if ':' in opt and not opt.startswith('/'):
            parts = opt.split(':', 1)
            label = parts[0].strip()
            path = parts[1].strip()
            options.append({"label": label, "path": path})
        else:
            options.append({"label": opt, "path": opt})

    return jsonify({
        "success": True,
        "options": options,
        "current_path": ADMIN_DOWNLOAD_PATH
    })


@app.route('/downloads/<session_id>/<path:filename>')
def serve_download(session_id, filename):
    session_download_folder = os.path.join(BASE_DOWNLOAD_FOLDER, session_id)
    full_path = os.path.join(session_download_folder, filename)

    if ".." in filename or filename.startswith("/"):
        return "Invalid filename", 400

    if not os.path.isfile(full_path):
        print(f"❌ File does not exist: {full_path}")
        return "File not found or has expired.", 404

    return send_from_directory(session_download_folder, filename, as_attachment=True)


schedule_emergency_cleanup(3600)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT)
