import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { api, ApiError } from './client';

type Data = { background: string; foundations: string[]; long_term_directions: string[];
  learning_goals: string[]; blocked_topics: string[]; language_preferences: string[];
  daily_reading_minutes: number | null };
type Profile = { version: number; data: Data };
type Form = { background: string; foundations: string; long_term_directions: string;
  learning_goals: string; blocked_topics: string };
const labels = { foundations: '已有基础', long_term_directions: '关注方向', learning_goals: '学习目标', blocked_topics: '不感兴趣的主题' };
const formFor = (data: Data): Form => ({ background: data.background,
  foundations: data.foundations.join('\n'), long_term_directions: data.long_term_directions.join('\n'),
  learning_goals: data.learning_goals.join('\n'), blocked_topics: data.blocked_topics.join('\n') });
const list = (text: string) => text.split(/[,，\n]/).map(value => value.trim()).filter(Boolean);

export function ProfilePanel({ onSaved }: { onSaved: () => void }) {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [conflict, setConflict] = useState(false);
  const fail = (e: unknown) => { setError(e instanceof Error ? e.message : '读取失败'); if (e instanceof ApiError && e.status === 409) setConflict(true); };
  async function load() {
    setBusy(true);
    try { const data = await api<Profile>('/profile'); setProfile(data); setForm(formFor(data.data)); setConflict(false); setError(''); }
    catch (e) { fail(e); } finally { setBusy(false); }
  }
  useEffect(() => { load(); }, []);
  async function save(event: FormEvent) {
    event.preventDefault(); if (!form || !profile) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const data = { ...profile.data, ...form, foundations: list(form.foundations),
        long_term_directions: list(form.long_term_directions), learning_goals: list(form.learning_goals), blocked_topics: list(form.blocked_topics) };
      const saved = await api<Profile>('/profile', { method: 'PATCH', body: JSON.stringify({ ...data, expected_version: profile.version }) });
      setProfile(saved); setForm(formFor(saved.data)); setConflict(false); setNotice('已保存'); onSaved();
    } catch (e) { fail(e); } finally { setBusy(false); }
  }
  return <section className="settings-panel"><h2>长期用户画像</h2>
    {error && <div className="message error" role="alert">{error}</div>}
    {notice && <p role="status" className="saved-notice">{notice}</p>}
    {conflict && <p className="scope">草稿已保留。请复制需要的内容，再重新载入。</p>}
    {form ? <form onSubmit={save}><fieldset disabled={busy}>
      <label>专业与背景<textarea aria-label="专业与背景" maxLength={2000} value={form.background} onChange={e => setForm({ ...form, background: e.target.value })} /></label>
      {Object.entries(labels).map(([key, label]) => <label key={key}>{label}<textarea aria-label={label} value={form[key as keyof Form]} maxLength={2100} onChange={e => setForm({ ...form, [key]: e.target.value })} placeholder="每行一项" /></label>)}
      <div className="source-actions"><button type="submit">保存画像</button><button className="subtle" type="button" onClick={load}>重新载入</button></div>
    </fieldset></form> : <p className="muted">{busy ? '加载中…' : <button onClick={load}>重试</button>}</p>}
  </section>;
}
