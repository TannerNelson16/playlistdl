from flask import Flask, send_from_directory, jsonify, request, Response
import subprocess
import os
import zipfile
import uuid
import shutil
import threading
import time
import re  # Add regex for capturing album/playlist name
import hashlib
import mutagen
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.m4a import M4A
from datetime import datetime, timedelta

app = Flask(__name__, static_folder='web')
BASE_DOWNLOAD_FOLDER = os.getenv('BASE_DOWNLOAD_FOLDER', '/app/downloads')
AUDIO_DOWNLOAD_PATH = os.getenv('AUDIO_DOWNLOAD_PATH', BASE_DOWNLOAD_FOLDER)
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')
ADMIN_DOWNLOAD_PATH = AUDIO_DOWNLOAD_PATH  # default to .env path
# 阻止清理操作的保护时间, 文件修改时间在此时间段内，则不执行清理操作, 缺省4小时
CLEANUP_PROTECTION_TIME = os.getenv('CLEANUP_PROTECTION_TIME', 4 * 3600)
print(f"AUDIO_DOWNLOAD_PATH: {AUDIO_DOWNLOAD_PATH}")
print(f"ADMIN_DOWNLOAD_PATH: {ADMIN_DOWNLOAD_PATH}")
print(f"CLEANUP_PROTECTION_TIME: {CLEANUP_PROTECTION_TIME}")

sessions = {}
sessions_lock = threading.Lock()

