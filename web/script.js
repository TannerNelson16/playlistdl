console.log("Audio Downloader script initialized.");

let activeEventSource = null;
let pollingInterval = null;

function setProgress(percent, text) {
    const progressSection = document.getElementById('progressSection');
    const progressBar = document.getElementById('progress');
    const statusText = document.getElementById('statusText');

    if (percent === null) {
        progressSection.style.display = 'none';
        return;
    }

    progressSection.style.display = 'block';
    if (percent !== undefined && percent !== null) {
        progressBar.value = percent;
    }
    if (text) {
        statusText.innerText = text;
    }
}

function appendLog(log) {
    const logsElement = document.getElementById('logs');
    if (!logsElement) return;

    const line = document.createElement('div');
    line.innerText = log;
    logsElement.appendChild(line);
    logsElement.scrollTop = logsElement.scrollHeight;

    const clearBtn = document.getElementById('clearLogsBtn');
    if (clearBtn) clearBtn.style.display = 'inline-block';
}

function clearLogs() {
    const logsElement = document.getElementById('logs');
    if (logsElement) logsElement.innerHTML = '';
    const clearBtn = document.getElementById('clearLogsBtn');
    if (clearBtn) clearBtn.style.display = 'none';
}

function showDownloadLink(path) {
    const resultElement = document.getElementById('result');
    resultElement.innerHTML = "";

    const linkWrapper = document.createElement('div');
    linkWrapper.style.margin = "15px 0";

    const downloadLink = document.createElement('a');
    downloadLink.href = `/downloads/${path}`;
    const rawFilename = path.split('/').pop();
    const cleanFilename = decodeURIComponent(rawFilename);
    downloadLink.download = cleanFilename;
    downloadLink.innerText = `💾 Download: ${cleanFilename}`;
    downloadLink.className = "download-button";

    linkWrapper.appendChild(downloadLink);
    resultElement.appendChild(linkWrapper);
}

function showNewDownloadButton() {
    const resultElement = document.getElementById('result');
    if (document.getElementById('newDownloadBtn')) return;

    const newBtn = document.createElement('button');
    newBtn.id = 'newDownloadBtn';
    newBtn.innerText = "Start New Download";
    newBtn.style.marginTop = "10px";
    newBtn.style.backgroundColor = "#2563eb";
    newBtn.onclick = function() {
        if (activeEventSource) {
            activeEventSource.close();
            activeEventSource = null;
        }
        if (pollingInterval) {
            clearInterval(pollingInterval);
            pollingInterval = null;
        }

        const activeSessionId = localStorage.getItem('active_session_id');
        if (activeSessionId) {
            localStorage.removeItem('active_session_id');
            localStorage.removeItem('downloaded_' + activeSessionId);
        }

        document.getElementById('result').innerHTML = "";
        clearLogs();
        setProgress(null);
        document.getElementById('spotifyLink').value = "";
        document.getElementById('downloadBtn').disabled = false;
        document.getElementById('downloadBtn').innerText = "Download";
    };

    resultElement.appendChild(newBtn);
}

