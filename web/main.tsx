import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';
import { ScheduleControl, type ChargeSchedule, type ScheduleConfig } from './schedule';

type Vehicle = { vin: string; name: string; plate: string; commands: {id: string; chargeLimits?: number[]}[] };
type State = {configured: boolean; connected: boolean; quietMode: boolean; accountBlocked: boolean; cooldownUntil: string | null; error: string | null; region: string; vehicles: Vehicle[]; schedule?: ChargeSchedule};
async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {...options, headers: {'Content-Type': 'application/json', ...options?.headers}});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed.');
  return data;
}
function App() {
  const [state, setState] = useState<State | null>(null);
  const [vin, setVin] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [showAccount, setShowAccount] = useState(false);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const data = await api<State>('/state');
        if (active) { setState(data); setVin(current => data.vehicles.some(v => v.vin === current) ? current : data.vehicles[0]?.vin ?? ''); setError(''); }
      } catch (e) { if (active) setError((e as Error).message); }
      if (active) timer = setTimeout(poll, 5000);
    }
    void poll(); return () => {active = false; clearTimeout(timer);};
  }, []);
  const car = state?.vehicles.find(v => v.vin === vin);
  async function saveSchedule(config: ScheduleConfig) {
    const schedule = await api<ChargeSchedule>('/schedule', {method: 'PUT', body: JSON.stringify(config)});
    setState(current => current ? {...current, schedule} : current); return schedule;
  }
  async function disableSchedule() {
    const schedule = await api<ChargeSchedule>('/schedule', {method: 'DELETE'});
    setState(current => current ? {...current, schedule} : current); return schedule;
  }
  async function connect(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault(); const form = e.currentTarget; const data = new FormData(form);
    const payload = {email: data.get('email'), password: data.get('password'), region: data.get('region')};
    (form.elements.namedItem('password') as HTMLInputElement).value = '';
    setBusy(true); setError('');
    try {setState(await api<State>('/account', {method: 'POST', body: JSON.stringify(payload)})); setShowAccount(false);}
    catch (e) {setError((e as Error).message);} finally {setBusy(false);}
  }
  return <div className="app">
    <header><a className="brand" href="/">MB <span>LOCAL</span></a><span className="tag">NIGHT CHARGING</span></header>
    <main>
      <div className="intro"><p className="eyebrow">READY FOR TONIGHT</p><h1>Your overnight charge.</h1><p>Two changes for one night. Quiet the rest of the time.</p></div>
      {error ? <div className="message error" role="alert">{error}</div> : null}
      {state ? <div className="toolbar"><span className={`connection ${state.connected ? 'online' : ''}`}><i/>{state.connected ? 'Applying a scheduled change' : 'Quiet · no background connections'}</span><button onClick={() => setShowAccount(v => !v)}>{showAccount ? 'Close account' : 'Account'}</button></div> : <p role="status">Loading your night plan…</p>}
      {state?.accountBlocked ? <div className="message error" role="alert">Mercedes rate limited the last connection. Connections are paused{state.cooldownUntil ? ` until at least ${new Date(state.cooldownUntil).toLocaleString('en-GB')}` : ''}. No retries are being made. Recovery is not guaranteed at that time.</div> : null}
      {state?.error ? <div className="message error" role="alert">{state.error}</div> : null}
      {state && (!state.configured || showAccount) ? <section className="panel connect"><div><p className="eyebrow">ACCOUNT SETUP</p><h2>Your Mercedes account</h2><p>Use the invited secondary account for your car.</p><p className="small">Setup contacts Mercedes once to save your vehicle details. Opening this page or saving a night does not contact Mercedes.</p></div><form onSubmit={connect}><label>Mercedes email<input name="email" type="email" autoComplete="username" required disabled={busy}/></label><label>Mercedes password<input name="password" type="password" autoComplete="current-password" required disabled={busy}/></label><label>Region<select name="region" defaultValue={state.region} disabled={busy}><option>Europe</option><option>North America</option><option>Asia-Pacific</option></select></label><button className="primary" disabled={busy || state.accountBlocked}>{busy ? 'Setting up…' : 'Log in to Mercedes'}</button><p className="small">Password is used once, not saved. Tokens are encrypted on this server.</p></form></section> : null}
      {car ? <><div className="vehicle-heading"><div><p className="eyebrow">YOUR VEHICLE</p><h2>{car.name}</h2><p className="small">{car.plate} · Saved details, not live telemetry</p></div>{(state?.vehicles.length ?? 0) > 1 ? <label>Vehicle<select value={vin} onChange={e => setVin(e.target.value)}>{state?.vehicles.map(v => <option key={v.vin} value={v.vin}>{v.name} · {v.plate}</option>)}</select></label> : null}</div><ScheduleControl key={car.vin} vin={vin} options={car.commands.find(c => c.id === 'charge_limit')?.chargeLimits ?? []} schedule={state?.schedule} busy={busy} onSave={saveSchedule} onDisable={disableSchedule}/></> : null}
    </main><footer><span>MB Local</span><a href="https://github.com/ReneNulschDE/mbapi2020" target="_blank" rel="noreferrer">Based on mbapi2020 ↗</a></footer>
  </div>;
}
createRoot(document.getElementById('root')!).render(<App/>);
