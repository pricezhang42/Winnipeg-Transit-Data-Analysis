import {filterRows, summarize} from './passups-core.mjs';
const $ = selector => document.querySelector(selector);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = value => value.toLocaleString('en-CA');
const state = {index:null, cache:new Map(), rows:[], page:0};
async function json(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`Cannot load ${path} (HTTP ${response.status})`);
  return response.json();
}
function bars(id, entries) {
  const max = Math.max(1, ...entries.map(([,n]) => n));
  $(id).innerHTML = entries.length ? entries.map(([label,n]) =>
    `<div class="bar-row"><span class="bar-label">${escape(label)}</span><span class="bar-track" aria-hidden="true"><span class="bar-fill" style="width:${n/max*100}%"></span></span><strong class="bar-count">${number(n)}</strong></div>`).join('') : '<p class="history-empty">No reports match these filters.</p>';
}
function table() {
  const i = state.index, offset = state.page * 100;
  $('#records').innerHTML = state.rows.slice(offset, offset+100).map(r => `<tr>
    <td>${escape(r[1].replace('T',' '))}</td><td>${escape(i.routes[r[2]] || 'Unknown')}</td>
    <td>${escape(i.destinations[r[4]] || 'Unknown')}</td><td>${escape(i.types[r[3]] || 'Unknown')}</td>
    <td>${r[5] ? r[5].map(v => v.toFixed(5)).join(', ') : 'Unavailable'}</td><td>${escape(r[0])}</td><td>${r[6]}</td></tr>`).join('') || '<tr><td colspan="7" class="history-empty">No reports match these filters.</td></tr>';
  $('#page').textContent = state.rows.length ? `${number(offset+1)}–${number(Math.min(offset+100,state.rows.length))} of ${number(state.rows.length)}` : '0 records';
  $('#previous').disabled = state.page === 0;
  $('#next').disabled = offset+100 >= state.rows.length;
}
async function apply() {
  const selection = Object.fromEntries(['from','to','route','type','query'].map(key => [key,$('#'+key).value]));
  if (!selection.from || !selection.to || selection.from > selection.to) {
    $('#status').textContent = 'Choose a valid date range with From on or before Through.'; return;
  }
  $('#controls').disabled = true;
  $('#status').className = '';
  $('#status').textContent = 'Loading selected months…';
  try {
    const months = state.index.months.filter(m => m.month >= selection.from.slice(0,7) && m.month <= selection.to.slice(0,7));
    let next = 0;
    await Promise.all(Array.from({length: Math.min(6, months.length)}, async () => {
      while (next < months.length) {
        const {month} = months[next++];
        if (!state.cache.has(month)) state.cache.set(month, await json(`passups/${month}.json`));
      }
    }));
    const rows = filterRows(months.flatMap(m => state.cache.get(m.month)), selection, state.index);
    const summary = summarize(rows, selection.from, selection.to);
    state.rows = rows.sort((a,b) => b[1].localeCompare(a[1]) || b[6]-a[6]); state.page = 0;
    const unknown = rows.filter(r => !state.index.routes[r[2]]).length;
    $('#stats').innerHTML = [
      ['Reported pass-ups',number(rows.length),'Source records in this selection'],
      ['Known routes',number(summary.routes.filter(([id]) => state.index.routes[id]).length),`${number(unknown)} reports with unknown route`],
      ['Missing coordinates',number(summary.missingLocation),'Included in all report totals'],
    ].map(([label,value,hint]) => `<div class="stat"><span class="label">${label}</span><strong class="value">${value}</strong><span class="hint">${hint}</span></div>`).join('');
    bars('#months',summary.months);
    bars('#routes',summary.routes.slice(0,15).map(([id,n]) => [state.index.routes[id] || 'Unknown',n]));
    bars('#hours',summary.hours.map((n,h) => [`${String(h).padStart(2,'0')}:00`,n]));
    bars('#types',summary.types.map(([id,n]) => [state.index.types[id] || 'Unknown',n]));
    bars('#weekdays',summary.weekdays.map((n,d) => [['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'][d],n]));
    table();
    $('#selection').textContent = `${selection.from} through ${selection.to} · Newest first · Winnipeg local time`;
    $('#status').textContent = `${number(rows.length)} reports match the applied filters. Charts show report counts; zero counts do not establish complete reporting coverage.`;
  } catch (error) {
    $('#status').className = 'history-error';
    $('#status').textContent = `${error.message}. Retry Apply filters. Any previous results still show the previous selection.`;
  } finally { $('#controls').disabled = false; }
}
$('#filters').addEventListener('submit', event => {event.preventDefault(); apply();});
$('#all').onclick = () => {$('#from').value = state.index.first; $('#to').value = state.index.last; apply();};
for (const [id,step] of [['previous',-1],['next',1]]) $('#'+id).onclick = () => {state.page += step; table(); $('.history-table').scrollTop = 0;};
try {
  const i = state.index = await json('passups/index.json');
  $('#coverage').textContent = `${number(i.records)} reports · ${i.first} to ${i.last}`;
  $('#source').textContent = i.sourceFile;
  $('#quality').textContent = `Full snapshot: ${number(i.missingRoute)} reports without route numbers; ${number(i.invalidLocation)} without usable coordinates; ${number(i.additionalDuplicateIds)} additional rows with repeated IDs. Exported ${i.exportedAt.slice(0,10)}.`;
  for (const [id,values] of [['route',i.routes],['type',i.types]]) {
    $('#'+id).innerHTML += values.map((label,index) => ({label,index})).sort((a,b) => a.label.localeCompare(b.label,undefined,{numeric:true})).map(({label,index}) => `<option value="${index}">${escape(label || 'Unknown')}</option>`).join('');
  }
  for (const id of ['from','to']) {$('#'+id).min = i.first; $('#'+id).max = i.last;}
  const [year,month] = i.last.split('-').map(Number);
  const start = new Date(Date.UTC(year,month-12,1)).toISOString().slice(0,10);
  $('#from').value = start < i.first ? i.first : start; $('#to').value = i.last;
  await apply();
} catch (error) {
  $('#status').className = 'history-error';
  $('#status').textContent = `${error.message}. Serve dashboard/dist over HTTP and export pass-up data using the command in dashboard/README.md, then reload.`;
}