async function checkActiveSession() {
    const activeSessionId = localStorage.getItem('active_session_id');
    if (!activeSessionId) return;

    try {
        const response = await fetch(`/status/${activeSessionId}`);
        if (response.status === 404) {
            localStorage.removeItem('active_session_id');
            return;
        }
        const data = await response.json();

        // Restore logs
        const logsElement = document.getElementById('logs');
        logsElement.innerHTML = "";
        if (data.logs && Array.isArray(data.logs)) {
            data.logs.forEach(log => appendLog(log));
        }

        if (data.status === 'running') {
            document.getElementById('downloadBtn').disabled = true;
            document.getElementById('downloadBtn').innerText = "Downloading...";
            
            let statusMsg = "Download in progress...";
            if (data.total_songs > 0) {
                statusMsg = `Downloading track ${data.downloaded_songs || 0} of ${data.total_songs} (${data.progress || 0}%)`;
            } else if (data.progress > 0) {
                statusMsg = `Downloading... ${data.progress}%`;
            }
            setProgress(data.progress || 10, statusMsg);

            connectToStream(activeSessionId);
        } else if (data.status === 'completed') {
            setProgress(100, "Download completed!");
            document.getElementById('downloadBtn').disabled = false;
            document.getElementById('downloadBtn').innerText = "Download";

            if (data.download_path) {
                showDownloadLink(data.download_path);
            } else if (data.completed_message) {
                const msgDiv = document.createElement('div');
                msgDiv.style.color = '#4ade80';
                msgDiv.style.fontWeight = 'bold';
                msgDiv.style.margin = '15px 0';
                msgDiv.innerText = data.completed_message;
                document.getElementById('result').appendChild(msgDiv);
            }
            showNewDownloadButton();
        } else if (data.status === 'failed') {
            setProgress(null);
            document.getElementById('downloadBtn').disabled = false;
            document.getElementById('downloadBtn').innerText = "Download";

            const resultElement = document.getElementById('result');
            resultElement.innerHTML = `<div style="color: #f87171; font-weight: bold; margin: 15px 0;">❌ ${data.error || 'Download failed.'}</div>`;
            showNewDownloadButton();
        }
    } catch (e) {
        console.error("Error checking active session:", e);
    }
}

function connectToStream(sessionId) {
    if (activeEventSource) {
        activeEventSource.close();
    }

    let totalSongs = 0;
    let downloadedSongs = 0;

    const eventSource = new EventSource(`/stream/${sessionId}`);
    activeEventSource = eventSource;

    eventSource.onmessage = function(event) {
        const log = event.data;
        if (!log) return;

        appendLog(log);

        // Check total tracks from logs
        const matchTotal = log.match(/Found (\d+) songs/);
        if (matchTotal) {
            totalSongs = parseInt(matchTotal[1], 10);
            setProgress(10, `Found ${totalSongs} songs. Starting download...`);
        }

        // Check downloaded tracks from logs
        if (log.includes("Downloaded \"")) {
            downloadedSongs++;
            const songNameMatch = log.match(/Downloaded "(.+?)"/);
            const songTitle = songNameMatch ? songNameMatch[1] : "";
            const pct = totalSongs > 0 ? Math.min(Math.round((downloadedSongs / totalSongs) * 98), 98) : 50;
            setProgress(pct, `Downloaded ${downloadedSongs}/${totalSongs || '?'} songs (${pct}%) ${songTitle ? '- ' + songTitle : ''}`);
        }

        // Percentage matching for single yt-dlp download
        const matchPct = log.match(/\[download\]\s+([0-9\.]+)%/);
        if (matchPct) {
            const pct = Math.min(Math.round(parseFloat(matchPct[1])), 98);
            setProgress(pct, `Downloading... ${pct}%`);
        }

        // Completed for public users (zip / direct file)
        if (log.startsWith("DOWNLOAD:")) {
            setProgress(100, "Download completed!");
            const path = log.replace("DOWNLOAD: ", "").trim();
            console.log("Download path received:", path);

            showDownloadLink(path);
            showNewDownloadButton();
            document.getElementById('downloadBtn').disabled = false;
            document.getElementById('downloadBtn').innerText = "Download";

            // Auto-click the link if it just finished and we haven't clicked it yet
            const downloadLink = document.querySelector('#result a.download-button');
            if (downloadLink && !localStorage.getItem('downloaded_' + sessionId)) {
                localStorage.setItem('downloaded_' + sessionId, 'true');
                downloadLink.click();
            }

            eventSource.close();
            activeEventSource = null;
        }
        // Completed for server / admin direct downloads
        else if (log.includes("Download completed") || log.includes("saved directly to server directory") || log.includes("Files saved to server directory")) {
            setProgress(100, "Download completed and saved to server!");
            document.getElementById('downloadBtn').disabled = false;
            document.getElementById('downloadBtn').innerText = "Download";
            showNewDownloadButton();
            eventSource.close();
            activeEventSource = null;
        }
        // Fatal errors
        else if (log.startsWith("ERROR: Download failed") || log.startsWith("ERROR: No valid audio files found")) {
            setProgress(null);
            document.getElementById('downloadBtn').disabled = false;
            document.getElementById('downloadBtn').innerText = "Download";
            const resultElement = document.getElementById('result');
            resultElement.innerHTML = `<div style="color: #f87171; font-weight: bold; margin: 15px 0;">❌ ${log.replace('ERROR: ', '')}</div>`;
            showNewDownloadButton();
            eventSource.close();
            activeEventSource = null;
        }
    };

    eventSource.onerror = function() {
        console.log("EventSource connection disconnected or ended. Polling for final status...");
        // Start polling fallback in case stream closed before final status or tab re-opened
        if (!pollingInterval) {
            pollingInterval = setInterval(async () => {
                try {
                    const res = await fetch(`/status/${sessionId}`);
                    if (res.status === 200) {
                        const jobData = await res.json();
                        if (jobData.status === 'completed' || jobData.status === 'failed') {
                            clearInterval(pollingInterval);
                            pollingInterval = null;
                            if (activeEventSource) {
                                activeEventSource.close();
                                activeEventSource = null;
                            }
                            checkActiveSession();
                        }
                    } else if (res.status === 404) {
                        clearInterval(pollingInterval);
                        pollingInterval = null;
                    }
                } catch (err) {
                    console.error("Polling error:", err);
                }
            }, 3000);
        }
    };
}

