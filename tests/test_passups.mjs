import test from 'node:test';
import assert from 'node:assert/strict';
import {filterRows,summarize} from '../dashboard/dist/assets/passups-core.mjs';
const index = {routes:['BLUE',''],destinations:['South','']};
const rows = [
  ['a','2026-01-31T23:59:59',0,0,0,[-97,49],1],
  ['b','2026-02-01T00:00:00',1,1,1,null,2],
  ['c','2026-02-01T23:59:59',0,0,0,null,3],
];
test('inclusive dates and combined filters preserve unknown routes', () => {
  assert.equal(filterRows(rows,{from:'2026-02-01',to:'2026-02-01'},index).length,2);
  assert.deepEqual(filterRows(rows,{from:'2026-01-01',to:'2026-02-28',route:'1',type:'1'},index),[rows[1]]);
  assert.deepEqual(filterRows(rows,{from:'2026-01-01',to:'2026-02-28',query:'SOUTH'},index),[rows[0],rows[2]]);
});
test('aggregations reconcile and use source local dates across midnight', () => {
  const result = summarize(rows,'2026-01-01','2026-03-31');
  assert.deepEqual(result.months,[['2026-01',1],['2026-02',2],['2026-03',0]]);
  assert.equal(result.hours[23],2);
  assert.equal(result.hours[0],1);
  assert.equal(result.weekdays[5],1);
  assert.equal(result.weekdays[6],2);
  assert.equal(result.missingLocation,2);
  for (const grouping of [result.months,result.routes,result.types]) assert.equal(grouping.reduce((n,r)=>n+r[1],0),rows.length);
});
test('empty selection and year rollover', () => {
  const result = summarize([],'2025-12-31','2026-01-01');
  assert.deepEqual(result.months,[['2025-12',0],['2026-01',0]]);
  assert.equal(result.records,0);
});
