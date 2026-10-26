import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function render(states) {
  let Card;
  class Element {
    attachShadow() {
      this.shadowRoot = {
        innerHTML: '', querySelectorAll: () => [], querySelector: () => null,
      };
    }
  }
  vm.runInNewContext(readFileSync(new URL('../lovelace/climate-x-glance-card.js', import.meta.url), 'utf8'), {
    HTMLElement: Element,
    customElements: { get: () => undefined, define: (_, type) => { Card = type; } },
    window: {},
  });
  const card = new Card();
  card.setConfig({ entities: { hlc: 'sensor.hlc', dhw: 'sensor.dhw', gas_7d: 'sensor.hw', heat_7d: 'sensor.heat' }, rooms: [] });
  card.hass = { states };
  return card.shadowRoot.innerHTML;
}

test('retained numeric HLC carries source warning and evidence dates', () => {
  const html = render({ 'sensor.hlc': { state: '307.2', attributes: {
    status: 'source_problem', reason: 'Expected heating coverage incomplete',
    model_data_through: '2026-10-09', raw_latest_source_day: '2026-10-25',
  } } });
  assert.match(html, /307/);
  assert.match(html, /role="status">source problem/);
  assert.match(html, /Expected heating coverage incomplete/);
  assert.match(html, /Model data 2026-10-09/);
  assert.match(html, /Source through 2026-10-25/);
  assert.match(html, /conditional regression interval/);
});

test('unknown allocation stays unavailable, not measured zero', () => {
  const html = render({ 'sensor.hw': { state: 'unknown' }, 'sensor.heat': { state: 'unknown' } });
  assert.match(html, /7d gas allocation unavailable/);
  assert.doesNotMatch(html, /hot water 0\.0 \+ space heating 0\.0/);
});

test('held baseline and gas evidence date remain visible', () => {
  const html = render({ 'sensor.dhw': { state: '10.7', attributes: {
    status: 'historical_baseline_held', latest_complete_gas_day: '2026-10-09',
  } } });
  assert.match(html, /historical baseline held/);
  assert.match(html, /Source through 2026-10-09/);
});

test('source reason is escaped as data', () => {
  const html = render({ 'sensor.hlc': { state: '20', attributes: {
    status: 'source_problem', reason: '<script>bad()</script>',
  } } });
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>bad/);
});
