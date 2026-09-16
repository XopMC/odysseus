// Pure chat timeline reducer shared by foreground streaming and replay.
// Rendering layers may decorate the returned state, but event identity and
// segment/tool transitions live in one place so reload cannot invent a second
// conversation shape.

function textOf(value) {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(item => item?.text || '').join('');
  return value == null ? '' : String(value);
}

export function createTimelineReducer() {
  const state = {
    runId: '', lastSeq: -1, segmentId: '', round: 0,
    segments: [], tools: new Map(), seen: new Set(), context: null,
    terminal: null,
  };

  function apply(raw, seqOverride = null) {
    const event = raw && typeof raw === 'object' ? raw : {};
    const replay = event._replay && typeof event._replay === 'object' ? event._replay : {};
    const runId = String(replay.run_id || event.run_id || state.runId || '');
    const seq = Number.isInteger(replay.seq) ? replay.seq
      : Number.isInteger(seqOverride) ? seqOverride : null;
    const identity = runId && seq != null ? `${runId}:${seq}` : '';
    if (identity && state.seen.has(identity)) return { accepted: false, state };
    if (identity) state.seen.add(identity);
    if (runId) state.runId = runId;
    if (seq != null) state.lastSeq = Math.max(state.lastSeq, seq);
    const type = String(event.type || (event.delta ? 'text' : 'unknown'));
    const segmentId = String(replay.segment_id || event.segment_id || '');
    if (segmentId) state.segmentId = segmentId;
    if (Number.isInteger(event.round)) state.round = Math.max(state.round, event.round);
    if (type === 'agent_step') {
      const round = Number(event.round || state.round + 1);
      state.round = Math.max(state.round, round);
      state.segmentId = segmentId || `${state.runId}:${round}`;
      state.segments.push({ id: state.segmentId, round, thinking: '', text: '', tools: [] });
    } else if (event.delta != null) {
      let segment = state.segments[state.segments.length - 1];
      if (!segment || (segmentId && segment.id !== segmentId)) {
        const id = segmentId || `${state.runId}:${state.round || 1}`;
        segment = { id, round: state.round || 1, thinking: '', text: '', tools: [] };
        state.segments.push(segment);
        state.segmentId = id;
      }
      const delta = textOf(event.delta);
      if (event.thinking === true || event.channel === 'thinking' || event.channel === 'thought') segment.thinking += delta;
      else segment.text += delta;
    } else if (type === 'tool_start' || type === 'tool_progress' || type === 'tool_output') {
      const toolId = String(event.tool_call_id || replay.tool_call_id || `${state.runId}:tool:${state.tools.size + 1}`);
      let tool = state.tools.get(toolId);
      if (!tool) {
        tool = { id: toolId, tool: String(event.tool || 'Tool'), command: '', output: '', status: 'running', progress: '' };
        state.tools.set(toolId, tool);
        let segment = state.segments[state.segments.length - 1];
        if (!segment) {
          segment = { id: state.segmentId || `${state.runId}:${state.round || 1}`, round: state.round || 1, thinking: '', text: '', tools: [] };
          state.segments.push(segment);
        }
        segment.tools.push(toolId);
      }
      if (event.tool) tool.tool = String(event.tool);
      if (event.command) tool.command = String(event.command);
      if (type === 'tool_progress') tool.progress = textOf(event.tail ?? event.message ?? '');
      if (type === 'tool_output') {
        tool.output = textOf(event.output);
        tool.status = event.exit_code == null || event.exit_code === 0 ? 'done' : 'error';
        tool.exitCode = event.exit_code;
      }
    } else if (type === 'context_usage') {
      state.context = event.data || null;
    } else if (type === 'agent_terminal' || type === 'chat_terminal' || type === 'done') {
      state.terminal = event.data || event;
    }
    return { accepted: true, state };
  }

  function snapshot() {
    return {
      ...state,
      segments: state.segments.map(segment => ({ ...segment, tools: [...segment.tools] })),
      tools: Object.fromEntries([...state.tools].map(([id, value]) => [id, { ...value }])),
      seen: undefined,
    };
  }

  return { apply, snapshot, state };
}

export default { createTimelineReducer };