# ========== Download History (deduplication) ==========
class DownloadHistory:
    """Maintains a hash set of downloaded audio files to prevent duplicates."""
    
    def __init__(self, target_dir, scan_interval_seconds=1800):
        self.target_dir = target_dir
        self.scan_interval = scan_interval_seconds
        self._hash_set = set()  # {(artist, title): content_hash}
        self._mtime_map = {}  # {file_path: mtime}
        self._lock = threading.Lock()
        self._last_scan_time = None
        self._scan_in_progress = False
    
    def _get_file_hash(self, file_path):
        """Compute MD5 hash of entire file content."""
        h = hashlib.md5()
        try:
            with open(file_path, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    h.update(chunk)
            return h.hexdigest()
        except Exception as e:
            print(f"⚠️ Failed to hash {file_path}: {e}")
            return None
    
    def _get_metadata(self, file_path):
        """Extract (artist, title) from audio file metadata."""
        try:
            ext = os.path.splitext(file_path)[1].lower()
            if ext == '.mp3':
                audio = MP3(file_path)
            elif ext == '.flac':
                audio = FLAC(file_path)
            elif ext in ('.m4a', '.aac'):
                audio = M4A(file_path)
            else:
                return (None, None)
            
            artist = None
            title = None
            if 'artist' in audio:
                artist = str(audio['artist'][0]) if audio['artist'] else None
            if 'title' in audio:
                title = str(audio['title'][0]) if audio['title'] else None
            return (artist, title)
        except Exception as e:
            # Fallback: use filename as title hint
            return (None, os.path.splitext(os.path.basename(file_path))[0])
    
    def scan_directory(self):
        """Scan target directory and build/update the hash set. Uses mtime for incremental updates."""
        if not os.path.isdir(self.target_dir):
            print(f"📂 Download history: target dir not found {self.target_dir}")
            return
        
        current_mtime_map = {}
        all_files = []
        
        for root, _, files in os.walk(self.target_dir):
            for fname in files:
                if not fname.lower().endswith(('.mp3', '.m4a', '.flac', '.wav', '.ogg')):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    mtime = os.path.getmtime(fpath)
                    current_mtime_map[fpath] = mtime
                    all_files.append((fpath, mtime))
                except OSError:
                    continue
        
        new_files = []
        removed_files = []
        unchanged = []
        
        for fpath, mtime in all_files:
            if fpath not in self._mtime_map:
                new_files.append((fpath, mtime))
            else:
                unchanged.append((fpath, mtime))
        
        for fpath in list(self._mtime_map.keys()):
            if fpath not in current_mtime_map:
                removed_files.append(fpath)
        
        with self._lock:
            for fpath in removed_files:
                entry = self._mtime_map.pop(fpath, None)
            
            for fpath, mtime in new_files:
                file_hash = self._get_file_hash(fpath)
                if file_hash:
                    artist, title = self._get_metadata(fpath)
                    key = (artist or '', title or '', file_hash)
                    self._hash_set.add(key)
                self._mtime_map[fpath] = mtime
        
        print(f"📊 Download history scan done: +{len(new_files)} new, -{len(removed_files)} removed, {len(unchanged)} unchanged (total {len(self._hash_set)})")
        self._last_scan_time = datetime.now()
    
    def is_duplicate(self, file_path):
        """Check if a file (by its content hash) is already in history."""
        with self._lock:
            file_hash = self._get_file_hash(file_path)
            if not file_hash:
                return False
            artist, title = self._get_metadata(file_path)
            key = (artist or '', title or '', file_hash)
            return key in self._hash_set
    
    def add_file(self, file_path):
        """Add a newly downloaded file to the history."""
        file_hash = self._get_file_hash(file_path)
        if not file_hash:
            return
        artist, title = self._get_metadata(file_path)
        with self._lock:
            key = (artist or '', title or '', file_hash)
            self._hash_set.add(key)
            try:
                self._mtime_map[file_path] = os.path.getmtime(file_path)
            except OSError:
                pass
    
    def start_background_scan(self):
        """Start periodic background scanning."""
        def loop():
            while True:
                time.sleep(self.scan_interval)
                self.scan_directory()
        
        # Initial scan
        self.scan_directory()
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        print(f"🔄 Download history background scan started (interval={self.scan_interval}s)")

# Global download history instance (initialized after ADMIN_DOWNLOAD_PATH is set)
download_history = None


def cleanup_expired_sessions(max_age_hours=24):
    """Remove sessions older than max_age_hours"""
    now = datetime.now()
    expired = []
    with sessions_lock:
        for sid, last_active in list(sessions.items()):
            if isinstance(last_active, datetime) and (now - last_active) > timedelta(hours=max_age_hours):
                expired.append(sid)
        for sid in expired:
                del sessions[sid]
    if expired:
        print(f"🧹 Cleaned up {len(expired)} expired sessions")

def session_cleanup_loop():
    while True:
        time.sleep(3600)  # Run every hour
        cleanup_expired_sessions()

os.makedirs(BASE_DOWNLOAD_FOLDER, exist_ok=True)

# Initialize download history for deduplication (scan every 30 min)
history_scan_interval = int(os.getenv('HISTORY_SCAN_INTERVAL', 1800))
download_history = DownloadHistory(ADMIN_DOWNLOAD_PATH, scan_interval_seconds=history_scan_interval)
download_history.start_background_scan()

@app.route('/')
def serve_index():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory(app.static_folder, path)

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        session_id = str(uuid.uuid4())
        with sessions_lock:
            sessions[session_id] = datetime.now()
        response = jsonify({"success": True})
        response.set_cookie('session', session_id)
        return response
    return jsonify({"success": False}), 401

def is_logged_in():
    session_id = request.cookies.get('session')
    with sessions_lock:
        if session_id in sessions:
            sessions[session_id] = datetime.now()  # Update last active time
            return True
    return False

@app.route('/logout', methods=['POST'])
def logout():
    session_id = request.cookies.get('session')
    if session_id:
        with sessions_lock:
            sessions.pop(session_id, None)
    response = jsonify({"success": True})
    response.delete_cookie('session')  # Remove session cookie
    return response

@app.route('/check-login')
def check_login():
    is_logged_in_status = is_logged_in()
    return jsonify({"loggedIn": is_logged_in_status})


@app.route('/download')
def download_media():
    spotify_link = request.args.get('spotify_link')
    if not spotify_link:
        return jsonify({"status": "error", "output": "No link provided"}), 400

    # Fix QQ Music _v2 URL issue: convert y.qq.com/n/ryqq_v2/playlist/ to y.qq.com/n/ryqq/playlist/
    # yt-dlp's QQMusicPlaylistIE only supports the old format
    # Only apply to QQ Music links to avoid affecting other platforms
    if 'y.qq.com/n/ryqq_v2/' in spotify_link:
        spotify_link = spotify_link.replace('y.qq.com/n/ryqq_v2/', 'y.qq.com/n/ryqq/')

    session_id = str(uuid.uuid4())
    temp_download_folder = os.path.join(BASE_DOWNLOAD_FOLDER, session_id)
    os.makedirs(temp_download_folder, exist_ok=True)

    # More robust platform detection
    is_spotify = 'spotify.com' in spotify_link

    if is_spotify:
        command = [
            'spotdl',
            '--output', f"{temp_download_folder}/{{artist}}/{{album}}/{{title}}.{{output-ext}}",
            spotify_link
        ]
    else:
        command = [
            'yt-dlp', '-x', '--audio-format', 'mp3',
            '-o', f"{temp_download_folder}/%(uploader)s/%(album)s/%(title)s.%(ext)s",
            spotify_link
        ]

    is_admin = is_logged_in()
    return Response(generate(is_admin, command, temp_download_folder, session_id), mimetype='text/event-stream')

def generate(is_admin, command, temp_download_folder, session_id):
    album_name = None
    try:
        print(f"🎧 Command being run: {' '.join(command)}")
        print(f"📁 Temp download folder: {temp_download_folder}")

        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        for line in process.stdout:
            print(f"▶️ {line.strip()}")
            yield f"data: {line.strip()}\n\n"

            # Capture album name for zipping later
            match = re.search(r'Found \d+ songs in (.+?) \(', line)
            if match:
                album_name = match.group(1).strip()

        process.stdout.close()
        process.wait()

        if process.returncode != 0:
            yield f"data: Error: Download exited with code {process.returncode}.\n\n"
            return

        # Gather all downloaded audio files
        downloaded_files = []
        for root, _, files in os.walk(temp_download_folder):
            for file in files:
                full_path = os.path.join(root, file)
                print(f"📄 Found file: {full_path}")
                downloaded_files.append(full_path)

        valid_audio_files = [f for f in downloaded_files if f.lower().endswith(('.mp3', '.m4a', '.flac', '.wav', '.ogg'))]

        if not valid_audio_files:
            yield f"data: Error: No valid audio files found. Please check the link.\n\n"
            return

        # ✅ ADMIN HANDLING
        if is_admin:
            for file_path in valid_audio_files:
                filename = os.path.basename(file_path)

                # Check for duplicate before moving
                if download_history and download_history.is_duplicate(file_path):
                    artist, title = None, None
                    try:
                        ext = os.path.splitext(file_path)[1].lower()
                        if ext == '.mp3':
                            audio = MP3(file_path)
                        elif ext == '.flac':
                            audio = FLAC(file_path)
                        elif ext in ('.m4a', '.aac'):
                            audio = M4A(file_path)
                        if 'title' in audio and audio['title']:
                            title = str(audio['title'][0])
                        if 'artist' in audio and audio['artist']:
                            artist = str(audio['artist'][0])
                    except Exception:
                        pass
                    track_name = f"{artist} - {title}" if artist and title else filename
                    print(f"⏭️ Duplicate skipped: {track_name}")
                    yield f"data: ⏭️ Duplicate skipped: {track_name}\n\n"
                    continue

                if 'General Conference' in filename and '｜' in filename:
                    speaker_name = filename.split('｜')[0].strip()
                    target_path = os.path.join(ADMIN_DOWNLOAD_PATH, speaker_name, filename)
                    print(f"🚚 Moving GC file to: {target_path}")
                else:
                    relative_path = os.path.relpath(file_path, temp_download_folder)
                    target_path = os.path.join(ADMIN_DOWNLOAD_PATH, relative_path)
                    print(f"🚚 Moving to default admin path: {target_path}")

                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                try:
                    shutil.move(file_path, target_path)
                    if download_history:
                        download_history.add_file(target_path)
                except Exception as move_error:
                    print(f"❌ Failed to move {file_path} to {target_path}: {move_error}")


            shutil.rmtree(temp_download_folder, ignore_errors=True)
            yield "data: Download completed. Files saved to server directory.\n\n"
            return  # ✅ Don’t try to serve/move anything else

        # ✅ PUBLIC USER HANDLING
        if len(valid_audio_files) > 1:
            zip_filename = f"{album_name}.zip" if album_name else "playlist.zip"
            zip_path = os.path.join(temp_download_folder, zip_filename)
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for file_path in valid_audio_files:
                    arcname = os.path.relpath(file_path, start=temp_download_folder)
                    zipf.write(file_path, arcname=arcname)

            yield f"data: DOWNLOAD: {session_id}/{zip_filename}\n\n"

            # Schedule cleanup of the temp folder (including zip)
            threading.Thread(target=delayed_delete, args=(temp_download_folder,)).start()

        else:
            from urllib.parse import quote
            relative_path = os.path.relpath(valid_audio_files[0], start=temp_download_folder)
            encoded_path = quote(relative_path)
            yield f"data: DOWNLOAD: {session_id}/{encoded_path}\n\n"

            # Schedule cleanup of the temp folder
            threading.Thread(target=delayed_delete, args=(temp_download_folder,)).start()

    except Exception as e:
        yield f"data: Error: {str(e)}\n\n"


def delayed_delete(folder_path):
    time.sleep(300)
    shutil.rmtree(folder_path, ignore_errors=True)

def emergency_cleanup_container_downloads():
    print("🚨 Running backup cleanup in /app/downloads")
    current_time = time.time()
    for folder in os.listdir(BASE_DOWNLOAD_FOLDER):
        folder_path = os.path.join(BASE_DOWNLOAD_FOLDER, folder)
        try:
            # 检查文件夹的修改时间
            folder_mtime = os.path.getmtime(folder_path)
            # 如果文件夹修改时间在一定时间内，则跳过清理
            if current_time - folder_mtime < CLEANUP_PROTECTION_TIME:
                print(f"⏭️ Skipping recently modified folder: {folder_path}, mtime: {folder_mtime}")
                continue

            shutil.rmtree(folder_path)
            print(f"🗑️ Cleaned: {folder_path}")
        except Exception as e:
            print(f"⚠️ Could not delete {folder_path}: {e}")

def schedule_emergency_cleanup(interval_seconds=3600):
    def loop():
        while True:
            time.sleep(interval_seconds)
            emergency_cleanup_container_downloads()

    threading.Thread(target=loop, daemon=True).start()

@app.route('/set-download-path', methods=['POST'])
def set_download_path():
    global ADMIN_DOWNLOAD_PATH, download_history
    if not is_logged_in():
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    data = request.get_json()
    new_path = data.get('path')

    if not new_path:
        return jsonify({"success": False, "message": "Path cannot be empty."}), 400

    # Optional: Validate the path, ensure it exists
    if not os.path.isdir(new_path):
        try:
            os.makedirs(new_path, exist_ok=True)
        except Exception as e:
            return jsonify({"success": False, "message": f"Cannot create path: {str(e)}"}), 500

    ADMIN_DOWNLOAD_PATH = new_path
    # Update history target and rescan
    download_history = DownloadHistory(ADMIN_DOWNLOAD_PATH, scan_interval_seconds=history_scan_interval)
    download_history.start_background_scan()
    return jsonify({"success": True, "new_path": ADMIN_DOWNLOAD_PATH})


@app.route('/downloads/<session_id>/<path:filename>')
def serve_download(session_id, filename):
    session_download_folder = os.path.join(BASE_DOWNLOAD_FOLDER, session_id)
    full_path = os.path.join(session_download_folder, filename)

    print(f"📥 Requested filename: {filename}")
    print(f"📁 Resolved full path: {full_path}")

    if ".." in filename or filename.startswith("/"):
        return "Invalid filename", 400

    if not os.path.isfile(full_path):
        print("❌ File does not exist!")
        return "File not found", 404

    return send_from_directory(session_download_folder, filename, as_attachment=True)

schedule_emergency_cleanup()
threading.Thread(target=session_cleanup_loop, daemon=True).start()
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

