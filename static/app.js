// SDR Monitor Dashboard — Multi-channel WebSocket client, UI updates, and settings modal
// Modal open/close/tabs are defined inline in index.html (before this file loads)

(function() {
    "use strict";

    // ── Channel state ────────────────────────────────────
    var channels = [];           // list from /api/channels
    var currentChannelId = null;
    var channelDcsCode = "---";

    // ── Runtime state (from /api/runtime) ────────────────
    var runtimeState = null;     // fetched on load + modal open

    // ── Elements ───────────────────────────────────────
    const channelSelector = document.getElementById("channelSelector");
    const channelName = document.getElementById("channelName");
    const squelchInd = document.getElementById("squelchIndicator");
    const dcsInd = document.getElementById("dcsIndicator");
    const dcsLabel = document.getElementById("dcsLabel");
    const recInd = document.getElementById("recIndicator");
    const stateLabel = document.getElementById("stateLabel");
    const txCount = document.getElementById("txCount");
    const dcsStats = document.getElementById("dcsStats");
    const dcsMatchRate = document.getElementById("dcsMatchRate");
    const connStatus = document.getElementById("connStatus");
    const meterBar = document.getElementById("meterBar");
    const meterThreshold = document.getElementById("meterThreshold");
    const freqLabel = document.getElementById("freqLabel");
    const rssiValue = document.getElementById("rssiValue");
    const rssiUnit = document.getElementById("rssiUnit");
    const txLogBody = document.getElementById("txLogBody");
    const squelchSlider = document.getElementById("squelchSlider");
    const thresholdLabel = document.getElementById("thresholdLabel");
    const gainSlider = document.getElementById("gainSlider");
    const gainValue = document.getElementById("gainValue");
    const audioToggle = document.getElementById("audioToggle");
    const audioStatus = document.getElementById("audioStatus");
    const infoFreq = document.getElementById("infoFreq");
    const infoDcs = document.getElementById("infoDcs");
    const infoDcsMatch = document.getElementById("infoDcsMatch");
    const infoDcsTotal = document.getElementById("infoDcsTotal");
    const infoDcsRate = document.getElementById("infoDcsRate");
    const gainRow = document.getElementById("gainRow");
    const gainLockLabel = document.getElementById("gainLockLabel");
    const squelchLockLabel = document.getElementById("squelchLockLabel");

    // ── Channel selector ─────────────────────────────────
    function renderChannelTabs() {
        channelSelector.innerHTML = "";
        channels.forEach(function(ch) {
            var tab = document.createElement("button");
            tab.className = "channel-tab" + (ch.id === currentChannelId ? " active" : "");
            tab.textContent = ch.name;
            tab.dataset.channelId = ch.id;
            tab.addEventListener("click", function() {
                if (ch.id !== currentChannelId) {
                    switchChannel(ch.id);
                }
            });
            channelSelector.appendChild(tab);
        });

        // Show a "+" tab if there's room for more channels
        var maxCh = runtimeState && runtimeState.receiver ? runtimeState.receiver.max_channels : 2;
        if (channels.length < maxCh) {
            var addTab = document.createElement("button");
            addTab.className = "channel-tab channel-tab-add";
            addTab.textContent = "+";
            addTab.title = "Add channel";
            addTab.addEventListener("click", function() {
                window.SDR.openSettings(true);
            });
            channelSelector.appendChild(addTab);
        }
    }

    function switchChannel(channelId) {
        currentChannelId = channelId;
        lastTxCount = 0;

        // Update tab highlights
        var tabs = channelSelector.querySelectorAll(".channel-tab");
        tabs.forEach(function(tab) {
            tab.classList.toggle("active", tab.dataset.channelId === channelId);
        });

        // Reset UI state
        squelchSlider._userSet = false;
        gainSlider._userSet = false;
        dcsStats.style.display = "none";

        // Load channel info
        loadChannelConfig(channelId);

        // Load channel config (squelch/gain)
        fetch("/api/channels/" + channelId + "/config")
            .then(function(r) { return r.json(); })
            .then(function(cfg) {
                if (cfg.squelch_threshold !== undefined) {
                    squelchSlider.value = cfg.squelch_threshold;
                    thresholdLabel.textContent = cfg.squelch_threshold.toFixed(1);
                    var threshPct = Math.max(0, Math.min(100, (cfg.squelch_threshold - RSSI_MIN) / (RSSI_MAX - RSSI_MIN) * 100));
                    meterThreshold.style.left = threshPct + "%";
                }
                if (cfg.gain !== undefined) {
                    var gPct = ((cfg.gain - GAIN_MIN) / (GAIN_MAX - GAIN_MIN)) * 100;
                    setGainFromPct(gPct);
                }
            })
            .catch(function() {});

        // Reconnect telemetry WS
        connectWS();

        // Reconnect audio WS if playing
        if (audioPlaying) {
            connectAudioWs();
        }

        // Fetch TX log
        fetchTxLog();

        // Re-apply lock state for this channel
        applyLockState();
    }

    // ── Fetch channel config ────────────────────────────
    function loadChannelConfig(channelId) {
        fetch("/api/channels/" + channelId)
            .then(function(r) { return r.json(); })
            .then(function(ch) {
                var name = ch.name || "SDR Monitor";
                var freqMhz = (ch.freq_hz / 1e6).toFixed(3);
                var dcsCode = String(ch.dcs_code).padStart(3, "0");
                channelDcsCode = dcsCode;

                document.title = name;
                channelName.textContent = name;
                freqLabel.textContent = freqMhz;
                dcsLabel.textContent = "DPL " + dcsCode;
                infoFreq.textContent = freqMhz;
                infoDcs.textContent = "DPL " + dcsCode;
            })
            .catch(function() {});
    }

    // ── Runtime state fetch + control locking ────────────
    function fetchRuntime() {
        return fetch("/api/runtime")
            .then(function(r) { return r.json(); })
            .then(function(data) {
                runtimeState = data;
                applyLockState();
                return data;
            })
            .catch(function() { return null; });
    }

    function applyLockState() {
        if (!runtimeState) return;

        var eff = runtimeState.effective_settings || {};
        var chRt = runtimeState.channel_runtime || {};

        // Gain lock
        var gainInfo = eff.gain || {};
        if (gainInfo.locked) {
            gainRow.classList.add("locked");
            gainLockLabel.style.display = "";
        } else {
            gainRow.classList.remove("locked");
            gainLockLabel.style.display = "none";
        }

        // Squelch lock (per-channel)
        var chSq = (chRt[currentChannelId] || {}).squelch || {};
        if (chSq.locked) {
            meterThreshold.classList.add("locked");
            squelchLockLabel.style.display = "";
        } else {
            meterThreshold.classList.remove("locked");
            squelchLockLabel.style.display = "none";
        }
    }

    // ── Telemetry WebSocket ────────────────────────────
    let ws = null;
    let reconnectTimer = null;
    let lastTxCount = 0;

    function connectWS() {
        // Close existing connection
        if (ws) {
            ws.onclose = null;
            ws.onerror = null;
            ws.close();
            ws = null;
        }
        if (reconnectTimer) {
            clearTimeout(reconnectTimer);
            reconnectTimer = null;
        }

        if (!currentChannelId) return;

        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        ws = new WebSocket(proto + "//" + location.host + "/ws/" + currentChannelId);

        ws.onopen = function() {
            connStatus.textContent = "connected";
            connStatus.className = "status-item conn-status connected";
        };

        ws.onmessage = function(ev) {
            try {
                const data = JSON.parse(ev.data);
                if (data.type === "telemetry") {
                    updateUI(data);
                }
            } catch(e) {}
        };

        ws.onclose = function() {
            connStatus.textContent = "disconnected";
            connStatus.className = "status-item conn-status disconnected";
            scheduleReconnect();
        };

        ws.onerror = function() {
            ws.close();
        };
    }

    function scheduleReconnect() {
        if (!reconnectTimer) {
            reconnectTimer = setTimeout(function() {
                reconnectTimer = null;
                connectWS();
            }, 2000);
        }
    }

    function sendConfig(key, value) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            const msg = {};
            msg[key] = value;
            ws.send(JSON.stringify(msg));
        }
    }

    // ── UI Update ──────────────────────────────────────
    // RSSI meter range
    const RSSI_MIN = -70;
    const RSSI_MAX = -5;
    var GAIN_MIN = 0, GAIN_MAX = 50;

    function updateUI(d) {
        // RSSI meter
        var rssi = (d.rssi != null) ? d.rssi : -100;
        var pct = Math.max(0, Math.min(100, (rssi - RSSI_MIN) / (RSSI_MAX - RSSI_MIN) * 100));
        meterBar.style.width = pct + "%";

        if (d.squelch_open) {
            meterBar.className = "meter-bar high";
        } else if (rssi > -40) {
            meterBar.className = "meter-bar mid";
        } else {
            meterBar.className = "meter-bar low";
        }

        rssiValue.textContent = rssi.toFixed(1);

        // Squelch threshold marker — skip if user is actively adjusting
        if (!squelchSlider._userSet) {
            var thresh = (d.squelch_threshold != null) ? d.squelch_threshold : -30;
            var threshPct = Math.max(0, Math.min(100, (thresh - RSSI_MIN) / (RSSI_MAX - RSSI_MIN) * 100));
            meterThreshold.style.left = threshPct + "%";
        }

        // Indicators
        squelchInd.className = "indicator " + (d.squelch_open ? "squelch-open" : "squelch-closed");
        dcsInd.className = "indicator " + (d.dcs_detected ? "dcs-on" : "dcs-off");
        recInd.className = "indicator " + (d.recording ? "rec-on" : "rec-off");

        // State
        var state = d.state || "IDLE";
        stateLabel.textContent = state;
        stateLabel.className = "state-label state-" + state;

        // TX count — fetch log when it changes
        var count = (d.tx_count != null) ? d.tx_count : 0;
        txCount.textContent = count;
        if (d.tx_count > lastTxCount) {
            lastTxCount = d.tx_count;
            fetchTxLog();
        }

        // DCS match stats
        if (d.tx_count > 0) {
            dcsStats.style.display = "";
            dcsMatchRate.textContent = (d.dcs_match_rate != null) ? d.dcs_match_rate : 0;
            infoDcsMatch.textContent = (d.tx_with_dcs_match != null) ? d.tx_with_dcs_match : 0;
            infoDcsTotal.textContent = (d.tx_count != null) ? d.tx_count : 0;
            infoDcsRate.textContent = (d.dcs_match_rate != null) ? d.dcs_match_rate : 0;
        }

        // Sync slider values from server on first update
        if (d.squelch_threshold !== undefined && !squelchSlider._userSet) {
            squelchSlider.value = d.squelch_threshold;
            thresholdLabel.textContent = d.squelch_threshold.toFixed(1);
        }
        if (d.gain !== undefined && !gainSlider._userSet) {
            var gPct = ((d.gain - GAIN_MIN) / (GAIN_MAX - GAIN_MIN)) * 100;
            setGainFromPct(gPct);
        }
    }

    // ── SVG Icons ─────────────────────────────────────
    var ICO_PLAY = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><polygon points="6,3 20,12 6,21"/></svg>';
    var ICO_PAUSE = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="3" width="4" height="18" rx="1"/><rect x="15" y="3" width="4" height="18" rx="1"/></svg>';
    var ICO_DL = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0l-4-4m4 4l4-4"/><path d="M5 19h14"/></svg>';
    var ICO_TRASH = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>';

    // ── Recording Playback ──────────────────────────────
    var currentAudioEl = null;
    var currentPlayBtn = null;

    function playRecording(btn) {
        // If same button clicked again, toggle pause/play
        if (currentPlayBtn === btn && currentAudioEl) {
            if (currentAudioEl.paused) {
                currentAudioEl.play();
                btn.innerHTML = ICO_PAUSE;
                btn.classList.add("playing");
            } else {
                currentAudioEl.pause();
                btn.innerHTML = ICO_PLAY;
                btn.classList.remove("playing");
            }
            return;
        }

        // Stop any currently playing
        if (currentAudioEl) {
            currentAudioEl.pause();
            currentAudioEl = null;
            if (currentPlayBtn) {
                currentPlayBtn.innerHTML = ICO_PLAY;
                currentPlayBtn.classList.remove("playing");
            }
        }

        var audio = new Audio(btn.dataset.url);
        currentAudioEl = audio;
        currentPlayBtn = btn;
        btn.innerHTML = ICO_PAUSE;
        btn.classList.add("playing");

        audio.addEventListener("ended", function() {
            btn.innerHTML = ICO_PLAY;
            btn.classList.remove("playing");
            currentAudioEl = null;
            currentPlayBtn = null;
        });

        audio.addEventListener("error", function() {
            btn.innerHTML = ICO_PLAY;
            btn.classList.remove("playing");
            currentAudioEl = null;
            currentPlayBtn = null;
        });

        audio.play();
    }

    // ── TX Log ─────────────────────────────────────────
    function fetchTxLog() {
        if (!currentChannelId) return;
        fetch("/api/channels/" + currentChannelId + "/transmissions")
            .then(function(r) { return r.json(); })
            .then(function(log) {
                txLogBody.innerHTML = "";
                // Show newest first
                for (let i = log.length - 1; i >= 0; i--) {
                    const e = log[i];
                    const tr = document.createElement("tr");

                    const tdTime = document.createElement("td");
                    tdTime.textContent = e.time || "";
                    tr.appendChild(tdTime);

                    const tdDur = document.createElement("td");
                    tdDur.textContent = (e.duration != null) ? e.duration.toFixed(1) + "s" : "";
                    tr.appendChild(tdDur);

                    const tdRssi = document.createElement("td");
                    tdRssi.textContent = (e.peak_rssi != null) ? e.peak_rssi.toFixed(1) : "";
                    tr.appendChild(tdRssi);

                    const tdDcs = document.createElement("td");
                    const badge = document.createElement("span");
                    badge.className = "dcs-badge " + (e.dcs_confirmed ? "confirmed" : "unconfirmed");
                    badge.textContent = e.dcs_confirmed ? (channelDcsCode + (e.dcs_polarity ? e.dcs_polarity : "")) : "---";
                    tdDcs.appendChild(badge);
                    tr.appendChild(tdDcs);

                    const tdAudio = document.createElement("td");
                    if (e.filename) {
                        const fname = e.filename.split("/").pop();
                        const url = "/audio/" + currentChannelId + "/" + encodeURIComponent(fname);

                        var playBtn = document.createElement("button");
                        playBtn.className = "play-btn";
                        playBtn.innerHTML = ICO_PLAY;
                        playBtn.title = "Play";
                        playBtn.dataset.url = url;
                        playBtn.addEventListener("click", function() {
                            playRecording(this);
                        });
                        tdAudio.appendChild(playBtn);

                        var dlLink = document.createElement("a");
                        dlLink.className = "audio-link dl-link";
                        dlLink.href = url;
                        dlLink.download = fname;
                        dlLink.innerHTML = ICO_DL;
                        dlLink.title = "Download";
                        tdAudio.appendChild(dlLink);
                    }

                    var delBtn = document.createElement("button");
                    delBtn.className = "del-btn";
                    delBtn.innerHTML = ICO_TRASH;
                    delBtn.title = "Delete";
                    delBtn.dataset.index = i;
                    delBtn.addEventListener("click", function() {
                        var idx = parseInt(this.dataset.index);
                        if (!confirm("Delete this transmission entry" + (log[idx].filename ? " and its recording?" : "?"))) return;
                        fetch("/api/channels/" + currentChannelId + "/transmissions/" + idx, { method: "DELETE" })
                            .then(function(r) { return r.json(); })
                            .then(function() { fetchTxLog(); });
                    });
                    tdAudio.appendChild(delBtn);

                    tr.appendChild(tdAudio);

                    txLogBody.appendChild(tr);
                }
            })
            .catch(function() {});
    }

    // ── Controls ───────────────────────────────────────
    // ── Squelch threshold drag on meter bar ────────────
    var squelchDebounce = null;
    var meterContainer = document.getElementById("meterContainer");

    function pctToDb(pct) {
        return RSSI_MIN + (pct / 100) * (RSSI_MAX - RSSI_MIN);
    }

    function posToDb(clientX) {
        var rect = meterContainer.getBoundingClientRect();
        var pct = Math.max(0, Math.min(100, (clientX - rect.left) / rect.width * 100));
        // Snap to 0.5 dB steps
        var db = pctToDb(pct);
        return Math.round(db * 2) / 2;
    }

    function isSquelchLocked() {
        if (!runtimeState || !currentChannelId) return false;
        var chRt = (runtimeState.channel_runtime || {})[currentChannelId];
        return chRt && chRt.squelch && chRt.squelch.locked;
    }

    function isGainLocked() {
        if (!runtimeState) return false;
        var g = (runtimeState.effective_settings || {}).gain;
        return g && g.locked;
    }

    function applySquelch(db) {
        if (isSquelchLocked()) return;
        ensureAudioCtxRunning();
        squelchSlider._userSet = true;
        squelchSlider.value = db;
        thresholdLabel.textContent = db.toFixed(1);
        var threshPct = Math.max(0, Math.min(100, (db - RSSI_MIN) / (RSSI_MAX - RSSI_MIN) * 100));
        meterThreshold.style.left = threshPct + "%";
        clearTimeout(squelchDebounce);
        squelchDebounce = setTimeout(function() {
            sendConfig("squelch_threshold", db);
            // Allow server updates again after round-trip
            setTimeout(function() { squelchSlider._userSet = false; }, 500);
        }, 100);
    }

    // Mouse drag
    meterThreshold.addEventListener("mousedown", function(e) {
        if (isSquelchLocked()) return;
        e.preventDefault();
        meterThreshold.classList.add("dragging");
        function onMove(e) { applySquelch(posToDb(e.clientX)); }
        function onUp() {
            meterThreshold.classList.remove("dragging");
            document.removeEventListener("mousemove", onMove);
            document.removeEventListener("mouseup", onUp);
        }
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
    });

    // Touch drag
    meterThreshold.addEventListener("touchstart", function(e) {
        if (isSquelchLocked()) return;
        e.preventDefault();
        meterThreshold.classList.add("dragging");
        function onMove(e) {
            if (e.touches.length > 0) applySquelch(posToDb(e.touches[0].clientX));
        }
        function onEnd() {
            meterThreshold.classList.remove("dragging");
            document.removeEventListener("touchmove", onMove);
            document.removeEventListener("touchend", onEnd);
            document.removeEventListener("touchcancel", onEnd);
        }
        document.addEventListener("touchmove", onMove, { passive: false });
        document.addEventListener("touchend", onEnd);
        document.addEventListener("touchcancel", onEnd);
    }, { passive: false });

    // Tap anywhere on the meter bar to set threshold
    meterContainer.addEventListener("click", function(e) {
        if (isSquelchLocked()) return;
        if (e.target === meterThreshold) return;
        applySquelch(posToDb(e.clientX));
    });

    // ── Custom gain slider ──────────────────────────────
    var gainTrack = document.getElementById("gainTrack");
    var gainFill = document.getElementById("gainFill");
    var gainThumb = document.getElementById("gainThumb");
    var gainDebounce = null;

    function setGainFromPct(pct) {
        pct = Math.max(0, Math.min(100, pct));
        var val = Math.round(GAIN_MIN + (pct / 100) * (GAIN_MAX - GAIN_MIN));
        gainFill.style.width = pct + "%";
        gainThumb.style.left = pct + "%";
        gainValue.textContent = val;
        gainSlider.value = val;
        return val;
    }

    function gainPosToVal(clientX) {
        var rect = gainTrack.getBoundingClientRect();
        var pct = (clientX - rect.left) / rect.width * 100;
        return setGainFromPct(pct);
    }

    function applyGain(val) {
        if (isGainLocked()) return;
        ensureAudioCtxRunning();
        gainSlider._userSet = true;
        clearTimeout(gainDebounce);
        gainDebounce = setTimeout(function() {
            sendConfig("gain", val);
            setTimeout(function() { gainSlider._userSet = false; }, 500);
        }, 100);
    }

    // Init position
    (function() {
        var initVal = parseFloat(gainSlider.value);
        var initPct = ((initVal - GAIN_MIN) / (GAIN_MAX - GAIN_MIN)) * 100;
        setGainFromPct(initPct);
    })();

    // Mouse drag
    gainThumb.addEventListener("mousedown", function(e) {
        if (isGainLocked()) return;
        e.preventDefault();
        gainThumb.classList.add("dragging");
        function onMove(e) { applyGain(gainPosToVal(e.clientX)); }
        function onUp() {
            gainThumb.classList.remove("dragging");
            document.removeEventListener("mousemove", onMove);
            document.removeEventListener("mouseup", onUp);
        }
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
    });

    // Touch drag
    gainThumb.addEventListener("touchstart", function(e) {
        if (isGainLocked()) return;
        e.preventDefault();
        gainThumb.classList.add("dragging");
        function onMove(e) {
            if (e.touches.length > 0) applyGain(gainPosToVal(e.touches[0].clientX));
        }
        function onEnd() {
            gainThumb.classList.remove("dragging");
            document.removeEventListener("touchmove", onMove);
            document.removeEventListener("touchend", onEnd);
            document.removeEventListener("touchcancel", onEnd);
        }
        document.addEventListener("touchmove", onMove, { passive: false });
        document.addEventListener("touchend", onEnd);
        document.addEventListener("touchcancel", onEnd);
    }, { passive: false });

    // Tap on track
    gainTrack.addEventListener("click", function(e) {
        if (isGainLocked()) return;
        if (e.target === gainThumb) return;
        applyGain(gainPosToVal(e.clientX));
    });

    // ── Live Audio (Web Audio API) ─────────────────────
    let audioCtx = null;
    let audioWs = null;
    let audioPlaying = false;
    let audioWorklet = null;
    let jitterBuffer = [];
    let bufferReady = false;  // gate: don't play until prebuffer is filled
    const SAMPLE_RATE = 8000;
    const TARGET_BUFFER_MS = 600;  // target playout delay
    const BUFFER_FRAMES = Math.ceil(SAMPLE_RATE * TARGET_BUFFER_MS / 1000);

    audioToggle.addEventListener("click", function() {
        if (audioPlaying) {
            stopAudio();
        } else {
            startAudio();
        }
    });

    function connectAudioWs() {
        if (audioWs) {
            audioWs.onclose = null;
            audioWs.close();
            audioWs = null;
        }

        if (!currentChannelId) return;

        var proto = location.protocol === "https:" ? "wss:" : "ws:";
        audioWs = new WebSocket(proto + "//" + location.host + "/audio/" + currentChannelId + "/live");
        audioWs.binaryType = "arraybuffer";

        audioWs.onmessage = function(ev) {
            var buf = ev.data;
            if (buf.byteLength < 4) return;

            var pcm = new Int16Array(buf, 4);
            var floats = new Float32Array(pcm.length);
            for (var i = 0; i < pcm.length; i++) {
                floats[i] = pcm[i] / 32768.0;
            }

            var totalSamples = jitterBuffer.reduce(function(s, c) { return s + c.length; }, 0);
            if (totalSamples < SAMPLE_RATE * 2) {
                jitterBuffer.push(floats);
            }
        };

        audioWs.onclose = function() {
            audioStatus.textContent = "Reconnecting...";
            if (audioPlaying) {
                jitterBuffer = [];
                bufferReady = false;
                setTimeout(connectAudioWs, 2000);
            }
        };

        audioStatus.textContent = "Playing";
    }

    function ensureAudioCtxRunning() {
        if (audioCtx && audioCtx.state === "suspended") {
            audioCtx.resume();
        }
    }

    function startAudio() {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: SAMPLE_RATE });
        // Mobile browsers require resume after user gesture
        audioCtx.resume();

        var bufferSize = 2048;
        var scriptNode = audioCtx.createScriptProcessor(bufferSize, 1, 1);

        scriptNode.onaudioprocess = function(ev) {
            var output = ev.outputBuffer.getChannelData(0);
            var written = 0;

            if (!bufferReady) {
                var totalSamples = jitterBuffer.reduce(function(s, c) { return s + c.length; }, 0);
                if (totalSamples >= BUFFER_FRAMES) {
                    bufferReady = true;
                } else {
                    for (var k = 0; k < output.length; k++) output[k] = 0;
                    return;
                }
            }

            if (jitterBuffer.length === 0) {
                bufferReady = false;
                for (var k = 0; k < output.length; k++) output[k] = 0;
                return;
            }

            while (written < output.length && jitterBuffer.length > 0) {
                var chunk = jitterBuffer[0];
                var remaining = output.length - written;
                var available = chunk.length;

                if (available <= remaining) {
                    output.set(chunk, written);
                    written += available;
                    jitterBuffer.shift();
                } else {
                    output.set(chunk.subarray(0, remaining), written);
                    jitterBuffer[0] = chunk.subarray(remaining);
                    written = output.length;
                }
            }

            for (var i = written; i < output.length; i++) {
                output[i] = 0;
            }
        };

        scriptNode.connect(audioCtx.destination);

        audioPlaying = true;
        audioToggle.textContent = "Stop Audio";
        audioToggle.classList.add("active");
        connectAudioWs();
    }

    function stopAudio() {
        audioPlaying = false;
        if (audioWs) {
            audioWs.onclose = null;
            audioWs.close();
            audioWs = null;
        }
        if (audioCtx) {
            audioCtx.close();
            audioCtx = null;
        }
        jitterBuffer = [];
        bufferReady = false;
        audioToggle.textContent = "Start Audio";
        audioToggle.classList.remove("active");
        audioStatus.textContent = "Stopped";
    }

    // ── Resume audio on visibility change (mobile tab switch) ──
    document.addEventListener("visibilitychange", function() {
        if (!document.hidden) {
            ensureAudioCtxRunning();
        }
    });

    // ══════════════════════════════════════════════════════
    // ── SETTINGS MODAL ───────────────────────────────────
    // ══════════════════════════════════════════════════════

    var restartBtn = document.getElementById("restartBtn");
    var settingsBanner = document.getElementById("settingsBanner");

    // Wire modal open callback into the inline modal code
    window.SDR._onModalOpen = function(openAddForm) {
        fetchRuntime().then(function() {
            return Promise.all([loadChannelsTab(), loadSettingsTab()]);
        }).then(function() {
            if (openAddForm) {
                openAddChannelForm();
            }
        });
    };

    // Restart
    restartBtn.addEventListener("click", function() {
        if (!confirm("Restart SDR Monitor? Active recordings will be finalized.")) return;
        restartBtn.disabled = true;
        restartBtn.textContent = "Restarting...";
        fetch("/api/restart", { method: "POST" })
            .then(function() {
                showBanner("Restarting \u2014 page will reload...", "success");
                setTimeout(function() { location.reload(); }, 3000);
            })
            .catch(function() {
                showBanner("Restart failed", "error");
                restartBtn.disabled = false;
                restartBtn.textContent = "Restart";
            });
    });

    function showBanner(msg, type) {
        settingsBanner.textContent = msg;
        settingsBanner.className = "settings-banner " + type;
        settingsBanner.style.display = "";
        setTimeout(function() { settingsBanner.style.display = "none"; }, 4000);
    }

    // ── Channels Tab ─────────────────────────────────────

    var channelTableBody = document.getElementById("channelTableBody");
    var channelFormWrap = document.getElementById("channelFormWrap");
    var addChannelBtn = document.getElementById("addChannelBtn");
    var channelFormTitle = document.getElementById("channelFormTitle");
    var channelFormErrors = document.getElementById("channelFormErrors");
    var cfId = document.getElementById("cfId");
    var cfName = document.getElementById("cfName");
    var cfFreq = document.getElementById("cfFreq");
    var cfDcs = document.getElementById("cfDcs");
    var cfMode = document.getElementById("cfMode");
    var cfSquelch = document.getElementById("cfSquelch");
    var cfSave = document.getElementById("cfSave");
    var cfCancel = document.getElementById("cfCancel");

    var editingChannelId = null; // null = add mode, string = edit mode
    var pendingStartup = null;  // null = no pending changes, array = pending set
    var savedStartup = [];      // last known saved startup_channels

    var startupActions = document.getElementById("startupActions");
    var saveStartupBtn = document.getElementById("saveStartupBtn");
    var revertStartupBtn = document.getElementById("revertStartupBtn");

    function loadChannelsTab() {
        // Receiver info bar
        var bar = document.getElementById("receiverInfoBar");
        if (runtimeState && runtimeState.receiver) {
            var r = runtimeState.receiver;
            bar.textContent = "RTL-SDR #" + r.device_index + " \u2014 " +
                (r.sample_rate / 1000) + " kHz \u2014 max " + r.max_channels + " channels";
        } else {
            bar.textContent = "Receiver info unavailable";
        }

        // Load config
        return fetch("/api/config")
            .then(function(r) { return r.json(); })
            .then(function(cfg) {
                renderChannelTable(cfg);
                renderSessionInfo(cfg);
            })
            .catch(function() {
                channelTableBody.innerHTML = '<tr><td colspan="9">Error loading config</td></tr>';
            });
    }

    function renderSessionInfo(cfg) {
        var info = document.getElementById("sessionInfo");
        var running = runtimeState ? runtimeState.running_channels : [];
        var startup = cfg.startup_channels || [];
        info.innerHTML =
            '<span class="label">Current session:</span> <span class="value">' +
            (running.length ? running.join(", ") : "none") + '</span><br>' +
            '<span class="label">Saved startup set:</span> <span class="value">' +
            (startup.length ? startup.join(", ") : "none") + '</span>';
    }

    function renderChannelTable(cfg) {
        channelTableBody.innerHTML = "";
        var chMap = cfg.channels || {};
        var running = runtimeState ? runtimeState.running_channels : [];
        savedStartup = (cfg.startup_channels || []).slice();
        var displayStartup = pendingStartup || savedStartup;
        var ids = Object.keys(chMap);

        ids.forEach(function(id) {
            var ch = chMap[id];
            var isRunning = running.indexOf(id) >= 0;
            var isStartup = displayStartup.indexOf(id) >= 0;

            var tr = document.createElement("tr");

            // Startup checkbox
            var tdStart = document.createElement("td");
            tdStart.className = "ch-col-startup";
            var cb = document.createElement("input");
            cb.type = "checkbox";
            cb.checked = isStartup;
            cb.title = "Include in startup set";
            cb.addEventListener("change", function() {
                handleStartupToggle(id, cb.checked);
            });
            tdStart.appendChild(cb);
            tr.appendChild(tdStart);

            // ID
            var tdId = document.createElement("td");
            tdId.textContent = id;
            tr.appendChild(tdId);

            // Name
            var tdName = document.createElement("td");
            tdName.textContent = ch.name || "";
            tr.appendChild(tdName);

            // Freq
            var tdFreq = document.createElement("td");
            var freqText = ch.freq_hz ? (ch.freq_hz / 1e6).toFixed(4) : "";
            if (isRunning) {
                tdFreq.className = "field-locked";
            }
            tdFreq.textContent = freqText;
            tr.appendChild(tdFreq);

            // DCS
            var tdDcs = document.createElement("td");
            if (isRunning) tdDcs.className = "field-locked";
            tdDcs.textContent = String(ch.dcs_code || 0).padStart(3, "0");
            tr.appendChild(tdDcs);

            // Mode
            var tdMode = document.createElement("td");
            if (isRunning) tdMode.className = "field-locked";
            tdMode.textContent = ch.dcs_mode || "advisory";
            tr.appendChild(tdMode);

            // Squelch
            var tdSq = document.createElement("td");
            tdSq.textContent = (ch.squelch != null) ? ch.squelch : "-45.0";
            tr.appendChild(tdSq);

            // Status
            var tdStatus = document.createElement("td");
            if (isRunning) {
                var badge = document.createElement("span");
                badge.className = "status-badge running";
                badge.textContent = "RUNNING";
                tdStatus.appendChild(badge);
            } else if (isStartup) {
                var badge = document.createElement("span");
                badge.className = "status-badge startup";
                badge.textContent = "STARTUP";
                tdStatus.appendChild(badge);
            }
            tr.appendChild(tdStatus);

            // Actions
            var tdAct = document.createElement("td");

            var editBtn = document.createElement("button");
            editBtn.className = "ch-action-btn";
            editBtn.textContent = "\u270E";
            editBtn.title = "Edit";
            editBtn.addEventListener("click", function() {
                openEditForm(id, ch, isRunning);
            });
            tdAct.appendChild(editBtn);

            if (!isRunning) {
                var delBtn = document.createElement("button");
                delBtn.className = "ch-action-btn delete";
                delBtn.textContent = "\u2716";
                delBtn.title = "Delete";
                delBtn.addEventListener("click", function() {
                    if (!confirm("Delete channel '" + id + "'?")) return;
                    fetch("/api/config/channels/" + id, { method: "DELETE" })
                        .then(function(r) { return r.json(); })
                        .then(function(res) {
                            if (res.error) {
                                showBanner(res.error, "error");
                            } else {
                                showBanner("Channel deleted", "success");
                                loadChannelsTab();
                            }
                        });
                });
                tdAct.appendChild(delBtn);
            }

            tr.appendChild(tdAct);
            channelTableBody.appendChild(tr);
        });
    }

    function handleStartupToggle(chId, checked) {
        var current = (pendingStartup || savedStartup).slice();
        if (checked) {
            if (current.indexOf(chId) < 0) current.push(chId);
        } else {
            current = current.filter(function(id) { return id !== chId; });
        }
        pendingStartup = current;
        updateStartupActions();
    }

    function updateStartupActions() {
        var changed = pendingStartup !== null &&
            (pendingStartup.length !== savedStartup.length ||
             pendingStartup.some(function(id) { return savedStartup.indexOf(id) < 0; }));
        startupActions.style.display = changed ? "" : "none";
    }

    saveStartupBtn.addEventListener("click", function() {
        if (!pendingStartup) return;
        saveStartupBtn.disabled = true;
        saveStartupBtn.textContent = "Saving...";
        fetch("/api/config/startup_channels", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ channels: pendingStartup }),
        })
            .then(function(r) { return r.json(); })
            .then(function(res) {
                saveStartupBtn.disabled = false;
                saveStartupBtn.textContent = "Save Startup Set";
                if (res.error) {
                    showBanner(res.error, "error");
                } else {
                    savedStartup = pendingStartup.slice();
                    pendingStartup = null;
                    updateStartupActions();
                    showRestartBanner("Startup set saved");
                }
                loadChannelsTab();
            });
    });

    revertStartupBtn.addEventListener("click", function() {
        pendingStartup = null;
        updateStartupActions();
        loadChannelsTab();
    });

    function showRestartBanner(msg) {
        settingsBanner.innerHTML = msg + ' \u2014 <a href="#" id="bannerRestart">Restart now</a> to apply';
        settingsBanner.className = "settings-banner success";
        settingsBanner.style.display = "";
        document.getElementById("bannerRestart").addEventListener("click", function(e) {
            e.preventDefault();
            fetch("/api/restart", { method: "POST" }).then(function() {
                settingsBanner.textContent = "Restarting \u2014 page will reload...";
                setTimeout(function() { location.reload(); }, 3000);
            });
        });
    }

    // ── Channel form (add/edit) ──────────────────────────

    function openAddChannelForm() {
        editingChannelId = null;
        channelFormTitle.textContent = "Add Channel";
        channelFormErrors.textContent = "";
        cfId.value = "";
        cfId.disabled = false;
        cfName.value = "";
        cfFreq.value = "";
        cfDcs.value = "0";
        cfMode.value = "advisory";
        cfSquelch.value = "-45";
        cfFreq.disabled = false;
        cfDcs.disabled = false;
        cfMode.disabled = false;
        channelFormWrap.style.display = "";
        addChannelBtn.style.display = "none";
    }

    addChannelBtn.addEventListener("click", function() {
        openAddChannelForm();
    });

    function openEditForm(id, ch, isRunning) {
        editingChannelId = id;
        channelFormTitle.textContent = "Edit Channel: " + id;
        channelFormErrors.textContent = "";
        cfId.value = id;
        cfId.disabled = true;
        cfName.value = ch.name || "";
        cfFreq.value = ch.freq_hz ? (ch.freq_hz / 1e6).toFixed(6) : "";
        cfDcs.value = ch.dcs_code || 0;
        cfMode.value = ch.dcs_mode || "advisory";
        cfSquelch.value = (ch.squelch != null) ? ch.squelch : -45;
        cfFreq.disabled = isRunning;
        cfDcs.disabled = isRunning;
        cfMode.disabled = isRunning;
        channelFormWrap.style.display = "";
        addChannelBtn.style.display = "none";
    }

    cfCancel.addEventListener("click", function() {
        channelFormWrap.style.display = "none";
        addChannelBtn.style.display = "";
    });

    cfSave.addEventListener("click", function() {
        channelFormErrors.textContent = "";

        var freqMhz = parseFloat(cfFreq.value);
        var freqHz = Math.round(freqMhz * 1e6);
        var dcsCode = parseInt(cfDcs.value) || 0;
        var squelch = parseFloat(cfSquelch.value);

        // Client-side validation
        var errors = [];
        if (!editingChannelId && !cfId.value.match(/^[a-zA-Z0-9_-]+$/)) {
            errors.push("ID: must match [a-zA-Z0-9_-]+");
        }
        if (!cfName.value.trim()) errors.push("Name: required");
        if (isNaN(freqHz) || freqHz < 1000000 || freqHz > 6000000000) {
            errors.push("Freq: must be 1-6000 MHz");
        }
        if (isNaN(squelch) || squelch < -70 || squelch > -5) {
            errors.push("Squelch: must be -70 to -5");
        }
        if (errors.length) {
            channelFormErrors.textContent = errors.join(" | ");
            return;
        }

        var body = {
            name: cfName.value.trim(),
            freq_hz: freqHz,
            dcs_code: dcsCode,
            dcs_mode: cfMode.value,
            squelch: squelch,
        };

        var url, method;
        if (editingChannelId) {
            url = "/api/config/channels/" + editingChannelId;
            method = "PUT";
        } else {
            body.id = cfId.value.trim();
            url = "/api/config/channels";
            method = "POST";
        }

        fetch(url, {
            method: method,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        })
            .then(function(r) { return r.json(); })
            .then(function(res) {
                if (res.errors) {
                    var msgs = Object.entries(res.errors).map(function(e) {
                        return e[0] + ": " + e[1];
                    });
                    channelFormErrors.textContent = msgs.join(" | ");
                } else if (res.error) {
                    channelFormErrors.textContent = res.error;
                } else {
                    channelFormWrap.style.display = "none";
                    addChannelBtn.style.display = "";
                    showRestartBanner(editingChannelId ? "Channel updated" : "Channel created");
                    loadChannelsTab();
                }
            });
    });

    // ── Settings Tab ─────────────────────────────────────

    var SETTINGS_META = {
        gain:            { label: "RF Gain",         type: "number", step: 1,   min: 0,  max: 50,    timing: "Applies immediately" },
        default_squelch: { label: "Default Squelch",  type: "number", step: 0.5, min: -70, max: -5,   timing: "Applied on restart (new channels only)" },
        audio_preset:    { label: "Audio Preset",     type: "choice", choices: ["conservative", "aggressive", "flat"], timing: "Applied on restart" },
        tau:             { label: "FM De-emphasis",    type: "number", step: 0.000001, min: 0, max: 1, timing: "Applied on restart" },
        record:          { label: "Record",           type: "bool",   timing: "Applied on restart" },
        max_audio_mb:    { label: "Max Audio MB",     type: "number", step: 1,   min: 10, max: 100000, timing: "Applied on restart" },
        tx_tail:         { label: "TX Tail (s)",      type: "number", step: 0.5, min: 0.5, max: 30,   timing: "Applied on restart" },
        log_days:        { label: "Log Days",         type: "number", step: 1,   min: 1,  max: 365,   timing: "Applied on restart" },
    };

    function loadSettingsTab() {
        var fields = document.getElementById("settingsFields");
        fields.innerHTML = "";

        return fetch("/api/config")
            .then(function(r) { return r.json(); })
            .then(function(cfg) {
                var saved = cfg.settings || {};
                var eff = runtimeState ? runtimeState.effective_settings : {};

                Object.keys(SETTINGS_META).forEach(function(key) {
                    var meta = SETTINGS_META[key];
                    var effInfo = eff[key] || {};
                    var value = effInfo.value != null ? effInfo.value : (saved[key] != null ? saved[key] : "");
                    var source = effInfo.source || "default";
                    var locked = effInfo.locked || false;

                    var row = document.createElement("div");
                    row.className = "setting-row";

                    // Label
                    var lbl = document.createElement("div");
                    lbl.className = "setting-label";
                    lbl.textContent = meta.label;
                    row.appendChild(lbl);

                    // Input
                    var inp = document.createElement("div");
                    inp.className = "setting-input";
                    var el;

                    if (meta.type === "choice") {
                        el = document.createElement("select");
                        meta.choices.forEach(function(c) {
                            var opt = document.createElement("option");
                            opt.value = c;
                            opt.textContent = c;
                            if (c === value) opt.selected = true;
                            el.appendChild(opt);
                        });
                    } else if (meta.type === "bool") {
                        el = document.createElement("select");
                        var optT = document.createElement("option");
                        optT.value = "true"; optT.textContent = "true";
                        var optF = document.createElement("option");
                        optF.value = "false"; optF.textContent = "false";
                        if (value === true) optT.selected = true;
                        else optF.selected = true;
                        el.appendChild(optT);
                        el.appendChild(optF);
                    } else {
                        el = document.createElement("input");
                        el.type = "number";
                        el.step = meta.step;
                        el.min = meta.min;
                        el.max = meta.max;
                        el.value = value;
                    }

                    el.dataset.key = key;
                    el.className = "setting-control";
                    if (locked) el.disabled = true;
                    inp.appendChild(el);
                    row.appendChild(inp);

                    // Source badge
                    var badge = document.createElement("span");
                    badge.className = "source-badge " + source;
                    badge.textContent = source;
                    row.appendChild(badge);

                    // Timing
                    var timing = document.createElement("span");
                    timing.className = "apply-timing";
                    timing.textContent = meta.timing;
                    row.appendChild(timing);

                    fields.appendChild(row);
                });
            });
    }

    document.getElementById("settingsSaveBtn").addEventListener("click", function() {
        var controls = document.querySelectorAll("#settingsFields .setting-control");
        var body = {};
        controls.forEach(function(el) {
            var key = el.dataset.key;
            if (el.disabled) return;
            var meta = SETTINGS_META[key];
            if (meta.type === "number") {
                body[key] = parseFloat(el.value);
            } else if (meta.type === "bool") {
                body[key] = el.value === "true";
            } else {
                body[key] = el.value;
            }
        });

        document.getElementById("settingsFormErrors").textContent = "";

        fetch("/api/config/settings", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        })
            .then(function(r) { return r.json(); })
            .then(function(res) {
                if (res.errors) {
                    var msgs = Object.entries(res.errors).map(function(e) {
                        return e[0] + ": " + e[1];
                    });
                    document.getElementById("settingsFormErrors").textContent = msgs.join(" | ");
                } else if (res.error) {
                    document.getElementById("settingsFormErrors").textContent = res.error;
                } else {
                    showBanner("Settings saved", "success");
                    loadSettingsTab();
                }
            });
    });

    // ── Init ───────────────────────────────────────────
    Promise.all([
        fetch("/api/channels").then(function(r) { return r.json(); }),
        fetchRuntime(),
    ]).then(function(results) {
        channels = results[0] || [];
        if (channels.length > 0) {
            currentChannelId = channels[0].id;
            renderChannelTabs();
            switchChannel(currentChannelId);
        } else {
            renderChannelTabs();
        }
    }).catch(function() {});

})();
