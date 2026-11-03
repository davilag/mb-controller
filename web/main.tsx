import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';
import { ScheduleControl, type ChargeSchedule, type ScheduleConfig } from './schedule';

type Attribute = { value: unknown; display: unknown; unit: string | null; status: string; updatedAt: unknown };
type Command = { id: string; label: string; available: boolean; requiresPin: boolean; chargeLimits?: number[]; maxSoc?: number };
type Vehicle = { vin: string; name: string; plate: string; updatedAt: number | null; attributes: Record<string, Attribute>; commands: Command[]; lastCommand: { type: string; state: string; error: string; timestamp: number } };
type State = { configured: boolean; connected: boolean; connection: string; error: string | null; region: string; vehicles: Vehicle[]; schedule?: ChargeSchedule };

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, { ...options, headers: { 'Content-Type': 'application/json', ...options?.headers } });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed.');
  return data;
}

const emptyState: State = { configured: false, connected: false, connection: 'disconnected', error: null, region: 'Europe', vehicles: [] };
const metricDefinitions = [
  ['Battery', 'electric.soc', '%'], ['Fuel', 'odometer.tanklevelpercent', '%'],
  ['Electric range', 'electric.rangeelectric', ''], ['Fuel range', 'odometer.rangeliquid', ''],
  ['Odometer', 'odometer.odo', ''], ['Door lock', 'doors.doorlockstatusvehicle', ''],
] as const;

function display(attribute?: Attribute, suffix = '') {
  if (!attribute || attribute.value == null || !['VALID', '0'].includes(String(attribute.status))) return '—';
  const value = attribute.display ?? attribute.value;
  const unit = suffix || ({ KM: 'km', MILES: 'mi', BAR: 'bar', KPA: 'kPa', PERCENT: '%' }[attribute.unit ?? ''] ?? attribute.unit ?? '');
  return `${typeof value === 'object' ? JSON.stringify(value) : String(value)}${unit === '%' ? '' : ' '}${unit}`.trim();
}

function ChargeLimitControl({ car, connected, busy, scheduled, onConfirm }: { car: Vehicle; connected: boolean; busy: boolean; scheduled: boolean; onConfirm: (command: Command) => void }) {
  const [target, setTarget] = useState('');
  const command = car.commands.find(item => item.id === 'charge_limit');
  const options = command?.chargeLimits ?? [];
  const disabled = !command?.available || !connected || busy || scheduled;
  const current = car.attributes['electric.max_soc']?.status === 'VALID' ? car.attributes['electric.max_soc'] : car.attributes['electric.maxSoc'];
  return <section className="panel charge-limit">
    <div><h2>Charge limit</h2><p>Charge up to your chosen battery percentage.</p><p className="small">Reported limit: <strong>{display(current, '%')}</strong> · Current battery: {display(car.attributes['electric.soc'], '%')}</p>{!command?.available ? <p className="small">Charge-limit changes are not reported as supported for this vehicle.</p> : null}</div>
    <form onSubmit={event => { event.preventDefault(); if (command && options.includes(Number(target))) onConfirm({ ...command, maxSoc: Number(target) }); }}>
      <label>Target charge percentage<select aria-label="Target charge percentage" value={target} onChange={event => setTarget(event.target.value)} required disabled={disabled}><option value="" disabled>Select a limit</option>{options.map(value => <option key={value} value={value}>{value}%</option>)}</select></label>
      <button className="primary" disabled={disabled || !options.includes(Number(target))}>{busy ? 'Please wait…' : 'Set charge limit'}</button>
      <p className="small">{scheduled ? 'The daily schedule manages this limit. Turn it off to set a manual limit.' : 'Keeps the active charging program. Changes apply after Mercedes confirms them.'}</p>
    </form>
  </section>;
}

