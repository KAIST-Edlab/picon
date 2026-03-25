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

  // Community submissions (loaded from API or localStorage)
  var communityEntries = [];
  try {
    communityEntries = JSON.parse(localStorage.getItem('picon_community') || '[]');
  } catch (e) { /* ignore */ }

  // ===== Tab switching =====
  document.querySelectorAll('.demo-tab').forEach(function (tab) {
    tab.addEventListener('click', function () {
      document.querySelectorAll('.demo-tab').forEach(function (t) { t.classList.remove('active'); });
      document.querySelectorAll('.demo-panel').forEach(function (p) { p.classList.remove('active'); });
      tab.classList.add('active');
      document.getElementById('panel-' + tab.dataset.tab).classList.add('active');
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
    el.textContent = phase + ' — Q' + progress.current + '/' + progress.total;
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
    addMessage(expMessages, 'info', 'Starting interview for ' + name + '...');

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
      addMessage(expMessages, 'system', data.first_question);
      expSend.disabled = false;
      expInput.focus();
    } catch (err) {
      addMessage(expMessages, 'info', 'Error: ' + err.message);
    }
  });

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
        addMessage(expMessages, 'info', 'Interview complete! Calculating your consistency scores...');
        expSend.disabled = true;
        expInput.disabled = true;

        try {
          var rRes = await fetch(API_BASE + '/api/results/' + expSessionId);
          if (rRes.ok) {
            var results = await rRes.json();
            var expResultsEl = document.getElementById('exp-results');
            expResultsEl.style.display = 'block';
            renderScoreGrid(
              document.getElementById('exp-score-grid'),
              results.results.eval_scores || {}
            );
          }
        } catch (e) {
          addMessage(expMessages, 'info', 'Results saved. Thank you for participating!');
        }
        return;
      }

      if (data.next_question) {
        addMessage(expMessages, 'system', data.next_question);
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

  // ===== Agent Test Mode =====

  var agentTerminal = document.getElementById('agent-terminal-body');
  var agentProgress = document.getElementById('agent-progress');
  var agentSessionId = null;
  var agentLogIndex = 0;  // track how many log lines we've fetched

  document.getElementById('agent-start-btn').addEventListener('click', async function () {
    var name = document.getElementById('agent-name').value.trim();
    var model = document.getElementById('agent-model').value.trim();
    var endpoint = document.getElementById('agent-endpoint').value.trim();
    var apiKey = document.getElementById('agent-api-key').value.trim();
    var persona = document.getElementById('agent-persona').value.trim();
    var turns = document.getElementById('agent-turns').value;
    var sessions = document.getElementById('agent-sessions').value;

    if (!name) { alert('Please provide an agent name.'); return; }
    if (!model) { alert('Please provide a model name (e.g. gpt-4o, gemini/gemini-2.5-flash).'); return; }
    if (!apiKey) { alert('Please provide an API key for the model provider. You will be billed for the evaluation cost.'); return; }
    if (!persona) { alert('Please provide a persona / system prompt.'); return; }

    if (!API_BASE) {
      alert('Demo backend is not configured. Please set demo_api_url in _config.yml.');
      return;
    }

    document.getElementById('agent-form').style.display = 'none';
    document.getElementById('agent-log').style.display = 'block';
    agentTerminal.textContent = '';
    agentLogIndex = 0;

    appendTerminal('$ picon.run(' + name + ', model=' + model + ', turns=' + turns + ')\n');
    agentProgress.textContent = 'Starting evaluation...';

    try {
      var res = await fetch(API_BASE + '/api/agent/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: name,
          model: model,
          api_base: endpoint,
          api_key: apiKey,
          persona: persona,
          num_turns: parseInt(turns),
          num_sessions: parseInt(sessions),
        })
      });

      if (!res.ok) throw new Error('Failed to start: ' + res.status);
      var data = await res.json();
      agentSessionId = data.session_id;
      appendTerminal('Evaluation started. This may take several minutes...\n\n');

      // Poll for progress and logs
      pollAgentProgress(data.session_id);
    } catch (err) {
      appendTerminal('ERROR: ' + err.message + '\n');
      agentProgress.textContent = 'Error';
    }
  });

  function appendTerminal(text) {
    agentTerminal.textContent += text;
    agentTerminal.scrollTop = agentTerminal.scrollHeight;
  }

  async function fetchAgentLogs(sessionId) {
    try {
      var res = await fetch(API_BASE + '/api/agent/logs/' + sessionId + '?since=' + agentLogIndex);
      if (!res.ok) return;
      var data = await res.json();
      if (data.lines && data.lines.length > 0) {
        appendTerminal(data.lines.join('\n') + '\n');
        agentLogIndex = data.total;
      }
    } catch (e) { /* ignore */ }
  }

  async function pollAgentProgress(sessionId) {
    var interval = setInterval(async function () {
      try {
        // Fetch logs and status in parallel
        await fetchAgentLogs(sessionId);

        var res = await fetch(API_BASE + '/api/agent/status/' + sessionId);
        if (!res.ok) { clearInterval(interval); return; }
        var data = await res.json();

        agentProgress.textContent =
          'Session ' + data.current_session + '/' + data.total_sessions + ' — Running...';

        if (data.is_complete) {
          clearInterval(interval);

          // Fetch final logs
          await fetchAgentLogs(sessionId);

          if (data.error) {
            agentProgress.textContent = 'Error';
            appendTerminal('\nERROR: ' + data.error + '\n');
            return;
          }

          agentProgress.textContent = 'Complete';
          appendTerminal('\n--- Evaluation complete ---\n');

          // Fetch results
          var rRes = await fetch(API_BASE + '/api/agent/results/' + sessionId);
          if (rRes.ok) {
            var results = await rRes.json();
            document.getElementById('agent-log').style.display = 'none';
            document.getElementById('agent-results').style.display = 'block';
            renderScoreGrid(
              document.getElementById('agent-score-grid'),
              results.scores || {}
            );

            // Add to community leaderboard
            var entry = {
              name: results.name || 'Agent',
              type: 'community',
              arch: 'Community',
              ic: results.scores.ic || 0,
              ec: results.scores.ec || 0,
              rc: results.scores.rc || 0,
            };
            entry.area = computeArea(entry);
            communityEntries.push(entry);
            localStorage.setItem('picon_community', JSON.stringify(communityEntries));
            renderLeaderboard();
          }
        }
      } catch (err) {
        clearInterval(interval);
        appendTerminal('\nERROR: ' + err.message + '\n');
      }
    }, 3000);
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
      document.getElementById('agent-form').style.display = 'flex';
      agentTerminal.textContent = '';
      agentLogIndex = 0;
    });
  }

  // Retry button
  var retryBtn = document.getElementById('agent-retry-btn');
  if (retryBtn) {
    retryBtn.addEventListener('click', function () {
      document.getElementById('agent-results').style.display = 'none';
      document.getElementById('agent-form').style.display = 'flex';
      agentTerminal.textContent = '';
      agentLogIndex = 0;
    });
  }

  // ===== Leaderboard =====

  var currentSort = 'area';
  var currentFilter = 'all';
  var currentTurnsFilter = 'all';

  function getAllEntries() {
    return BASELINES.concat(communityEntries);
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
