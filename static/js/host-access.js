'use strict';
const secureHostOrigin = location.protocol === 'https:' || ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);
if (!secureHostOrigin) {
    document.getElementById('host-form').hidden = true;
    document.getElementById('password').disabled = true;
    document.getElementById('result').textContent = 'Use HTTPS or a localhost SSH tunnel. Password entry is disabled on plain LAN HTTP.';
}
document.getElementById('host-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!secureHostOrigin) return;
    const button = document.getElementById('run');
    const output = document.getElementById('result');
    const password = document.getElementById('password');
    const body = JSON.stringify({command: document.getElementById('command').value, password: password.value});
    password.value = '';
    button.disabled = true;
    output.textContent = 'Running the explicitly authorized command…';
    try {
        const response = await fetch('/api/host-access/sudo', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Odysseus-Host-Action': 'one-shot-sudo'}, body});
        const result = await response.json();
        output.textContent = result.output || result.error || result.detail || JSON.stringify(result);
        if (result.exit_code !== undefined) output.textContent += '\nExit code: ' + result.exit_code;
    } catch (_) {
        output.textContent = 'Connection lost; command state is unknown. Inspect the host before retrying.';
    } finally { button.disabled = false; }
});