function App() {
  const [state, setState] = useState<State>(emptyState);
  const [vin, setVin] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [showConnect, setShowConnect] = useState(false);
  const [selectedCommand, setSelectedCommand] = useState<Command | null>(null);
  const [filter, setFilter] = useState('');
  const [pin, setPin] = useState('');
  const dialog = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const data = await api<State>('/state');
        if (active) { setState(data); setVin(current => data.vehicles.some(v => v.vin === current) ? current : data.vehicles[0]?.vin ?? ''); }
      } catch (e) { if (active) setError((e as Error).message); }
      if (active) timer = setTimeout(poll, 2500);
    }
    void poll();
    return () => { active = false; clearTimeout(timer); };
  }, []);

  useEffect(() => {
    if (selectedCommand) dialog.current?.showModal();
    else dialog.current?.close();
  }, [selectedCommand]);

  const car = state.vehicles.find(v => v.vin === vin);

  async function saveSchedule(config: ScheduleConfig) {
    const schedule = await api<ChargeSchedule>('/schedule', { method: 'PUT', body: JSON.stringify(config) });
    setState(current => ({ ...current, schedule }));
    return schedule;
  }

  async function disableSchedule() {
    const schedule = await api<ChargeSchedule>('/schedule', { method: 'DELETE' });
    setState(current => ({ ...current, schedule }));
    return schedule;
  }

  async function run(action: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('');
    try { await action(); } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  async function connect(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const values = new FormData(form);
    const payload = { email: values.get('email'), password: values.get('password'), region: values.get('region') };
    // Clear the password input immediately; never persist credentials in browser storage.
    (form.elements.namedItem('password') as HTMLInputElement).value = '';
    await run(async () => {
      const data = await api<State>('/account', { method: 'POST', body: JSON.stringify(payload) });
      setState(data); setVin(data.vehicles[0]?.vin ?? ''); setShowConnect(false);
      setNotice('Account connected. Waiting for vehicle updates.');
    });
  }

  async function sendCommand(event: React.FormEvent) {
    event.preventDefault();
    if (!selectedCommand || !car) return;
    const command = selectedCommand.id;
    const vehicle = car.vin;
    const commandPin = pin;
    const maxSoc = selectedCommand.maxSoc;
    setPin(''); setSelectedCommand(null);
    await run(async () => {
      const result = await api<{ message: string; requestId: string }>(`/vehicles/${encodeURIComponent(vehicle)}/commands`, { method: 'POST', body: JSON.stringify({ command, pin: commandPin, maxSoc }) });
      setNotice(`${result.message} Request ${result.requestId.slice(0, 8)}.`);
    });
  }

  return <div className="app">
    <header><a className="brand" href="/">MB <span>LOCAL</span></a><span className="tag">PROOF OF CONCEPT</span></header>
    <main>
      <div className="intro"><p className="eyebrow">YOUR GARAGE</p><h1>Your Mercedes.<br /><span>Your local server.</span></h1><p>A direct connection to your car, with controls in your browser.</p></div>
      {error ? <div className="message error" role="alert">{error}</div> : null}
      {notice ? <div className="message" role="status">{notice}</div> : null}
      <>
        <div className="toolbar"><span className={`connection ${state.connected ? 'online' : ''}`}><i />{state.connected ? 'Mercedes connection live' : state.configured ? 'Connecting to Mercedes…' : 'Account not connected'}</span><div className="toolbar-actions">{state.configured ? <button disabled={busy} onClick={() => setShowConnect(value => !value)}>{showConnect ? 'Close account form' : 'Reconnect account'}</button> : null}</div></div>
        {state.error ? <div className="message error" role="alert">{state.error}</div> : null}
        {!state.configured || showConnect ? <section className="panel connect"><div><p className="eyebrow">01 / CONNECT</p><h2>Connect your account</h2><p>Use the account linked to your vehicle in the Mercedes-Benz app.</p><p className="small">This unofficial integration uses Mercedes cloud services. Sign in to the official app first. Accounts requiring MFA are not supported by this login flow.</p><a className="text-link" href="https://github.com/ReneNulschDE/mbapi2020" target="_blank" rel="noreferrer">Based on mbapi2020 ↗</a></div><form onSubmit={connect}>
          <label>Mercedes email<input name="email" type="email" autoComplete="username" required disabled={busy} placeholder="you@example.com" /></label>
          <label>Mercedes password<input name="password" type="password" autoComplete="current-password" required disabled={busy} /></label>
          <label>Account region<select name="region" defaultValue={state.region} disabled={busy}><option>Europe</option><option>North America</option><option>Asia-Pacific</option></select></label>
          <button className="primary" disabled={busy}>{busy ? 'Connecting…' : 'Connect Mercedes'}</button><p className="small">Password is used for this login only. Encrypted refresh tokens stay on your server.</p>
        </form></section> : null}
        {car ? <>
          <section className="vehicle-heading"><div><p className="eyebrow">02 / VEHICLE</p><h2>{car.name}</h2><p className="mono">{car.plate} · {car.vin}</p></div><label>Vehicle<select value={vin} onChange={e => { setVin(e.target.value); setNotice(''); }} disabled={busy}>{state.vehicles.map(v => <option key={v.vin} value={v.vin}>{v.name} · {v.plate}</option>)}</select></label></section>
          <div className="metrics">{metricDefinitions.map(([label, key, unit]) => <div className="metric" key={key}><span>{label}</span><strong>{key === 'doors.doorlockstatusvehicle' ? ({ '0': 'Unlocked', '1': 'Internally locked', '2': 'Locked', '3': 'Partly unlocked' }[String(car.attributes[key]?.status === 'VALID' ? car.attributes[key]?.value : '')] ?? '—') : display(car.attributes[key], unit)}</strong></div>)}</div>
          <ScheduleControl key={`schedule-${car.vin}`} vin={car.vin} options={car.commands.find(command => command.id === 'charge_limit')?.chargeLimits ?? []} schedule={state.schedule} busy={busy} onSave={saveSchedule} onDisable={disableSchedule} />
          <ChargeLimitControl key={car.vin} car={car} connected={state.connected} busy={busy} scheduled={Boolean(state.schedule?.config.enabled && state.schedule.config.vin === car.vin)} onConfirm={setSelectedCommand} />
          <section className="panel"><div className="section-heading"><h2>Remote controls</h2><span className="small">Unavailable controls depend on your car and subscriptions.</span></div><div className="controls">{car.commands.filter(command => command.id !== 'charge_limit').map(command => <button key={command.id} className={command.id === 'unlock' ? 'unlock' : ''} disabled={!command.available || !state.connected || busy} title={!command.available ? 'Not reported as supported by Mercedes' : !state.connected ? 'Waiting for Mercedes connection' : command.label} onClick={() => { setPin(''); setSelectedCommand(command); }}>{command.label}{command.requiresPin ? <span className="small">Vehicle PIN required</span> : null}</button>)}</div>
            <div className="command-status" aria-live="polite"><span className="eyebrow">LAST REPORTED COMMAND</span><p>{car.lastCommand.type ? `${car.lastCommand.type} · ${car.lastCommand.state || 'Pending'}` : 'No command confirmation received yet.'}</p>{car.lastCommand.error ? <p className="error-text">{car.lastCommand.error}</p> : null}</div>
          </section>
          <details className="panel"><summary>Vehicle data <span className="small">{Object.keys(car.attributes).length} attributes</span></summary><label className="filter">Filter attributes<input type="search" value={filter} onChange={e => setFilter(e.target.value)} placeholder="Battery, tyre pressure, location…" /></label><div className="data-table"><table><thead><tr><th>Attribute</th><th>Reported value</th><th>Status</th></tr></thead><tbody>{Object.entries(car.attributes).filter(([key]) => key.toLowerCase().includes(filter.toLowerCase())).map(([key, attribute]) => <tr key={key}><th scope="row">{key}</th><td>{display(attribute)}</td><td>{attribute.status}</td></tr>)}</tbody></table></div>{!Object.keys(car.attributes).length ? <p>Waiting for your first vehicle update.</p> : null}</details>
          <div className="bottom-actions"><p className="small">Latest message: {car.updatedAt ? new Date(car.updatedAt).toLocaleString() : 'Waiting for vehicle telemetry'}. Vehicle data can be delayed.</p><button disabled={busy} onClick={() => { if (window.confirm('Disconnect this account and delete its saved tokens?')) void run(async () => { const data = await api<State>('/account', { method: 'DELETE' }); setState(data); setVin(''); setNotice('Account disconnected. Saved tokens deleted.'); }); }}>Disconnect account</button></div>
        </> : null}
      </>
    </main>
    <footer><span>MB Local / v0.1</span><span>Independent project · Not affiliated with Mercedes-Benz</span></footer>
    <dialog ref={dialog} onCancel={() => { setSelectedCommand(null); setPin(''); }} onClose={() => { setSelectedCommand(null); setPin(''); }}><form onSubmit={sendCommand}><p className="eyebrow">CONFIRM REMOTE ACTION</p><h2>{selectedCommand?.label}</h2><p>{selectedCommand?.id === 'charge_limit' ? <>Set the charge limit to <strong>{selectedCommand.maxSoc}%</strong> for <strong>{car?.plate}</strong>?</> : <>Send this command to <strong>{car?.plate}</strong>?</>}</p>{selectedCommand?.requiresPin ? <label>Vehicle security PIN<input value={pin} onChange={e => setPin(e.target.value.replace(/\D/g, '').slice(0, 4))} type="password" inputMode="numeric" pattern="[0-9]{4}" minLength={4} maxLength={4} required autoComplete="off" /></label> : null}<div className="dialog-actions"><button type="button" onClick={() => { setSelectedCommand(null); setPin(''); }}>Cancel</button><button className="primary" type="submit" disabled={busy}>Send command</button></div></form></dialog>
  </div>;
}

createRoot(document.getElementById('root')!).render(<App />);
