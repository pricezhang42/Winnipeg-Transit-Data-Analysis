// Timestamps are Winnipeg wall-clock strings, so filtering never uses browser timezone.
export function filterRows(rows, {from, to, route = '', type = '', query = ''}, index) {
  const needle = query.trim().toLowerCase();
  return rows.filter(r => r[1].slice(0, 10) >= from && r[1].slice(0, 10) <= to
    && (route === '' || r[2] === Number(route))
    && (type === '' || r[3] === Number(type))
    && (!needle || [r[0], index.routes[r[2]], index.destinations[r[4]]]
      .some(value => String(value).toLowerCase().includes(needle))));
}

export function summarize(rows, from, to) {
  const months = new Map(), routes = new Map(), types = new Map();
  const hours = Array(24).fill(0), weekdays = Array(7).fill(0);
  // Include months without matching reports; counts do not establish service coverage.
  for (let month = from.slice(0, 7); month <= to.slice(0, 7);) {
    months.set(month, 0);
    const [year, number] = month.split('-').map(Number);
    month = number === 12 ? `${year + 1}-01` : `${year}-${String(number + 1).padStart(2, '0')}`;
  }
  let missingLocation = 0;
  for (const r of rows) {
    const month = r[1].slice(0, 7);
    months.set(month, (months.get(month) || 0) + 1);
    routes.set(r[2], (routes.get(r[2]) || 0) + 1);
    types.set(r[3], (types.get(r[3]) || 0) + 1);
    hours[Number(r[1].slice(11, 13))]++;
    weekdays[(new Date(r[1].slice(0, 10) + 'T12:00:00Z').getUTCDay() + 6) % 7]++;
    missingLocation += r[5] === null;
  }
  return {records: rows.length, months: [...months], routes: [...routes].sort((a,b) => b[1]-a[1]),
    types: [...types], hours, weekdays, missingLocation};
}
