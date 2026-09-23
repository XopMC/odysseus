// Convert a bounded, complete slice of durable run events into the same
// metadata shape used by the canonical history renderer. This is deliberately
// pure: the active-run pager decides when a round is complete and where its
// cards belong in the DOM.

function eventData(item) {
  return item?.data && typeof item.data === 'object' ? item.data : item;
}

export function replayThinkingStats(startedAt, endedAt, text) {
  const start = Number(startedAt);
  const end = Number(endedAt);
  const duration = Number.isFinite(start) && Number.isFinite(end) && start > 0 && end >= start
    ? (end - start) / 1000 : 0;
  const tokens = Math.max(1, Math.ceil(String(text || '').trim().length / 4));
  return `${duration.toFixed(1)}s · ${tokens} tok`;
}

export function splitReplayPageAtRoundBoundary(events) {
  const rows = Array.isArray(events) ? events : [];
  const firstStep = rows.findIndex(item => eventData(item)?.type === 'agent_step');
  if (firstStep < 0) return { incompletePrefix: rows, completeRounds: [] };
  return {
    incompletePrefix: rows.slice(0, firstStep),
    completeRounds: rows.slice(firstStep),
  };
}

export function replayEventsToHistoryMessage(events, { runId = '', model = '' } = {}) {
  const rows = Array.isArray(events) ? events : [];
  if (!rows.length) return null;
  const rounds = new Map();
  const tools = new Map();
  let currentRound = 1;
  let selectedModel = String(model || '');
  for (const item of rows) {
    const data = eventData(item);
    if (!data || typeof data !== 'object') continue;
    const replay = data._replay && typeof data._replay === 'object' ? data._replay : {};
    const rawRound = Number(data.round ?? replay.round ?? currentRound);
    const roundNumber = Number.isSafeInteger(rawRound) && rawRound > 0 ? rawRound : currentRound;
    if (data.type === 'agent_step') currentRound = roundNumber;
    else currentRound = Math.max(currentRound, roundNumber);
    if (!rounds.has(roundNumber)) {
      rounds.set(roundNumber, { thinking: [], text: [], timestamp: Number(replay.created_at) || 0,
        model: selectedModel });
    }
    const round = rounds.get(roundNumber);
    if (!round.timestamp && Number(replay.created_at) > 0) round.timestamp = Number(replay.created_at);
    if (data.type === 'model_actual') {
      selectedModel = String(data.model || selectedModel);
      round.model = selectedModel;
    } else if (data.type === 'fallback') {
      selectedModel = String(data.answered_by || selectedModel);
      round.model = selectedModel;
    }
    if (data.delta != null) {
      const channel = String(data.channel || '');
      const target = data.thinking === true || channel === 'thinking' || channel === 'thought'
        ? 'thinking' : 'text';
      round[target].push(String(data.delta));
    }
    if (!['tool_start', 'tool_progress', 'tool_output'].includes(data.type)) continue;
    const seq = Number(item?.seq ?? replay.seq);
    const id = String(data.tool_call_id || replay.tool_call_id || `seq-${seq}`);
    let tool = tools.get(id);
    if (!tool) {
      tool = { round: roundNumber, tool: String(data.tool || 'Tool'), command: '', output: '',
        exit_code: null, tool_call_id: id };
      tools.set(id, tool);
    }
    if (data.tool) tool.tool = String(data.tool);
    if (data.command) tool.command = String(data.command);
    if (data.type === 'tool_output') {
      tool.output = String(data.output || '');
      tool.exit_code = data.exit_code ?? null;
      for (const key of ['ask_user', 'diff', 'image_url', 'image_prompt', 'image_model',
        'image_size', 'image_quality', 'doc_id', 'doc_title']) {
        if (data[key] != null) tool[key] = data[key];
      }
    }
  }
  const ordered = [...rounds.entries()].sort((a, b) => a[0] - b[0]);
  if (!ordered.length) return null;
  const indexes = new Map(ordered.map(([number], index) => [number, index + 1]));
  const roundReasonings = ordered.map(([, round]) => round.thinking.join(''));
  const roundTexts = ordered.map(([, round], index) => {
    const reasoning = roundReasonings[index];
    return (reasoning ? `<think>\n${reasoning}\n</think>\n\n` : '') + round.text.join('');
  });
  const metadata = {
    model: selectedModel || String(model || ''),
    round_texts: roundTexts,
    round_reasonings: roundReasonings,
    round_timestamps: ordered.map(([, round]) => round.timestamp || null),
    round_models: ordered.map(([, round]) => round.model || selectedModel || String(model || '')),
    tool_events: [...tools.values()].map(tool => ({ ...tool, round: indexes.get(tool.round) || 1 })),
    replay_run_id: String(runId || ''),
    replay_preview: true,
  };
  return { role: 'assistant', content: roundTexts.join('\n\n'), metadata };
}