async function download() {
    const linkInput = document.getElementById('spotifyLink');
    const spotifyLink = linkInput.value.trim();

    if (!spotifyLink) {
        document.getElementById('result').innerHTML = '<div style="color: #f87171; margin-top: 10px;">Please enter a Spotify or YouTube link.</div>';
        return;
    }

    clearLogs();
    document.getElementById('result').innerHTML = "";
    setProgress(5, "Connecting to server...");

    const downloadBtn = document.getElementById('downloadBtn');
    downloadBtn.disabled = true;
    downloadBtn.innerText = "Downloading...";

    try {
        const response = await fetch('/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ spotify_link: spotifyLink })
        });
        const data = await response.json();

        if (data.status === 'success' && data.session_id) {
            localStorage.setItem('active_session_id', data.session_id);
            localStorage.removeItem('downloaded_' + data.session_id);
            connectToStream(data.session_id);
        } else {
            setProgress(null);
            downloadBtn.disabled = false;
            downloadBtn.innerText = "Download";
            document.getElementById('result').innerHTML = `<div style="color: #f87171; margin-top: 10px;">Error starting download: ${data.message || 'Unknown error'}</div>`;
        }
    } catch (e) {
        setProgress(null);
        downloadBtn.disabled = false;
        downloadBtn.innerText = "Download";
        document.getElementById('result').innerHTML = `<div style="color: #f87171; margin-top: 10px;">Failed to connect to server: ${e.message}</div>`;
    }
}

// ----------------- Admin Auth & Controls -----------------
function handleAdminButton() {
    const btn = document.getElementById('adminButton');
    if (btn.innerText === "Admin") {
        showLoginModal();
    } else {
        logout();
    }
}

function showLoginModal() {
    const loginModal = document.getElementById('loginModal');
    loginModal.classList.add('show');
}

function closeLoginModal() {
    const loginModal = document.getElementById('loginModal');
    loginModal.classList.remove('show');
    document.getElementById('loginMessage').innerText = "";
}

async function loadDownloadOptions() {
    try {
        const response = await fetch('/download-options');
        const data = await response.json();
        if (data.success) {
            const select = document.getElementById('downloadPathSelect');
            const input = document.getElementById('downloadPath');
            const button = document.getElementById('setPathButton');

            select.innerHTML = '';
            const options = data.options || [];
            const currentPath = data.current_path;

            if (options.length > 0) {
                options.forEach(opt => {
                    const el = document.createElement('option');
                    el.value = opt.path;
                    el.innerText = opt.label;
                    select.appendChild(el);
                });

                const customEl = document.createElement('option');
                customEl.value = 'custom';
                customEl.innerText = 'Custom Path...';
                select.appendChild(customEl);

                select.style.display = 'inline-block';

                const matchingOpt = options.find(opt => opt.path === currentPath);
                if (matchingOpt) {
                    select.value = currentPath;
                    input.style.display = 'none';
                    button.style.display = 'none';
                } else {
                    select.value = 'custom';
                    input.value = currentPath || '';
                    input.style.display = 'inline-block';
                    button.style.display = 'inline-block';
                }
            } else {
                select.style.display = 'none';
                input.value = currentPath || '';
                input.style.display = 'inline-block';
                button.style.display = 'inline-block';
            }
        }
    } catch (e) {
        console.error("Error loading download options:", e);
    }
}

