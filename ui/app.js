'use strict';
const $ = id => document.getElementById(id);
let state = null, sequence = 0, operating = false, logLines = [];

function log(text) {
  logLines.push(text);
  logLines = logLines.slice(-1000);
  const nearBottom = $('log').scrollHeight - $('log').scrollTop - $('log').clientHeight < 40;
  $('log').textContent = logLines.join('\n');
  if (nearBottom) $('log').scrollTop = $('log').scrollHeight;
}

function options(element, items, selected) {
  const signature = JSON.stringify(items);
  if (element.dataset.options !== signature) {
    element.replaceChildren(...items.map(([value, label]) => {
      const option = document.createElement('option');
      option.value = value; option.textContent = label; return option;
    }));
    element.dataset.options = signature;
  }
  if (selected !== undefined) element.value = selected;
}

function render() {
  if (!state) return;
  const busy = operating || state.busy || !state.data_ready;
  for (const button of document.querySelectorAll('[data-action]')) button.disabled = busy;
  const selection = $('accounts').value, kept = state.accounts.some(a => a.id === selection);
  options($('accounts'), state.accounts.map(a => [a.id, `${a.id === state.active ? '▶ ' : ''}${a.name || '(no name yet)'}   ${a.id}`]), kept ? selection : state.active || '');
  $('use').hidden = state.accounts.length < 2;
  $('use').disabled = busy || state.game_running || !$('accounts').value;
  $('remove').hidden = !state.accounts.length;
  $('remove').disabled = busy || state.running || state.game_running || !$('accounts').value;
  $('data-folder').textContent = state.data_folder || '';
  $('server-state').textContent = state.server;
  $('start').disabled = busy || state.running || !state.client_data.ready;
  $('stop').disabled = busy || !state.running;
  $('play').disabled = busy || !state.ready || state.game_running;
  options($('season'), [['0', 'None (all ended)'], ...state.seasons.map(s => [String(s.id), s.label])], state.season);
  $('season').disabled = busy || state.running;
  const currentIP = $('ip').value;
  options($('ip'), state.network.addresses.map(ip => [ip, ip]), state.network.addresses.includes(currentIP) ? currentIP : state.network.addresses[0]);
  $('game-info').textContent = state.game;
  const clientData = state.client_data;
  $('client-data-find').disabled = busy || state.running;
  $('client-data-source').disabled = busy || state.running;
  $('client-data-find').textContent = clientData.running ? 'Finding files…' : 'Find MasterData & LocalizeText';
  $('client-data-folder').textContent = clientData.source ? `Source: ${clientData.source}` : 'Searches gameassets, your Documents player folders, and the game installation.';
  $('client-data-info').textContent = clientData.label;
  $('protect').textContent = state.copy ? 'Protected' : 'Protect…';
  $('protect').disabled = busy || state.running || state.game_running || state.copy || !state.installed;
  $('use-copy').textContent = state.copy ? 'Change folder…' : 'Use a copy…';
  $('use-copy').disabled = busy || state.running || state.game_running;
  $('operation').textContent = !state.data_ready ? 'Setting up…' : busy ? 'Working…' : '';
}

async function refresh() {
  try {
    const next = await window.pywebview.api.status(sequence);
    if (next.error) { log(next.error); return; }
    state = next;
    for (const message of state.messages) { sequence = Math.max(sequence, message.id); log(message.text); }
    render();
  } catch (error) { log(String(error)); }
}

async function act(name, value) {
  if (operating) return;
  operating = true; render();
  try { await window.pywebview.api.action(name, value); }
  catch (error) { log(String(error)); }
  finally { operating = false; await refresh(); }
}

for (const button of document.querySelectorAll('[data-action]')) {
  button.addEventListener('click', () => {
    $('connection').open = false;
    const name = button.dataset.action;
    act(name, ['use', 'remove'].includes(name) ? $('accounts').value : null);
  });
}
$('accounts').addEventListener('change', render);
$('season').addEventListener('change', () => act('season', $('season').value));
$('dns').addEventListener('click', async () => {
  $('connection').open = false;
  if (!$('ip').value || !state) { log('No LAN address found. Connect to your local network and refresh IP addresses.'); return; }
  try {
    await navigator.clipboard.writeText(state.network.hosts.map(host => `${$('ip').value} ${host}`).join('\r\n') + '\r\n');
    log('DNS rewrites copied. Use this PC’s LAN address in your phone connector.');
  } catch (error) { log(`Could not copy DNS rewrites: ${error}`); }
});
window.addEventListener('pywebviewready', async () => {
  await window.pywebview.api.initialize();
  await refresh();
  async function poll() { await refresh(); setTimeout(poll, 1000); }
  setTimeout(poll, 1000);
});
