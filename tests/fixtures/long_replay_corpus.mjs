// Deterministic, content-free 1K/10K/100K trajectory generator.
export function* longReplayCorpus(count) {
  const run = 'e'.repeat(32);
  for (let seq = 0; seq < count; seq++) {
    const round = Math.floor(seq / 5) + 1;
    const toolCallId = `fixture-tool-${round}`;
    let event;
    switch (seq % 5) {
      case 0: event = { type: 'agent_step', round }; break;
      case 1: event = { delta: '[thinking fixture]', thinking: true, round }; break;
      case 2: event = { type: 'tool_start', tool: 'fixture_tool', tool_call_id: toolCallId, round }; break;
      case 3: event = { type: 'tool_output', tool: 'fixture_tool', tool_call_id: toolCallId, exit_code: 0, round }; break;
      default: event = { delta: '[answer fixture]', round };
    }
    yield {
      ...event,
      _replay: { run_id: run, seq, segment_id: `${run}:${round}`, tool_call_id: event.tool_call_id },
    };
  }
}
