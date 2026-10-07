import { useState } from 'react';
import { api } from './client';
import { ProfilePanel } from './ProfilePanel';

export type Identity = { id: string; name: string };
export function SettingsPanel({ identities, current, onSwitch, onIdentities, onSaved }: {
  identities: Identity[]; current: string; onSwitch: (id: string) => void;
  onIdentities: (rows: Identity[]) => void; onSaved: () => void;
}) {
  const [theme, setTheme] = useState(localStorage.getItem('tiris-theme') || 'light');
  const [name, setName] = useState(identities.find(row => row.id === current)?.name || '');
  const [newName, setNewName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  async function write(create: boolean) {
    setBusy(true); setError(''); setNotice('');
    try {
      const row = await api<Identity>(create ? '/identities' : `/identities/${current}`, {
        method: create ? 'POST' : 'PATCH', body: JSON.stringify({ name: create ? newName : name }),
      });
      onIdentities(create ? [...identities, row] : identities.map(identity => identity.id === row.id ? row : identity));
      if (create) onSwitch(row.id); else setNotice('名称已保存');
    } catch (e) { setError(e instanceof Error ? e.message : '保存失败'); }
    finally { setBusy(false); }
  }
  return <div className="settings-view"><h1>设置</h1>
    <section className="settings-panel"><h2>外观</h2><div className="theme-options" role="group" aria-label="颜色主题">
      {[['light', '浅色'], ['dark', '暗色']].map(([value, label]) => <button key={value} aria-pressed={theme === value} onClick={() => { setTheme(value); localStorage.setItem('tiris-theme', value); document.documentElement.dataset.theme = value; }}>{label}</button>)}
    </div></section>
    <section className="settings-panel"><h2>用户身份</h2>
      {error && <div className="message error" role="alert">{error}</div>}{notice && <p role="status" className="saved-notice">{notice}</p>}
      <fieldset disabled={busy}><label>当前身份<select aria-label="当前身份" value={current} onChange={e => onSwitch(e.target.value)}>{identities.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}</select></label>
        <form className="inline-form" onSubmit={e => { e.preventDefault(); write(false); }}><label>身份名称<input required maxLength={80} value={name} onChange={e => setName(e.target.value)} /></label><button type="submit">保存名称</button></form>
        <form className="inline-form" onSubmit={e => { e.preventDefault(); write(true); }}><label>新身份<input required maxLength={80} value={newName} onChange={e => setNewName(e.target.value)} placeholder="例如：AI 学习" /></label><button type="submit">添加并切换</button></form>
      </fieldset>
    </section>
    <ProfilePanel onSaved={onSaved} />
  </div>;
}