async function handlePathSelectChange() {
    const select = document.getElementById('downloadPathSelect');
    const input = document.getElementById('downloadPath');
    const button = document.getElementById('setPathButton');
    const messageDiv = document.getElementById('pathMessage');

    const val = select.value;
    if (val === 'custom') {
        input.style.display = 'inline-block';
        button.style.display = 'inline-block';
        messageDiv.innerText = '';
    } else {
        input.style.display = 'none';
        button.style.display = 'none';
        input.value = val;

        messageDiv.innerText = "Saving path...";
        messageDiv.style.color = "yellow";

        try {
            const response = await fetch('/set-download-path', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path: val})
            });
            const data = await response.json();
            if (data.success) {
                messageDiv.innerText = `Download path set to: ${data.new_path}`;
                messageDiv.style.color = "#4ade80";
            } else {
                messageDiv.innerText = `Error: ${data.message}`;
                messageDiv.style.color = "#f87171";
            }
        } catch (e) {
            messageDiv.innerText = `Error saving path: ${e}`;
            messageDiv.style.color = "#f87171";
        }
    }
}

async function checkLoginStatus() {
    try {
        const response = await fetch('/check-login');
        const data = await response.json();
        const adminButton = document.getElementById('adminButton');
        const adminMessage = document.getElementById('adminMessage');
        const adminControls = document.getElementById('adminControls');

        if (data.loggedIn) {
            adminButton.innerText = "Log Out";
            adminMessage.style.display = "block";
            adminControls.style.display = "block";
            await loadDownloadOptions();
        } else {
            adminButton.innerText = "Admin";
            adminMessage.style.display = "none";
            adminControls.style.display = "none";
        }
    } catch (e) {
        console.error("Error checking login status:", e);
    }
}

async function logout() {
    try {
        const response = await fetch('/logout', { method: 'POST' });
        const data = await response.json();
        if (data.success) {
            await checkLoginStatus();
        }
    } catch (e) {
        console.error("Error logging out:", e);
    }
}

async function login() {
    const usernameInput = document.getElementById('username');
    const passwordInput = document.getElementById('password');
    const loginMessage = document.getElementById('loginMessage');

    const username = usernameInput.value.trim();
    const password = passwordInput.value.trim();

    try {
        const response = await fetch('/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password })
        });

        const data = await response.json();
        if (data.success) {
            loginMessage.innerText = "Login successful!";
            loginMessage.style.color = "#4ade80";
            setTimeout(() => {
                closeLoginModal();
                checkLoginStatus();
            }, 400);
        } else {
            loginMessage.innerText = data.message || "Login failed. Check credentials.";
            loginMessage.style.color = "#f87171";
        }
    } catch (e) {
        loginMessage.innerText = `Login error: ${e.message}`;
        loginMessage.style.color = "#f87171";
    }
}

async function setDownloadPath() {
    const path = document.getElementById('downloadPath').value.trim();
    const messageDiv = document.getElementById('pathMessage');

    if (!path) {
        messageDiv.innerText = "Path cannot be empty.";
        messageDiv.style.color = "#f87171";
        return;
    }

    try {
        const response = await fetch('/set-download-path', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({path})
        });

        const data = await response.json();
        if (data.success) {
            messageDiv.innerText = `Download path set successfully to: ${data.new_path}`;
            messageDiv.style.color = "#4ade80";
            await loadDownloadOptions();
        } else {
            messageDiv.innerText = `Error: ${data.message}`;
            messageDiv.style.color = "#f87171";
        }
    } catch (e) {
        messageDiv.innerText = `Error saving path: ${e}`;
        messageDiv.style.color = "#f87171";
    }
}

// Window load init
window.onload = async function() {
    await checkLoginStatus();
    checkActiveSession();
};
