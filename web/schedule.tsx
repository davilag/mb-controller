import { useEffect, useState } from 'react';

export type ScheduleEntry = { time: string; maxSoc: number };
export type ScheduleConfig = { mode: 'oneNight'; enabled: boolean; vin: string; date: string; entries: ScheduleEntry[] };
export type Job = { at: string; target: number; status: string; message: string; sentAt: string | null; confirmedAt: string | null };
export type ChargeSchedule = { config: ScheduleConfig; timezone: string; status: string; message: string; jobs: Job[]; nextChangeAt: string | null };
const defaults = [{ time: '23:30', maxSoc: 80 }, { time: '05:30', maxSoc: 50 }];
function today(zone: string) { return new Intl.DateTimeFormat('en-CA', {timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit'}).format(new Date()); }
function localDate(value: string) { const [y, m, d] = value.split('-').map(Number); return new Date(y, m - 1, d, 12); }
const labels: Record<string, string> = { scheduled: 'Scheduled', running: 'Connecting', confirmed: 'Confirmed by car', already_set: 'Already at target', failed: 'Not confirmed', missed: 'Missed', interrupted: 'Interrupted', cancelled: 'Cancelled' };

export function ScheduleControl({ vin, options, schedule, busy, onSave, onDisable }: {
  vin: string; options: number[]; schedule?: ChargeSchedule; busy: boolean;
  onSave: (config: ScheduleConfig) => Promise<ChargeSchedule>; onDisable: () => Promise<ChargeSchedule>;
}) {
  const timezone = schedule?.timezone ?? 'Europe/London';
  const [date, setDate] = useState(today(timezone));
  const [entries, setEntries] = useState<ScheduleEntry[]>(defaults);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const active = Boolean(schedule?.config.enabled && schedule.config.vin === vin);
  const key = JSON.stringify(schedule?.config);
  useEffect(() => {
    if (!dirty && active && schedule) { setDate(schedule.config.date); setEntries(schedule.config.entries); }
  }, [key, dirty, vin]);
  function change(index: number, update: Partial<ScheduleEntry>) {
    setEntries(current => current.map((entry, i) => i === index ? { ...entry, ...update } : entry));
    setDirty(true); setSaved(false); setError('');
  }
  async function save(event: React.FormEvent) {
    event.preventDefault(); setSaving(true); setError(''); setSaved(false);
    try { await onSave({ mode: 'oneNight', vin, date, enabled: true, entries }); setDirty(false); setSaved(true); }
    catch (e) { setError((e as Error).message); } finally { setSaving(false); }
  }
  async function disable() {
    setSaving(true); setError('');
    try { await onDisable(); setDirty(false); setSaved(false); }
    catch (e) { setError((e as Error).message); } finally { setSaving(false); }
  }
  const endDay = localDate(date);
  if (entries[1].time < entries[0].time) endDay.setDate(endDay.getDate() + 1);
  const dayLabel = (d: Date) => Number.isNaN(d.getTime()) ? 'Choose a date' : new Intl.DateTimeFormat('en-GB', {weekday: 'short', day: 'numeric', month: 'short'}).format(d);
  return <section className="panel schedule-panel" aria-labelledby="schedule-title">
    <div className="section-heading"><div><p className="eyebrow">ONE NIGHT ONLY</p><h2 id="schedule-title">Plan your charge</h2></div><span className={`schedule-badge ${active ? 'enabled' : ''}`}>{active ? 'Night armed' : 'Not armed'}</span></div>
    <p>Two limit changes. One night. Nothing repeats.</p>
    <form onSubmit={save}>
      <fieldset disabled={busy || saving || schedule?.status === 'pending'}>
        <legend className="sr-only">One-night charging plan</legend>
        <label className="night-date">Night starting<input type="date" required min={today(timezone)} value={date} onChange={e => { setDate(e.target.value); setDirty(true); setSaved(false); }} /></label>
        <div className="night-steps">{entries.map((entry, i) => <div className="night-step" key={i}>
          <div className="step-heading"><span className="step-number">{i + 1}</span><div><h3>{i === 0 ? 'Evening limit' : 'Morning limit'}</h3><p className="small">{dayLabel(i === 0 ? localDate(date) : endDay)}</p></div></div>
          <div className="step-fields"><label>{i === 0 ? 'Evening time' : 'Morning time'}<input type="time" step="60" required value={entry.time} onChange={e => change(i, {time: e.target.value})} /></label><label>{i === 0 ? 'Evening percentage' : 'Morning percentage'}<select required value={entry.maxSoc} onChange={e => change(i, {maxSoc: Number(e.target.value)})}>{options.map(value => <option key={value} value={value}>{value}%</option>)}</select></label></div>
        </div>)}</div>
        <p className="small">Times are {timezone}, including daylight saving. A lower limit won’t discharge the battery.</p>
        <div className="schedule-actions"><button className="primary" type="submit" disabled={!options.length}>{saving ? 'Saving…' : active ? 'Replace this night' : 'Schedule this night'}</button>{active ? <button type="button" onClick={() => void disable()}>Cancel night</button> : null}</div>
        <p className="small" role="status">{saved ? 'Night saved. No connection is made until the scheduled time.' : dirty ? 'Unsaved changes' : 'Stays quiet before, between and after your two changes.'}</p>
      </fieldset>
      {error ? <p className="error-text" role="alert">{error}</p> : null}
    </form>
    {schedule?.jobs.length ? <div className="night-result" aria-live="polite"><h3>{active ? 'Scheduled night' : 'Last night plan'}</h3><p className={schedule.status === 'error' ? 'error-text' : 'small'}>{schedule.message}</p><ol className="job-list">{schedule.jobs.map((job, i) => <li key={job.at}><div className="job-summary"><strong>{new Intl.DateTimeFormat('en-GB', {timeZone: timezone, weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}).format(new Date(job.at))} · {job.target}%</strong><span className={`job-state ${job.status}`}>{labels[job.status] ?? job.status}</span></div><p className="small">{job.message}</p></li>)}</ol></div> : <p className="small schedule-note">{schedule?.message}</p>}
    <p className="small schedule-note">Keep this server running. Each change opens one short connection to send and confirm the limit, then closes it. No automatic retries. Unconfirmed changes need checking in the Mercedes app.</p>
  </section>;
}
