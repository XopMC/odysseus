"""Auto-compaction toast shows pre- and post-checkpoint usage, not just success."""
import subprocess
from pathlib import Path


def test_compaction_notice_explains_threshold_and_rejects_missing_measurements():
    module = (Path(__file__).resolve().parents[1] / 'static/js/context-compaction-notice.js').as_uri()
    script = r'''
      import assert from 'node:assert/strict';
      const {compactionToastText} = await import(process.argv[1]);
      const event = {before_percent:76.1, after_percent:16.5, trigger_percent:75};
      assert.equal(compactionToastText(event), 'Auto compact: ≈76.1% → ≈16.5% (threshold 75%)');
      const translate = text => ({'Auto compact':'Автосжатие','threshold':'порог'})[text] || text;
      assert.equal(compactionToastText(event, translate), 'Автосжатие: ≈76.1% → ≈16.5% (порог 75%)');
      assert.equal(compactionToastText({type:'compacted'}), 'Context compacted — older messages summarized');
      assert.equal(compactionToastText({before_percent:35, after_percent:76}),
        'Context compacted — older messages summarized');
    '''
    result = subprocess.run(['node', '--input-type=module', '-e', script, module],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
