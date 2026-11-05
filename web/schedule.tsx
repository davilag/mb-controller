import { useEffect, useState } from 'react';

export type ScheduleEntry = { time: string; maxSoc: number };
export type ScheduleConfig = { enabled: boolean; vin: string; entries: ScheduleEntry[] };
export type ChargeSchedule = {
  config: ScheduleConfig; timezone: string; status: string; message: string;
  target: number; nextChangeAt: string; nextTarget: number; lastSentAt: string | null; attempts: number;
};

const defaults: ScheduleEntry[] = [{ time: '05:30', maxSoc: 50 }, { time: '23:30', maxSoc: 80 }];

export function ScheduleControl({ vin, options, schedule, busy, onSave, onDisable }: {
  vin: string; options: number[]; schedule?: ChargeSchedule; busy: boolean;
  onSave: (config: ScheduleConfig) => Promise<ChargeSchedule>;
  onDisable: () => Promise<ChargeSchedule>;
}) {
  const initial = schedule?.config;
  const [entries, setEntries] = useState<ScheduleEntry[]>(initial?.entries ?? defaults);
  const [enabled, setEnabled] = useState(Boolean(initial?.enabled && initial.vin === vin));
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const configKey = JSON.stringify(schedule?.config);
  const active = Boolean(schedule?.config.enabled && schedule.config.vin === vin);
  const timezone = schedule?.timezone ?? 'Europe/London';
  const nextTime = schedule ? new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: timezone }).format(new Date(schedule.nextChangeAt)) : '';

  useEffect(() => {
    if (!dirty && schedule) {
      setEntries(schedule.config.entries.map(entry => ({ ...entry })));
      setEnabled(schedule.config.enabled && schedule.config.vin === vin);
    }
  }, [configKey, dirty, vin]);

  function change(index: number, update: Partial<ScheduleEntry>) {
    setEntries(current => current.map((entry, i) => i === index ? { ...entry, ...update } : entry));
    setDirty(true); setSaved(false); setError('');
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true); setSaved(false); setError('');
    try {
      const result = await onSave({ vin, enabled, entries });
      setEntries(result.config.entries); setEnabled(result.config.enabled); setDirty(false); setSaved(true);
    } catch (error) { setError((error as Error).message); }
    finally { setSaving(false); }
  }

  async function disable() {
    setSaving(true); setError('');
    try {
      await onDisable(); setEnabled(false); setDirty(false); setSaved(true);
    } catch (error) { setError((error as Error).message); }
    finally { setSaving(false); }
  }

  function addPeriod() {
    const used = new Set(entries.map(entry => entry.time));
    const time = Array.from({ length: 24 }, (_, hour) => `${String(hour).padStart(2, '0')}:00`).find(time => !used.has(time)) ?? '12:30';
    setEntries(current => [...current, { time, maxSoc: options[0] ?? 50 }]);
    setDirty(true); setSaved(false);
  }

  return <section className="panel schedule-panel" aria-labelledby="schedule-title">
    <div className="section-heading"><div><p className="eyebrow">DAILY AUTOMATION</p><h2 id="schedule-title">Charge-limit schedule</h2></div><span className={`schedule-badge ${active ? 'enabled' : ''}`}>{active ? 'Schedule on' : 'Schedule off'}</span></div>
    <p>Set a limit for each part of the day. Each period lasts until the next start time, including overnight.</p>
    <p className="small">All times use {timezone}, including daylight saving. Runs on your server with the browser closed.</p>
    {active && schedule ? <div className={`schedule-status ${schedule.status === 'error' ? 'schedule-error' : ''}`} role="status"><strong>Current target: {schedule.target}%</strong><span>{schedule.message}</span><span>Next: {schedule.nextTarget}% at {nextTime}.</span></div> : null}
    {schedule?.config.enabled && schedule.config.vin !== vin ? <p className="small">A schedule is active for another vehicle. Saving an enabled schedule here will replace it.</p> : null}
    <form onSubmit={save}>
      <fieldset disabled={busy || saving}>
        <legend className="sr-only">Daily charging periods</legend>
        <div className="schedule-rows">{entries.map((entry, index) => <div className="schedule-row" key={index}>
          <label>Period {index + 1} starts<input type="time" step="60" required value={entry.time} onChange={event => change(index, { time: event.target.value })} /></label>
          <label>Charge limit<select value={entry.maxSoc} onChange={event => change(index, { maxSoc: Number(event.target.value) })} required disabled={!options.length}>{options.map(value => <option value={value} key={value}>{value}%</option>)}</select></label>
          <button type="button" className="remove-period" aria-label={`Remove period ${index + 1}`} disabled={entries.length === 1} onClick={() => { setEntries(current => current.filter((_, i) => i !== index)); setDirty(true); setSaved(false); }}>Remove</button>
        </div>)}</div>
        <button type="button" className="add-period" disabled={entries.length >= 12} onClick={addPeriod}>+ Add period</button>
        <label className="schedule-toggle"><input type="checkbox" checked={enabled} disabled={!options.length} onChange={event => { setEnabled(event.target.checked); setDirty(true); setSaved(false); }} /><span>Enable daily schedule</span></label>
        <p className="small">Saving an enabled schedule applies the current period immediately and maintains its limit. A lower limit won’t discharge the battery.</p>
        <div className="schedule-actions"><button className="primary" type="submit" disabled={!options.length}>{saving ? 'Saving…' : enabled ? 'Save and enable schedule' : 'Save schedule'}</button>{active ? <button type="button" onClick={() => void disable()}>Turn schedule off</button> : null}{dirty ? <span className="small">Unsaved changes</span> : saved ? <span className="small" role="status">Schedule saved.</span> : null}</div>
      </fieldset>
      {error ? <p className="error-text" role="alert">{error}</p> : null}
    </form>
    <p className="small schedule-note">Keep the backend running. An idle Mercedes connection is normal; a due command reconnects on demand. Checks run every 20 seconds with up to three delayed attempts. Saved or sent does not mean confirmed by the car.</p>
  </section>;
}
