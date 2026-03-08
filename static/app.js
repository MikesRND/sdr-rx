// SDR Monitor Dashboard — WebSocket client and UI updates

(function() {
    "use strict";

    // ── Channel config (populated from /api/channel) ────
    var channelDcsCode = "---";

    // ── Elements ───────────────────────────────────────
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

    // ── Fetch channel config ────────────────────────────
    function loadChannelConfig() {
        fetch("/api/channel")
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

    // ── Telemetry WebSocket ────────────────────────────
    let ws = null;
    let reconnectTimer = null;
    let lastTxCount = 0;

    function connectWS() {
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        ws = new WebSocket(proto + "//" + location.host + "/ws");

        ws.onopen = function() {
            connStatus.textContent = "connected";
            connStatus.className = "status-item conn-status connected";
            if (reconnectTimer) {
                clearTimeout(reconnectTimer);
                reconnectTimer = null;
            }
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
        if (d.rssi_unit) {
            rssiUnit.textContent = d.rssi_unit;
            var sqUnit = document.getElementById("squelchUnit");
            if (sqUnit) sqUnit.textContent = d.rssi_unit;
        }

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
        fetch("/api/transmissions")
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
                        const url = "/audio/" + encodeURIComponent(fname);

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
                        fetch("/api/transmissions/" + idx, { method: "DELETE" })
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

    function applySquelch(db) {
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

        var proto = location.protocol === "https:" ? "wss:" : "ws:";
        audioWs = new WebSocket(proto + "//" + location.host + "/audio/live");
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

    // ── Init ───────────────────────────────────────────
    loadChannelConfig();
    connectWS();
    fetchTxLog();

})();
