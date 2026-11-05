const $ = (selector) => document.querySelector(selector);

const state = {
  employees: [],
  selectedId: null,
  report: null,
  reportRequestId: 0,
  busy: false,
};

const utcDay = (date) => date.toISOString().slice(0, 10);
const today = new Date();
$('#date-to').value = utcDay(today);
$('#date-from').value = utcDay(new Date(today.getTime() - 6 * 86400000));

function duration(seconds, showSeconds = false) {
  const amount = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(amount / 3600);
  const minutes = Math.floor((amount % 3600) / 60);
  if (showSeconds) {
    return [hours, minutes, amount % 60].map(n => String(n).padStart(2, '0')).join(':');
  }
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${amount % 60}s`;
  return `${amount}s`;
}

function dateTime(iso) {
  return iso ? `${iso.slice(0, 10)} ${iso.slice(11, 16)}` : '—';
}

function initials(name) {
  return name.trim().split(/\s+/).slice(0, 2).map(word => word[0].toUpperCase()).join('');
}

function element(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}

async function api(path, options) {
  const response = await fetch(path, options);
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error('The server returned an unexpected response.');
  }
  if (!response.ok) throw new Error(data.error || 'The request could not be completed.');
  return data;
}

function showError(message) {
  const notice = $('#notice');
  notice.textContent = message;
  notice.hidden = false;
}

function clearError() {
  $('#notice').hidden = true;
}

async function fetchEmployees() {
  const data = await api('/api/employees');
  state.employees = data.employees;
  if (!state.employees.some(employee => employee.id === state.selectedId)) {
    state.selectedId = state.employees[0]?.id ?? null;
  }
  drawEmployees();
  drawTimer();
}

async function fetchReport() {
  const requestId = ++state.reportRequestId;
  const from = $('#date-from').value;
  const to = $('#date-to').value;
  try {
    if (from > to) throw new Error('The end date must be on or after the start date.');
    const query = new URLSearchParams({ from, to });
    const report = await api(`/api/report?${query}`);
    if (requestId !== state.reportRequestId) return;
    state.report = report;
    drawReport();
  } catch (error) {
    if (requestId === state.reportRequestId) throw error;
  }
}

async function refresh() {
  await Promise.all([fetchEmployees(), fetchReport()]);
  drawReport();
}

function drawEmployees() {
  const list = $('#employees');
  list.replaceChildren();
  $('#employee-count').textContent = state.employees.length;
  if (!state.employees.length) {
    list.append(element('p', 'team-empty', 'No employees yet. Add someone below to get started.'));
    return;
  }

  for (const employee of state.employees) {
    const button = element('button', 'employee-button');
    button.type = 'button';
    button.classList.toggle('selected', employee.id === state.selectedId);
    button.setAttribute('aria-pressed', String(employee.id === state.selectedId));
    button.append(element('span', 'avatar', initials(employee.name)));
    const info = element('span', 'employee-meta');
    info.append(element('strong', '', employee.name));
    info.append(element('small', '', employee.active_entry ? 'Tracking time' : 'Not tracking'));
    button.append(info);
    if (employee.active_entry) button.append(element('span', 'active-dot'));
    button.addEventListener('click', () => {
      state.selectedId = employee.id;
      drawEmployees();
      drawTimer();
    });
    list.append(button);
  }
}

function currentEmployee() {
  return state.employees.find(employee => employee.id === state.selectedId);
}

function drawTimer() {
  const employee = currentEmployee();
  const active = employee?.active_entry;
  $('#timer-heading').textContent = employee ? employee.name : 'Select an employee';
  $('#timer-status').textContent = active ? '● Running' : 'Not running';
  $('#timer-status').classList.toggle('running', Boolean(active));

  const task = $('#task-description');
  task.disabled = !employee || Boolean(active) || state.busy;
  if (active) {
    task.value = active.description;
    $('#task-help').textContent = 'Stop this session before starting another task.';
  } else {
    if (task.dataset.employeeId !== String(state.selectedId)) task.value = '';
    $('#task-help').textContent = employee
      ? 'Enter a task description before starting the timer.'
      : 'Add an employee to start tracking time.';
  }
  task.dataset.employeeId = String(state.selectedId);

  const button = $('#toggle-timer');
  button.disabled = !employee || state.busy;
  button.classList.toggle('stop', Boolean(active));
  button.textContent = active ? 'Stop timer' : 'Start timer';
  $('#timer-message').textContent = active ? 'This session will be saved when stopped.' : 'One active timer per employee.';
  tickClock();
}

function tickClock() {
  const started = currentEmployee()?.active_entry?.started_at;
  const seconds = started ? Math.floor((Date.now() - Date.parse(started)) / 1000) : 0;
  $('#timer-clock').textContent = duration(seconds, true);
}

function drawReport() {
  if (!state.report) return;
  const report = state.report;
  const records = report.employees.flatMap(employee => employee.records.map(record => ({ ...record, employee: employee.name })));
  records.sort((a, b) => b.started_at.localeCompare(a.started_at) || b.id - a.id);
  $('#stat-time').textContent = duration(report.total_seconds);
  $('#stat-entries').textContent = records.length;
  $('#stat-active').textContent = state.employees.filter(employee => employee.active_entry).length;
  $('#record-count').textContent = `${records.length} ${records.length === 1 ? 'record' : 'records'}`;
  $('#report-period').textContent = `${report.from} — ${report.to}`;
  $('#report-updated').textContent = `Report generated ${dateTime(report.generated_at)} UTC · Durations are counted within the selected UTC dates.`;

  const totals = $('#employee-totals');
  totals.replaceChildren();
  if (!report.employees.length) {
    totals.append(element('p', 'totals-empty', 'Employee totals will appear here.'));
  }
  for (const employee of report.employees) {
    const item = element('div', 'employee-total');
    item.append(element('span', 'avatar', initials(employee.name)));
    item.append(element('span', 'name', employee.name));
    item.append(element('strong', 'time', duration(employee.total_seconds)));
    totals.append(item);
  }

  const tbody = $('#records');
  tbody.replaceChildren();
  for (const record of records) {
    const row = document.createElement('tr');
    row.append(element('td', '', record.employee));
    row.append(element('td', 'task', record.description));
    row.append(element('td', '', dateTime(record.started_at)));
    const ended = element('td');
    if (record.is_running) ended.append(element('span', 'running-tag', 'Running'));
    else ended.textContent = dateTime(record.ended_at);
    row.append(ended);
    row.append(element('td', 'align-right', duration(record.duration_seconds)));
    tbody.append(row);
  }
  $('#empty-report').hidden = records.length > 0;
}

async function perform(action) {
  if (state.busy) return;
  state.busy = true;
  clearError();
  drawTimer();
  try {
    await action();
    await refresh();
  } catch (error) {
    showError(error.message || 'Something went wrong.');
    try { await fetchEmployees(); } catch { /* Keep the current view if offline. */ }
  } finally {
    state.busy = false;
    drawTimer();
  }
}

$('#add-employee-form').addEventListener('submit', event => {
  event.preventDefault();
  const input = $('#employee-name');
  const name = input.value.trim();
  if (!name) return;
  perform(async () => {
    const added = await api('/api/employees', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
    });
    state.selectedId = added.id;
    input.value = '';
  });
});

$('#toggle-timer').addEventListener('click', () => {
  const employee = currentEmployee();
  if (!employee) return;
  const active = Boolean(employee.active_entry);
  const description = $('#task-description').value.trim();
  if (!active && !description) {
    showError('Enter a task description before starting a timer.');
    $('#task-description').focus();
    return;
  }
  perform(async () => {
    await api(active ? '/api/timer/stop' : '/api/timer/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(active ? { employee_id: employee.id } : { employee_id: employee.id, description }),
    });
    if (active) $('#task-description').value = '';
  });
});

$('#report-form').addEventListener('submit', event => {
  event.preventDefault();
  clearError();
  fetchReport().catch(error => showError(error.message));
});

setInterval(tickClock, 1000);
refresh().catch(error => showError(error.message));
