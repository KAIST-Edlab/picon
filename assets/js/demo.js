/**
 * PICon Demo — Experience Mode, Agent Test Mode & Leaderboard
 */
(function () {
  'use strict';

  // --- Config ---
  const API_BASE = (document.querySelector('meta[name="demo-api-url"]') || {}).content
    || window.PICON_API_URL
    || '';

  // ===== Leaderboard Data (paper baselines) =====
  // IC = Internal Consistency, EC = External Consistency, RC = Retest Consistency
  // Area = normalized triangle area on IC-EC-RC radar chart
  var BASELINES = [
    { name: 'Human',           type: 'baseline',   arch: 'Baseline',    turns: 50, ic: 0.90, ec: 0.66, rc: 0.94 },
    { name: 'Human Simulacra', type: 'baseline',   arch: 'RAG',         turns: 50, ic: 0.79, ec: 0.63, rc: 0.87 },
    { name: 'Li et al. (2025)',type: 'baseline',   arch: 'Prompting',   turns: 50, ic: 0.73, ec: 0.59, rc: 0.98 },
    { name: 'DeepPersona',     type: 'baseline',   arch: 'Prompting',   turns: 50, ic: 0.72, ec: 0.54, rc: 0.92 },
    { name: 'Character.ai',    type: 'baseline',   arch: 'Commercial',  turns: 50, ic: 0.71, ec: 0.71, rc: 0.46 },
    { name: 'Twin 2K 500',     type: 'baseline',   arch: 'Prompting',   turns: 50, ic: 0.53, ec: 0.26, rc: 0.95 },
    { name: 'Consistent LLM',  type: 'baseline',   arch: 'Fine-tuned',  turns: 50, ic: 0.31, ec: 0.30, rc: 0.14 },
    { name: 'OpenCharacter',   type: 'baseline',   arch: 'Fine-tuned',  turns: 50, ic: 0.16, ec: 0.15, rc: 0.14 },
  ];

  // Compute normalized triangle area: (IC*EC + EC*RC + RC*IC) / 3
  function computeArea(d) {
    return (d.ic * d.ec + d.ec * d.rc + d.rc * d.ic) / 3;
  }

  BASELINES.forEach(function (d) { d.area = computeArea(d); });

  // Abandon the old merged cache key — stale auto-inserted entries lived here.
  try { localStorage.removeItem('picon_community'); } catch (e) { /* ignore */ }
  // (v2 migration already ran — removed the one-time wipe to avoid accidental data loss)

  // myLocalRuns:   this browser's own completed runs (private, localStorage-backed)
  // publishedEntries: entries explicitly published to the public leaderboard via /api/leaderboard/submit
  var LOCAL_RUNS_KEY = 'picon_my_runs';
  var myLocalRuns = [];
  try {
    myLocalRuns = JSON.parse(localStorage.getItem(LOCAL_RUNS_KEY) || '[]');
  } catch (e) { /* ignore */ }
  var publishedEntries = [];

  function saveLocalRuns() {
    try { localStorage.setItem(LOCAL_RUNS_KEY, JSON.stringify(myLocalRuns)); } catch (e) { /* ignore */ }
  }

  // Append a single entry to localStorage atomically — re-reads from storage
  // right before writing, so concurrent tabs/popups don't overwrite each other.
  function appendLocalRun(entry) {
    var current = [];
    try { current = JSON.parse(localStorage.getItem(LOCAL_RUNS_KEY) || '[]'); }
    catch (e) { current = []; }
    current.push(entry);
    try { localStorage.setItem(LOCAL_RUNS_KEY, JSON.stringify(current)); } catch (e) { /* ignore */ }
    myLocalRuns = current;
  }

  // Sync in-memory myLocalRuns with what's in localStorage (picks up writes
  // from other tabs). Called on 'storage' events and before rendering.
  window.addEventListener('storage', function (e) {
    if (e.key === LOCAL_RUNS_KEY) {
      try { myLocalRuns = JSON.parse(e.newValue || '[]'); } catch (_e) {}
      if (typeof renderLeaderboard === 'function') renderLeaderboard();
    }
  });

  function fetchCommunityEntries() {
    if (!API_BASE) return;
    fetch(API_BASE + '/api/leaderboard')
      .then(function (res) { return res.json(); })
      .then(function (data) {
        publishedEntries = (data.entries || []).map(function (e) {
          e.area = computeArea(e);
          return e;
        });
        renderLeaderboard();
      })
      .catch(function () { /* network issue — leave publishedEntries as-is */ });
  }

  // ===== Tab switching =====
  document.querySelectorAll('.demo-tab').forEach(function (tab) {
    tab.addEventListener('click', function () {
      document.querySelectorAll('.demo-tab').forEach(function (t) { t.classList.remove('active'); });
      document.querySelectorAll('.demo-panel').forEach(function (p) { p.classList.remove('active'); });
      tab.classList.add('active');
      document.getElementById('panel-' + tab.dataset.tab).classList.add('active');
      if (tab.dataset.tab === 'leaderboard') fetchCommunityEntries();
    });
  });

  // ===== Helpers =====

  function linkify(text) {
    return text.replace(/(https?:\/\/[^\s)<>]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');
  }

  function addMessage(container, type, text) {
    var el = document.createElement('div');
    el.className = 'chat-msg ' + type;
    el.innerHTML = linkify(text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'));
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
    return el;
  }

  function showTyping(container) {
    var el = document.createElement('div');
    el.className = 'chat-typing';
    el.innerHTML = '<span></span><span></span><span></span>';
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
    return el;
  }

  function setProgress(el, progress) {
    if (!progress) return;
    var phase = {
      predefined: 'Part 1: Getting to Know You',
      main: 'Part 2: Interrogation',
      repeat: 'Part 3: Retest',
      complete: 'Complete'
    }[progress.phase] || progress.phase;
    el.textContent = progress.total
      ? phase + ' — Q' + progress.current + '/' + progress.total
      : phase + ' — Q' + progress.current;
  }

  function fmtScore(v) {
    if (v == null) return '—';
    return v.toFixed(2);
  }

  function renderScoreGrid(gridEl, scores) {
    gridEl.innerHTML = '';
    var dims = [
      { key: 'ic', label: 'Internal Consistency', sub: 'Non-contradiction × Cooperativeness' },
      { key: 'ec', label: 'External Consistency', sub: 'Non-refutation × Coverage' },
      { key: 'rc', label: 'Retest Consistency', sub: 'Inter-session stability' },
    ];
    dims.forEach(function (d) {
      var cell = document.createElement('div');
      cell.className = 'score-cell';
      cell.innerHTML =
        '<div class="score-label">' + d.label + '</div>' +
        '<div class="score-value">' + fmtScore(scores[d.key]) + '</div>' +
        '<div class="score-sub">' + d.sub + '</div>';
      gridEl.appendChild(cell);
    });
  }

  // ===== Experience Mode =====

  var expStart = document.getElementById('exp-start');
  var expChat = document.getElementById('exp-chat');
  var expMessages = document.getElementById('exp-messages');
  var expInput = document.getElementById('exp-input');
  var expSend = document.getElementById('exp-send');
  var expProgress = document.getElementById('exp-progress');
  var expSessionId = null;
  var expLoading = false;
  var expHeartbeat = null;
  var pendingExpEntry = null;

  document.getElementById('exp-start-btn').addEventListener('click', async function () {
    var name = document.getElementById('exp-name').value.trim();
    var turns = parseInt(document.getElementById('exp-turns').value);
    if (!name) return;

    if (!API_BASE) {
      alert('Demo backend is not configured. Please set demo_api_url in _config.yml.');
      return;
    }

    expStart.style.display = 'none';
    expChat.style.display = 'block';
    var expTurnsBadge = document.getElementById('exp-turns-badge');
    if (expTurnsBadge) expTurnsBadge.textContent = turns + ' turns';
    addMessage(expMessages, 'info', 'Starting interview for ' + name + ' (' + turns + ' turns chosen)...');

    try {
      var res = await fetch(API_BASE + '/api/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name, num_turns: turns })
      });

      if (!res.ok) throw new Error('Failed to start: ' + res.status);
      var data = await res.json();

      expSessionId = data.session_id;
      setProgress(expProgress, data.progress);

      if (data.status === 'queued') {
        // Server is at capacity — poll until a slot opens
        var queueMsg = addMessage(expMessages, 'info', 'Server is busy. You are in the queue — please wait...');
        var firstQuestion = await pollExperienceQueue(expSessionId, expMessages, queueMsg);
        if (!firstQuestion) {
          addMessage(expMessages, 'info', 'Failed to start interview. Please try again later.');
          return;
        }
        if (queueMsg) queueMsg.remove();
        addMessage(expMessages, 'system', firstQuestion);
      } else {
        addMessage(expMessages, 'system', data.first_question);
      }
      expSend.disabled = false;
      expInput.focus();

      // Heartbeat: periodically ping the server so it knows we're still here
      expHeartbeat = setInterval(function () {
        if (!expSessionId) { clearInterval(expHeartbeat); return; }
        fetch(API_BASE + '/api/experience/status/' + expSessionId).catch(function () {});
      }, 30000); // every 30s
    } catch (err) {
      addMessage(expMessages, 'info', 'Error: ' + err.message);
    }
  });

  async function pollExperienceQueue(sessionId, messagesEl, queueMsgEl) {
    // Poll /api/experience/status until we get the first question or an error.
    while (true) {
      await new Promise(function (r) { setTimeout(r, 2000); });
      try {
        var res = await fetch(API_BASE + '/api/experience/status/' + sessionId);
        if (!res.ok) return null;
        var data = await res.json();

        // Update queue position message in-place
        if (data.status === 'queued' && queueMsgEl) {
          var posText = data.queue_position
            ? 'Queue position: ' + data.queue_position + ' of ' + (data.max_concurrent || '?') + ' slots — please wait...'
            : 'Server is busy. You are in the queue — please wait...';
          queueMsgEl.textContent = posText;
        }

        if (data.status === 'error') return null;
        if (data.first_question) return data.first_question;
        if (data.status === 'running' && !data.first_question) continue; // still loading
      } catch (e) {
        return null;
      }
    }
  }

  async function sendExperienceResponse() {
    var text = expInput.value.trim();
    if (!text || !expSessionId || expLoading) return;

    expInput.value = '';
    expLoading = true;
    expSend.disabled = true;
    addMessage(expMessages, 'user', text);
    var typing = showTyping(expMessages);

    try {
      var res = await fetch(API_BASE + '/api/respond', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: expSessionId,
          response: text,
        })
      });

      typing.remove();
      if (!res.ok) throw new Error('Error: ' + res.status);
      var data = await res.json();

      setProgress(expProgress, data.progress);

      if (data.is_complete) {
        if (expHeartbeat) { clearInterval(expHeartbeat); expHeartbeat = null; }
        addMessage(expMessages, 'info', 'Interview complete! Calculating your consistency scores...');
        expSend.disabled = true;
        expInput.disabled = true;

        // Poll for results — eval may still be running after interview ends
        var maxWait = 600; // 10 min max
        var waited = 0;
        while (waited < maxWait) {
          try {
            var rRes = await fetch(API_BASE + '/api/results/' + expSessionId);
            if (rRes.ok) {
              var results = await rRes.json();
              if (results.status === 'complete' && results.results && results.results.eval_scores) {
                var evalScores = results.results.eval_scores || {};
                var hasScores = evalScores.ic != null || evalScores.ec != null || evalScores.rc != null;
                var expResultsEl = document.getElementById('exp-results');
                expResultsEl.style.display = 'block';
                var expResultsTurnsBadge = document.getElementById('exp-results-turns-badge');
                if (expResultsTurnsBadge) expResultsTurnsBadge.textContent = document.getElementById('exp-turns').value + ' turns';
                if (!hasScores) {
                  addMessage(expMessages, 'info', 'Evaluation could not produce scores — the interview may have been too short or was flagged by the AI detector.');
                }
                renderScoreGrid(
                  document.getElementById('exp-score-grid'),
                  evalScores
                );
                var expSubmitBtn = document.getElementById('exp-submit-lb-btn');
                var expNote = document.getElementById('exp-leaderboard-note');
                if (hasScores) {
                  pendingExpEntry = {
                    name: (document.getElementById('exp-name').value.trim() || 'You'),
                    type: 'community',
                    arch: 'Community',
                    turns: parseInt(document.getElementById('exp-turns').value),
                    ic: evalScores.ic || 0,
                    ec: evalScores.ec || 0,
                    rc: evalScores.rc || 0,
                  };
                  pendingExpEntry.area = computeArea(pendingExpEntry);
                  if (expSubmitBtn) { expSubmitBtn.style.display = ''; expSubmitBtn.disabled = false; expSubmitBtn.textContent = 'Submit to Leaderboard'; }
                  if (expNote) expNote.textContent = 'Add your result to the leaderboard to compare against baselines.';
                } else {
                  pendingExpEntry = null;
                  if (expSubmitBtn) expSubmitBtn.style.display = 'none';
                  if (expNote) expNote.textContent = '';
                }
                break;
              } else if (results.status === 'error') {
                addMessage(expMessages, 'info', 'Error during evaluation: ' + (results.error || 'Unknown error'));
                break;
              }
              // status === 'pending' — keep polling
            }
          } catch (e) { /* retry */ }
          await new Promise(function (r) { setTimeout(r, 5000); });
          waited += 5;
        }
        if (waited >= maxWait) {
          addMessage(expMessages, 'info', 'Evaluation is taking longer than expected. Please check back later.');
        }
        return;
      }

      if (data.next_question) {
        addMessage(expMessages, 'system', data.next_question);
      } else if (data.error) {
        addMessage(expMessages, 'info', 'Error: ' + data.error);
      }
    } catch (err) {
      typing.remove();
      addMessage(expMessages, 'info', 'Error: ' + err.message);
    } finally {
      expLoading = false;
      if (!expInput.disabled) {
        expSend.disabled = false;
        expInput.focus();
      }
    }
  }

  expSend.addEventListener('click', sendExperienceResponse);
  expInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') sendExperienceResponse();
  });

  // Submit interview result to the browser-local leaderboard
  var expSubmitLbBtn = document.getElementById('exp-submit-lb-btn');
  if (expSubmitLbBtn) {
    expSubmitLbBtn.addEventListener('click', function () {
      if (!pendingExpEntry) return;
      appendLocalRun(pendingExpEntry);
      pendingExpEntry = null;
      expSubmitLbBtn.disabled = true;
      expSubmitLbBtn.textContent = 'Added!';
      var note = document.getElementById('exp-leaderboard-note');
      if (note) note.textContent = 'Your result has been added to the leaderboard.';
      renderLeaderboard();
    });
  }

  // ===== Agent Test Mode =====

  var agentTerminal = document.getElementById('agent-terminal-body');
  var agentProgress = document.getElementById('agent-progress');
  var agentSessionId = null;
  var agentLogIndex = 0;  // track how many log lines we've fetched
  var activeSubmode = 'external';
  var pendingLeaderboardEntry = null;
  var savedLogLines = [];  // filtered log lines for download
  var currentAgentLabel = '';

  // Submode switching
  document.querySelectorAll('.agent-submode-card').forEach(function (card) {
    card.addEventListener('click', function () {
      document.querySelectorAll('.agent-submode-card').forEach(function (c) { c.classList.remove('active'); });
      card.classList.add('active');
      activeSubmode = card.dataset.submode;
      document.getElementById('agent-form-external').style.display = activeSubmode === 'external' ? 'flex' : 'none';
      document.getElementById('agent-form-quick').style.display = activeSubmode === 'quick' ? 'flex' : 'none';
      // Clear any stale results card from the other submode's previous run.
      document.getElementById('agent-results').style.display = 'none';
    });
  });

  function startAgentEvaluation(payload, displayLabel) {
    if (!API_BASE) {
      alert('Demo backend is not configured. Please set demo_api_url in _config.yml.');
      return;
    }

    document.getElementById('agent-submode-selector').style.display = 'none';
    document.getElementById('agent-form-external').style.display = 'none';
    document.getElementById('agent-form-quick').style.display = 'none';
    document.getElementById('agent-log').style.display = 'block';
    agentTerminal.innerHTML = '';
    agentLogIndex = 0;
    savedLogLines = [];
    currentAgentLabel = displayLabel;
    var saveLogBtn = document.getElementById('agent-save-log-btn');
    if (saveLogBtn) saveLogBtn.style.display = 'none';
    var cancelBtnEl = document.getElementById('agent-cancel-btn');
    if (cancelBtnEl) cancelBtnEl.style.display = '';

    var agentTurnsBadge = document.getElementById('agent-turns-badge');
    if (agentTurnsBadge) {
      var t = payload.num_turns;
      var s = payload.num_sessions;
      agentTurnsBadge.textContent = t + ' turns' + (s > 1 ? ' × ' + s + ' sessions' : '');
    }

    appendTerminal('$ picon.evaluate(' + displayLabel + ')\n');
    agentProgress.textContent = 'Starting evaluation...';

    (async function () {
      try {
        var res = await fetch(API_BASE + '/api/agent/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });

        if (!res.ok) throw new Error('Failed to start: ' + res.status);
        var data = await res.json();
        agentSessionId = data.session_id;
        appendTerminal('Evaluation started. This may take several minutes...\n\n');
        pollAgentProgress(data.session_id);
      } catch (err) {
        appendTerminal('ERROR: ' + err.message + '\n');
        agentProgress.textContent = 'Error';
      }
    })();
  }

  // External Agent start
  document.getElementById('agent-start-btn-external').addEventListener('click', function () {
    var name = document.getElementById('ext-agent-name').value.trim();
    var endpoint = document.getElementById('ext-agent-endpoint').value.trim();
    var turns = document.getElementById('ext-agent-turns').value;
    var sessions = document.getElementById('ext-agent-sessions').value;

    if (!name) { alert('Please provide an agent name.'); return; }
    if (!endpoint) { alert('Please provide your agent\'s API endpoint.'); return; }

    startAgentEvaluation({
      mode: 'external',
      name: name,
      api_base: endpoint,
      num_turns: parseInt(turns),
      num_sessions: parseInt(sessions),
    }, name + ', endpoint=' + endpoint + ', turns=' + turns);
  });

  // Quick Agent start
  document.getElementById('agent-start-btn-quick').addEventListener('click', function () {
    var name = document.getElementById('quick-agent-name').value.trim();
    var model = document.getElementById('quick-agent-model').value.trim();
    var apiKey = document.getElementById('quick-agent-api-key').value.trim();
    var persona = document.getElementById('quick-agent-persona').value.trim();
    var turns = document.getElementById('quick-agent-turns').value;
    var sessions = document.getElementById('quick-agent-sessions').value;
    var apiBase = document.getElementById('quick-agent-api-base').value.trim();
    var apiVersion = document.getElementById('quick-agent-api-version').value.trim();
    var extraEnvRaw = (document.getElementById('quick-agent-extra-env') || {}).value || '';

    var extraEnv = {};
    var extraEnvLines = extraEnvRaw.split('\n');
    for (var i = 0; i < extraEnvLines.length; i++) {
      var line = extraEnvLines[i].trim();
      if (!line || line.indexOf('#') === 0) continue;
      var eq = line.indexOf('=');
      if (eq <= 0) {
        alert('Extra Env Vars line "' + line + '" is not in KEY=value form.');
        return;
      }
      var k = line.substring(0, eq).trim();
      var v = line.substring(eq + 1).trim();
      if (!k) continue;
      extraEnv[k] = v;
    }
    var hasExtraEnv = Object.keys(extraEnv).length > 0;

    if (!name) { alert('Please provide an agent name.'); return; }
    if (!model) { alert('Please provide a model name in LiteLLM format (e.g. openai/gpt-4o, gemini/gemini-2.5-flash).'); return; }
    if (!apiKey && !apiBase && !hasExtraEnv) {
      alert('Please provide an API key, OR an API Base URL (self-hosted/local), OR provider env vars in "Advanced".');
      return;
    }
    if (!persona) { alert('Please provide a persona / system prompt.'); return; }
    if (model.toLowerCase().indexOf('azure/') === 0 && !apiBase && !extraEnv['AZURE_API_BASE']) {
      alert('Azure models require an API Base URL (or AZURE_API_BASE in Extra Env Vars). Expand "Advanced".');
      return;
    }

    var payload = {
      mode: 'quick',
      name: name,
      model: model,
      api_key: apiKey,
      persona: persona,
      num_turns: parseInt(turns),
      num_sessions: parseInt(sessions),
    };
    if (apiBase) payload.api_base = apiBase;
    if (apiVersion) payload.api_version = apiVersion;
    if (hasExtraEnv) payload.extra_env = extraEnv;

    startAgentEvaluation(payload, name + ', model=' + model + ', turns=' + turns);
  });

  function appendTerminal(text) {
    // System/status messages — always shown, not filtered.
    var lines = text.split('\n');
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (i === lines.length - 1 && line === '') continue;
      var el = document.createElement('div');
      el.className = 'log-entry log-system';
      el.textContent = line;
      agentTerminal.appendChild(el);
    }
    agentTerminal.scrollTop = agentTerminal.scrollHeight;
  }

  var LOG_TAG_RE = /\[(RESPONSE|ACTION|CONFIRMATION QUESTION|REPEAT QUESTION|TOOL OUTPUT)\]([\s\S]*)/;

  function appendLogLine(line) {
    var m = line.match(LOG_TAG_RE);
    if (!m) return;  // filter out all other log levels
    var tag = m[1];
    var rest = m[2];
    var fullTag = tag;
    if (tag === 'ACTION') {
      var am = rest.match(/^\s*(Extractor|Questioner)/);
      if (!am) return;
      fullTag = 'ACTION ' + am[1];
    }
    var display = '[' + fullTag + ']' + rest;
    savedLogLines.push(display);

    var collapsible = (fullTag === 'ACTION Extractor' || fullTag === 'TOOL OUTPUT');
    var cssSuffix = fullTag.toLowerCase().replace(/\s+/g, '-');
    var el;
    if (collapsible) {
      el = document.createElement('details');
      el.className = 'log-entry log-collapsible log-' + cssSuffix;
      var sum = document.createElement('summary');
      sum.textContent = '[' + fullTag + ']';
      el.appendChild(sum);
      var body = document.createElement('div');
      body.className = 'log-entry-body';
      body.textContent = rest.replace(/^\s*(Extractor|Questioner)\s*/, '').trim() || rest.trim();
      el.appendChild(body);
    } else {
      el = document.createElement('div');
      el.className = 'log-entry log-' + cssSuffix;
      el.textContent = display;
    }
    agentTerminal.appendChild(el);
    agentTerminal.scrollTop = agentTerminal.scrollHeight;
  }

  async function fetchAgentLogs(sessionId) {
    try {
      var res = await fetch(API_BASE + '/api/agent/logs/' + sessionId + '?since=' + agentLogIndex);
      if (!res.ok) return;
      var data = await res.json();
      if (data.lines && data.lines.length > 0) {
        for (var i = 0; i < data.lines.length; i++) appendLogLine(data.lines[i]);
        agentLogIndex = data.total;
      }
    } catch (e) { /* ignore */ }
  }

  async function pollAgentProgress(sessionId) {
    // Transient failures (Railway 5xx, network blips) MUST NOT kill the poller.
    // Under concurrent load, a single failed tick used to freeze the UI forever
    // even though the backend job kept running and completed successfully.
    // We now tolerate up to ~30s of consecutive failures before giving up, and
    // a successful tick resets the counter.
    var consecutiveFailures = 0;
    var MAX_CONSECUTIVE_FAILURES = 10; // ~30s at 3s interval
    var stopped = false;

    function stop() {
      stopped = true;
      clearInterval(interval);
    }

    var interval = setInterval(async function () {
      if (stopped) return;
      try {
        // Logs are best-effort (its own try/catch); failures here are swallowed.
        await fetchAgentLogs(sessionId);

        var res = await fetch(API_BASE + '/api/agent/status/' + sessionId);
        if (!res.ok) {
          consecutiveFailures++;
          if (consecutiveFailures >= MAX_CONSECUTIVE_FAILURES) {
            stop();
            appendTerminal('\nNetwork issue — status endpoint returned ' + res.status +
              ' for too long. The evaluation may still be running on the server; ' +
              'refresh and check the leaderboard in a few minutes.\n');
            agentProgress.textContent = 'Connection lost';
          }
          return;
        }
        consecutiveFailures = 0;

        var data = await res.json();

        if (data.status === 'queued') {
          var posText = data.queue_position ? ' (position ' + data.queue_position + ')' : '';
          agentProgress.textContent = 'Queued' + posText + ' — waiting for available slot...';
        } else {
          agentProgress.textContent =
            'Session ' + data.current_session + '/' + data.total_sessions + ' — Running...';
        }

        if (data.is_complete) {
          stop();

          // Fetch final logs
          await fetchAgentLogs(sessionId);

          if (data.error) {
            agentProgress.textContent = 'Error';
            appendTerminal('\nERROR: ' + data.error + '\n');
            return;
          }

          agentProgress.textContent = 'Complete';
          appendTerminal('\n--- Evaluation complete ---\n');

          // Fetch results — retry a few times in case of transient 5xx at the tail
          var results = null;
          for (var attempt = 0; attempt < 5; attempt++) {
            try {
              var rRes = await fetch(API_BASE + '/api/agent/results/' + sessionId);
              if (rRes.ok) {
                results = await rRes.json();
                break;
              }
            } catch (_e) { /* retry */ }
            await new Promise(function (r) { setTimeout(r, 2000); });
          }

          if (results) {
            // Keep the conversation log visible alongside the results.
            var cancelBtnEl2 = document.getElementById('agent-cancel-btn');
            if (cancelBtnEl2) cancelBtnEl2.style.display = 'none';
            var saveLogBtn2 = document.getElementById('agent-save-log-btn');
            if (saveLogBtn2) saveLogBtn2.style.display = '';
            document.getElementById('agent-results').style.display = 'block';
            var agentResultsTurnsBadge = document.getElementById('agent-results-turns-badge');
            if (agentResultsTurnsBadge) {
              var rt = document.getElementById(activeSubmode === 'external' ? 'ext-agent-turns' : 'quick-agent-turns').value;
              var rs = document.getElementById(activeSubmode === 'external' ? 'ext-agent-sessions' : 'quick-agent-sessions').value;
              agentResultsTurnsBadge.textContent = rt + ' turns' + (parseInt(rs) > 1 ? ' × ' + rs + ' sessions' : '');
            }
            renderScoreGrid(
              document.getElementById('agent-score-grid'),
              results.scores || {}
            );

            // Store pending entry for the submit button (not added to leaderboard yet).
            pendingLeaderboardEntry = {
              name: results.name || 'Agent',
              type: 'community',
              arch: 'Community',
              turns: parseInt(document.getElementById(activeSubmode === 'external' ? 'ext-agent-turns' : 'quick-agent-turns').value),
              ic: results.scores.ic || 0,
              ec: results.scores.ec || 0,
              rc: results.scores.rc || 0,
            };
            pendingLeaderboardEntry.area = computeArea(pendingLeaderboardEntry);
          } else {
            appendTerminal('\nCould not fetch final results after several retries. ' +
              'The evaluation completed on the server — try refreshing.\n');
            agentProgress.textContent = 'Results unavailable';
          }
        }
      } catch (err) {
        // Network blip, JSON parse error, etc. Count it but do not give up.
        consecutiveFailures++;
        if (consecutiveFailures >= MAX_CONSECUTIVE_FAILURES) {
          stop();
          appendTerminal('\nLost connection to server (' + err.message + '). ' +
            'The evaluation may still be running on the server; refresh in a few minutes.\n');
          agentProgress.textContent = 'Connection lost';
        }
      }
    }, 3000);
  }

  function showAgentForm() {
    document.getElementById('agent-submode-selector').style.display = '';
    document.getElementById('agent-form-external').style.display = activeSubmode === 'external' ? 'flex' : 'none';
    document.getElementById('agent-form-quick').style.display = activeSubmode === 'quick' ? 'flex' : 'none';
    // Hide any stale results card from a previous run — otherwise the user sees
    // an empty "Evaluation Report — 50 turns" with "—" placeholders before they
    // start a new run.
    document.getElementById('agent-results').style.display = 'none';
  }

  // Cancel button
  var cancelBtn = document.getElementById('agent-cancel-btn');
  if (cancelBtn) {
    cancelBtn.addEventListener('click', async function () {
      if (agentSessionId && API_BASE) {
        try { await fetch(API_BASE + '/api/agent/cancel/' + agentSessionId, { method: 'DELETE' }); }
        catch (e) { /* ignore */ }
      }
      document.getElementById('agent-log').style.display = 'none';
      showAgentForm();
      agentTerminal.innerHTML = '';
      agentLogIndex = 0;
      savedLogLines = [];
    });
  }

  // Save log button — downloads filtered conversation log as .txt
  var saveLogBtn = document.getElementById('agent-save-log-btn');
  if (saveLogBtn) {
    saveLogBtn.addEventListener('click', function () {
      var header = 'PICon Evaluation Log\n' + (currentAgentLabel ? currentAgentLabel + '\n' : '') +
        'Generated: ' + new Date().toISOString() + '\n' + '='.repeat(60) + '\n\n';
      var blob = new Blob([header + savedLogLines.join('\n') + '\n'], { type: 'text/plain' });
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      var stamp = new Date().toISOString().replace(/[:.]/g, '-');
      a.download = 'picon-log-' + stamp + '.txt';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(url); }, 0);
    });
  }

  // Submit to leaderboard button (adds to browser-local leaderboard only)
  var submitLbBtn = document.getElementById('agent-submit-lb-btn');
  if (submitLbBtn) {
    submitLbBtn.addEventListener('click', function () {
      if (!pendingLeaderboardEntry) return;
      appendLocalRun(pendingLeaderboardEntry);
      pendingLeaderboardEntry = null;
      submitLbBtn.disabled = true;
      submitLbBtn.textContent = 'Added!';
      document.getElementById('agent-leaderboard-note').textContent = 'Your result has been added to the leaderboard.';
      renderLeaderboard();
    });
  }

  // Retry button
  var retryBtn = document.getElementById('agent-retry-btn');
  if (retryBtn) {
    retryBtn.addEventListener('click', function () {
      document.getElementById('agent-results').style.display = 'none';
      document.getElementById('agent-log').style.display = 'none';
      if (submitLbBtn) {
        submitLbBtn.disabled = false;
        submitLbBtn.textContent = 'Submit to Leaderboard';
      }
      document.getElementById('agent-leaderboard-note').textContent =
        'Add your result to the leaderboard to compare against baselines.';
      pendingLeaderboardEntry = null;
      showAgentForm();
      agentTerminal.innerHTML = '';
      agentLogIndex = 0;
      savedLogLines = [];
    });
  }

  // ===== Leaderboard =====

  var currentSort = 'area';
  var currentFilter = 'all';
  var currentTurnsFilter = 'all';

  function getAllEntries() {
    return BASELINES.concat(publishedEntries).concat(myLocalRuns);
  }

  function renderLeaderboard() {
    var entries = getAllEntries();

    // Filter by type
    if (currentFilter !== 'all') {
      entries = entries.filter(function (d) { return d.type === currentFilter; });
    }

    // Filter by turns
    if (currentTurnsFilter !== 'all') {
      var turnsVal = parseInt(currentTurnsFilter);
      entries = entries.filter(function (d) { return d.turns === turnsVal; });
    }

    // Sort descending
    entries.sort(function (a, b) { return (b[currentSort] || 0) - (a[currentSort] || 0); });

    var tbody = document.getElementById('leaderboard-body');
    tbody.innerHTML = '';

    entries.forEach(function (d, i) {
      var rank = i + 1;
      var tr = document.createElement('tr');
      if (d.name === 'Human') tr.className = 'lb-human-row';

      var archClass = d.arch.toLowerCase().replace(/[^a-z]/g, '');
      if (archClass === 'finetuned') archClass = 'finetuned';
      else if (archClass === 'ragbased' || archClass === 'rag') archClass = 'rag';

      var badgeClass = d.type === 'community' ? 'community' : archClass;

      var rankClass = '';
      if (rank === 1) rankClass = 'lb-rank-1';
      else if (rank === 2) rankClass = 'lb-rank-2';
      else if (rank === 3) rankClass = 'lb-rank-3';

      tr.innerHTML =
        '<td class="lb-rank ' + rankClass + '">' + rank + '</td>' +
        '<td class="lb-name">' + d.name + '</td>' +
        '<td class="lb-type"><span class="lb-badge ' + badgeClass + '">' + d.arch + '</span></td>' +
        '<td class="lb-turns">' + (d.turns || '—') + '</td>' +
        '<td class="lb-score">' + fmtScore(d.ic) + '</td>' +
        '<td class="lb-score">' + fmtScore(d.ec) + '</td>' +
        '<td class="lb-score">' + fmtScore(d.rc) + '</td>' +
        '<td class="lb-score"><strong>' + fmtScore(d.area) + '</strong></td>';

      tbody.appendChild(tr);
    });

    // Update active header
    document.querySelectorAll('.leaderboard-table th.sortable').forEach(function (th) {
      th.classList.toggle('active', th.dataset.col === currentSort);
    });
  }

  // Sort handlers
  document.querySelectorAll('.leaderboard-table th.sortable').forEach(function (th) {
    th.addEventListener('click', function () {
      currentSort = th.dataset.col;
      document.getElementById('lb-sort').value = currentSort;
      renderLeaderboard();
    });
  });

  document.getElementById('lb-sort').addEventListener('change', function () {
    currentSort = this.value;
    renderLeaderboard();
  });

  document.getElementById('lb-type-filter').addEventListener('change', function () {
    currentFilter = this.value;
    renderLeaderboard();
  });

  document.getElementById('lb-turns-filter').addEventListener('change', function () {
    currentTurnsFilter = this.value;
    renderLeaderboard();
  });

  // Initial render
  renderLeaderboard();

})();
