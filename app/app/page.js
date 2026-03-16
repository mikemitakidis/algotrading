'use client'
import { useState, useEffect, useCallback } from 'react'

// ─── Styles ───────────────────────────────────────────────────────────────────
const S = {
  body:    { background: '#0d1117', color: '#e6edf3', fontFamily: "'Inter', 'Segoe UI', sans-serif", minHeight: '100vh', margin: 0 },
  nav:     { background: '#161b22', borderBottom: '1px solid #30363d', padding: '0 24px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', height: 56 },
  brand:   { fontSize: 18, fontWeight: 700, color: '#58a6ff', display: 'flex', alignItems: 'center', gap: 10 },
  badge:   { background: '#1f6feb', color: '#fff', fontSize: 11, padding: '2px 8px', borderRadius: 12, fontWeight: 600 },
  navLink: (active) => ({ color: active ? '#e6edf3' : '#8b949e', background: active ? '#21262d' : 'none', border: 'none', padding: '7px 14px', borderRadius: 6, fontSize: 14, cursor: 'pointer', fontFamily: 'inherit' }),
  page:    { padding: 24, maxWidth: 1400, margin: '0 auto' },
  grid2:   { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20, marginBottom: 20 },
  grid3:   { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 20, marginBottom: 20 },
  card:    { background: '#161b22', border: '1px solid #30363d', borderRadius: 12, padding: 24 },
  cardTitle: { fontSize: 11, fontWeight: 600, color: '#8b949e', letterSpacing: 1, textTransform: 'uppercase', marginBottom: 16 },
  metricVal: (color) => ({ fontSize: 36, fontWeight: 700, color: color || '#58a6ff', lineHeight: 1 }),
  metricLbl: { fontSize: 11, color: '#8b949e', textTransform: 'uppercase', letterSpacing: 1, marginTop: 4 },
  btn: (color) => ({ background: color, color: '#fff', border: 'none', padding: '9px 18px', borderRadius: 7, fontSize: 14, fontWeight: 600, cursor: 'pointer', fontFamily: 'inherit' }),
  table:   { width: '100%', borderCollapse: 'collapse', fontSize: 13 },
  th:      { background: '#21262d', color: '#8b949e', padding: '10px 12px', textAlign: 'left', fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.8 },
  td:      { padding: '10px 12px', borderTop: '1px solid #21262d' },
  logbox:  { background: '#0d1117', border: '1px solid #30363d', borderRadius: 8, padding: 16, fontFamily: "'Courier New', monospace", fontSize: 12, height: 400, overflowY: 'auto', lineHeight: 1.6 },
  input:   { background: '#0d1117', border: '1px solid #30363d', color: '#e6edf3', padding: '8px 12px', borderRadius: 7, fontSize: 14, fontFamily: 'inherit', width: '100%', boxSizing: 'border-box' },
  textarea: { background: '#0d1117', border: '1px solid #30363d', color: '#e6edf3', padding: 16, borderRadius: 8, fontFamily: "'Courier New', monospace", fontSize: 13, width: '100%', height: 480, resize: 'vertical', boxSizing: 'border-box' },
  tag: (color, bg) => ({ color, background: bg, padding: '3px 10px', borderRadius: 12, fontSize: 11, fontWeight: 600, whiteSpace: 'nowrap' }),
  alert: (type) => ({ padding: '12px 16px', borderRadius: 8, marginBottom: 16, fontSize: 14, background: type === 'success' ? '#0d4a1a' : '#4a0d0d', border: `1px solid ${type === 'success' ? '#238636' : '#da3633'}`, color: type === 'success' ? '#3fb950' : '#f85149' }),
}

// ─── Login Page ───────────────────────────────────────────────────────────────
function LoginPage({ onLogin }) {
  const [pw, setPw] = useState('')
  const [err, setErr] = useState('')
  const submit = async () => {
    const r = await fetch('/api/auth', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: pw }) })
    if (r.ok) onLogin()
    else setErr('Incorrect password')
  }
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: '100vh', background: '#0d1117' }}>
      <div style={{ background: '#161b22', border: '1px solid #30363d', borderRadius: 14, padding: 40, width: 360 }}>
        <div style={{ fontSize: 24, fontWeight: 700, color: '#58a6ff', textAlign: 'center', marginBottom: 8 }}>🤖 Algo Trader</div>
        <div style={{ color: '#8b949e', textAlign: 'center', marginBottom: 28 }}>v2.0 — Shadow Mode</div>
        <input style={S.input} type="password" placeholder="Password" value={pw}
          onChange={e => setPw(e.target.value)} onKeyDown={e => e.key === 'Enter' && submit()} />
        <button style={{ ...S.btn('#238636'), width: '100%', marginTop: 12, padding: 12, fontSize: 15 }} onClick={submit}>Login</button>
        {err && <div style={{ color: '#f85149', textAlign: 'center', marginTop: 10, fontSize: 13 }}>{err}</div>}
      </div>
    </div>
  )
}

// ─── API helpers ──────────────────────────────────────────────────────────────
const api = {
  get: (path, params = '') => fetch(`/api/proxy/${path}${params ? '?' + params : ''}`, { cache: 'no-store' }).then(r => r.json()).catch(() => ({})),
  post: (path, body) => fetch(`/api/proxy/${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(r => r.json()).catch(() => ({})),
}

// ─── Overview Tab ─────────────────────────────────────────────────────────────
function OverviewTab() {
  const [status, setStatus] = useState({})
  const [signals, setSignals] = useState([])
  const [logs, setLogs] = useState([])
  const [serverError, setServerError] = useState(false)

  const load = useCallback(async () => {
    const s = await api.get('status')
    if (s.error) setServerError(true)
    else { setServerError(false); setStatus(s) }
    const sg = await api.get('signals', 'limit=8')
    setSignals(sg.signals || [])
    const lg = await api.get('logs', 'lines=60')
    setLogs(lg.lines || [])
  }, [])

  useEffect(() => { load(); const t = setInterval(load, 20000); return () => clearInterval(t) }, [load])

  const action = async (a) => { await api.post(a, {}); setTimeout(load, 2500) }

  const logColor = (l) => {
    if (l.includes('SIGNAL') || l.includes('***')) return '#3fb950'
    if (l.includes('ERROR')) return '#f85149'
    if (l.includes('WARNING')) return '#d29922'
    if (l.includes('Connectivity OK') || l.includes('Tier A done') || l.includes('Focus:')) return '#58a6ff'
    return '#8b949e'
  }

  return (
    <div style={S.page}>
      {serverError && <div style={S.alert('error')}>⚠️ Cannot reach trading server at Hetzner (138.199.196.95:8080). Bot may be restarting.</div>}
      <div style={S.grid2}>
        <div style={S.card}>
          <div style={S.cardTitle}>Bot Status</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20 }}>
            <div style={{ width: 14, height: 14, borderRadius: '50%', background: status.running ? '#3fb950' : '#f85149', boxShadow: status.running ? '0 0 10px #3fb950' : 'none' }} />
            <div style={{ fontSize: 22, fontWeight: 600 }}>{status.running ? 'Running — SHADOW mode' : 'Stopped'}</div>
          </div>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            <button style={S.btn('#238636')} onClick={() => action('start')}>▶ Start</button>
            <button style={S.btn('#da3633')} onClick={() => action('stop')}>⏹ Stop</button>
            <button style={S.btn('#1f6feb')} onClick={() => action('restart')}>↺ Restart</button>
          </div>
        </div>
        <div style={S.card}>
          <div style={S.cardTitle}>Performance</div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 1, background: '#30363d', borderRadius: 8, overflow: 'hidden' }}>
            {[['Signals', status.signal_count || 0, '#58a6ff'], ['IBKR', status.ibkr_count || 0, '#3fb950'], ['eToro', status.etoro_count || 0, '#d29922']].map(([lbl, val, color]) => (
              <div key={lbl} style={{ background: '#161b22', padding: '18px 12px', textAlign: 'center' }}>
                <div style={S.metricVal(color)}>{val}</div>
                <div style={S.metricLbl}>{lbl}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
      <div style={{ ...S.card, marginBottom: 20 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <div style={S.cardTitle}>Recent Signals</div>
          <button style={{ ...S.btn('#21262d'), fontSize: 12, padding: '5px 12px' }} onClick={load}>↻ Refresh</button>
        </div>
        {signals.length === 0 ? (
          <div style={{ color: '#8b949e', textAlign: 'center', padding: 30 }}>No signals yet — bot is scanning in shadow mode...</div>
        ) : (
          <table style={S.table}>
            <thead><tr>{['Time','Symbol','Direction','Route','TFs','RSI','Price','ATR'].map(h => <th key={h} style={S.th}>{h}</th>)}</tr></thead>
            <tbody>{signals.map((s, i) => (
              <tr key={i}>
                <td style={S.td}>{s.timestamp?.slice(0,19)}</td>
                <td style={{ ...S.td, fontWeight: 700 }}>{s.symbol}</td>
                <td style={S.td}><span style={s.direction === 'long' ? S.tag('#3fb950','#0d4a1a') : S.tag('#f85149','#4a0d0d')}>{s.direction?.toUpperCase()}</span></td>
                <td style={S.td}><span style={s.route === 'ETORO' ? S.tag('#d29922','#2d1f00') : S.tag('#58a6ff','#0d2d5a')}>{s.route}</span></td>
                <td style={S.td}>{s.valid_count}/4</td>
                <td style={S.td}>{(s.rsi||0).toFixed(1)}</td>
                <td style={S.td}>${(s.price||0).toFixed(2)}</td>
                <td style={S.td}>{(s.atr||0).toFixed(2)}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </div>
      <div style={S.card}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <div style={S.cardTitle}>Live Log Feed</div>
          <button style={{ ...S.btn('#21262d'), fontSize: 12, padding: '5px 12px' }} onClick={load}>↻ Refresh</button>
        </div>
        <div style={S.logbox} ref={el => el && (el.scrollTop = el.scrollHeight)}>
          {logs.map((l, i) => <div key={i} style={{ color: logColor(l), marginBottom: 1 }}>{l}</div>)}
        </div>
      </div>
    </div>
  )
}

// ─── Signals Tab ─────────────────────────────────────────────────────────────
function SignalsTab() {
  const [signals, setSignals] = useState([])
  const [filter, setFilter] = useState({ route: 'all', direction: 'all', search: '' })
  const [sort, setSort] = useState({ col: 'id', dir: -1 })

  useEffect(() => {
    api.get('signals', 'limit=500').then(d => setSignals(d.signals || []))
  }, [])

  const filtered = signals
    .filter(s => filter.route === 'all' || s.route === filter.route)
    .filter(s => filter.direction === 'all' || s.direction === filter.direction)
    .filter(s => !filter.search || s.symbol?.toLowerCase().includes(filter.search.toLowerCase()))
    .sort((a, b) => (a[sort.col] > b[sort.col] ? 1 : -1) * sort.dir)

  const cols = [
    { key: 'timestamp', label: 'Time' }, { key: 'symbol', label: 'Symbol' },
    { key: 'direction', label: 'Dir' }, { key: 'route', label: 'Route' },
    { key: 'valid_count', label: 'TFs' }, { key: 'rsi', label: 'RSI' },
    { key: 'macd_hist', label: 'MACD' }, { key: 'ema20', label: 'EMA20' },
    { key: 'ema50', label: 'EMA50' }, { key: 'bb_pos', label: 'BB Pos' },
    { key: 'vwap_dev', label: 'VWAP Dev' }, { key: 'vol_ratio', label: 'Vol Ratio' },
    { key: 'atr', label: 'ATR' }, { key: 'price', label: 'Price' },
  ]

  return (
    <div style={S.page}>
      <div style={{ ...S.card, marginBottom: 20 }}>
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
          <input style={{ ...S.input, width: 200 }} placeholder="Search symbol..." value={filter.search} onChange={e => setFilter(f => ({ ...f, search: e.target.value }))} />
          {['all','ETORO','IBKR'].map(r => (
            <button key={r} style={{ ...S.btn(filter.route === r ? '#1f6feb' : '#21262d'), fontSize: 13 }} onClick={() => setFilter(f => ({ ...f, route: r }))}>{r === 'all' ? 'All Routes' : r}</button>
          ))}
          {['all','long','short'].map(d => (
            <button key={d} style={{ ...S.btn(filter.direction === d ? '#1f6feb' : '#21262d'), fontSize: 13 }} onClick={() => setFilter(f => ({ ...f, direction: d }))}>{d === 'all' ? 'All Directions' : d.charAt(0).toUpperCase()+d.slice(1)}</button>
          ))}
          <span style={{ color: '#8b949e', fontSize: 13, marginLeft: 'auto' }}>{filtered.length} signals</span>
          <button style={{ ...S.btn('#21262d'), fontSize: 12 }} onClick={() => api.get('signals','limit=500').then(d => setSignals(d.signals||[]))}>↻</button>
        </div>
      </div>
      <div style={{ ...S.card, overflowX: 'auto' }}>
        <table style={S.table}>
          <thead><tr>{cols.map(c => (
            <th key={c.key} style={{ ...S.th, cursor: 'pointer', userSelect: 'none' }} onClick={() => setSort(s => ({ col: c.key, dir: s.col === c.key ? -s.dir : -1 }))}>
              {c.label} {sort.col === c.key ? (sort.dir === -1 ? '↓' : '↑') : ''}
            </th>
          ))}</tr></thead>
          <tbody>{filtered.map((s, i) => (
            <tr key={i} style={{ background: i%2===0?'transparent':'#0d1117' }}>
              <td style={S.td}>{s.timestamp?.slice(0,19)}</td>
              <td style={{ ...S.td, fontWeight: 700, color: '#58a6ff' }}>{s.symbol}</td>
              <td style={S.td}><span style={s.direction === 'long' ? S.tag('#3fb950','#0d4a1a') : S.tag('#f85149','#4a0d0d')}>{s.direction?.toUpperCase()}</span></td>
              <td style={S.td}><span style={s.route === 'ETORO' ? S.tag('#d29922','#2d1f00') : S.tag('#58a6ff','#0d2d5a')}>{s.route}</span></td>
              <td style={{ ...S.td, textAlign: 'center' }}>{s.valid_count}/4</td>
              <td style={{ ...S.td, color: s.rsi > 70 ? '#f85149' : s.rsi < 30 ? '#3fb950' : '#e6edf3' }}>{(s.rsi||0).toFixed(1)}</td>
              <td style={{ ...S.td, color: s.macd_hist > 0 ? '#3fb950' : '#f85149' }}>{(s.macd_hist||0).toFixed(4)}</td>
              <td style={S.td}>{(s.ema20||0).toFixed(2)}</td>
              <td style={S.td}>{(s.ema50||0).toFixed(2)}</td>
              <td style={S.td}>{(s.bb_pos||0).toFixed(3)}</td>
              <td style={{ ...S.td, color: s.vwap_dev > 0 ? '#3fb950' : '#f85149' }}>{((s.vwap_dev||0)*100).toFixed(2)}%</td>
              <td style={S.td}>{(s.vol_ratio||0).toFixed(2)}x</td>
              <td style={S.td}>{(s.atr||0).toFixed(2)}</td>
              <td style={{ ...S.td, fontWeight: 600 }}>${(s.price||0).toFixed(2)}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  )
}

// ─── Parameters Tab ───────────────────────────────────────────────────────────
function ParametersTab() {
  const [cfg, setCfg] = useState(null)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => { api.get('settings').then(d => d.content && parseYaml(d.content)) }, [])

  const parseYaml = (content) => {
    // Parse YAML manually for display
    const lines = content.split('\n')
    const result = {}
    let section = ''
    lines.forEach(line => {
      if (line.match(/^\w+:$/)) section = line.replace(':', '')
      else if (line.match(/^\s+\w+:/) && section) {
        const [k, v] = line.trim().split(': ')
        if (!result[section]) result[section] = {}
        result[section][k] = v?.replace(/['"]/g, '') || ''
      }
    })
    setCfg(result)
  }

  const update = (section, key, value) => {
    setCfg(prev => ({ ...prev, [section]: { ...prev[section], [key]: value } }))
  }

  const save = async () => {
    const yaml = Object.entries(cfg).map(([section, vals]) => {
      const lines = [`${section}:`]
      Object.entries(vals).forEach(([k, v]) => lines.push(`  ${k}: "${v}"`))
      return lines.join('\n')
    }).join('\n\n')

    const r = await api.post('settings', { content: yaml })
    if (r.ok) { setSaved(true); setTimeout(() => setSaved(false), 3000) }
    else setError(r.error || 'Save failed')
  }

  if (!cfg) return <div style={{ ...S.page, color: '#8b949e' }}>Loading parameters...</div>

  const paramDefs = {
    bot: {
      mode:                { label: 'Bot Mode', type: 'select', options: ['shadow','live'], desc: 'shadow = no real trades, live = execute orders' },
      cycle_minutes:       { label: 'Scan Cycle (minutes)', type: 'number', desc: 'How often to scan all symbols' },
      focus_size:          { label: 'Focus Set Size', type: 'number', desc: 'Top N symbols to analyse each cycle' },
      rank_interval_hours: { label: 'Re-rank Every (hours)', type: 'number', desc: 'How often to refresh the focus set' },
    },
    alpaca: {
      api_key:    { label: 'Alpaca API Key', type: 'text', desc: 'Paper trading key (for future order execution)' },
      secret_key: { label: 'Alpaca Secret Key', type: 'password', desc: 'Keep private — used for order execution only' },
      feed:       { label: 'Data Feed', type: 'select', options: ['iex','sip'], desc: 'iex = free tier, sip = paid only' },
    },
    signal_routing: {
      etoro_min_timeframes: { label: 'eToro Min Timeframes', type: 'number', desc: 'Require this many TFs for eToro Telegram alert' },
      ibkr_min_timeframes:  { label: 'IBKR Min Timeframes', type: 'number', desc: 'Require this many TFs for IBKR execution' },
    },
    scoring: {
      rsi_long_min:        { label: 'RSI Long Min', type: 'number', desc: 'Minimum RSI for long signal' },
      rsi_long_max:        { label: 'RSI Long Max', type: 'number', desc: 'Maximum RSI for long signal' },
      rsi_short_min:       { label: 'RSI Short Min', type: 'number', desc: 'Minimum RSI for short signal' },
      vol_ratio_min:       { label: 'Volume Ratio Min', type: 'number', desc: 'Minimum volume vs 20-bar avg' },
      vwap_dev_threshold:  { label: 'VWAP Dev Threshold', type: 'number', desc: 'Max VWAP deviation allowed' },
      ema_tolerance:       { label: 'EMA Tolerance', type: 'number', desc: 'EMA20/50 crossover tolerance' },
    },
    telegram: {
      token:   { label: 'Telegram Bot Token', type: 'password', desc: 'From @BotFather — for signal alerts' },
      chat_id: { label: 'Telegram Chat ID', type: 'text', desc: 'Your personal chat ID' },
    },
    dashboard: {
      port:     { label: 'Dashboard Port', type: 'number', desc: 'Hetzner server port' },
      password: { label: 'Dashboard Password', type: 'password', desc: 'Password to access this dashboard' },
    },
  }

  const sectionNames = { bot: '🤖 Bot', alpaca: '📈 Alpaca', signal_routing: '🔀 Signal Routing', scoring: '📊 Scoring', telegram: '📱 Telegram', dashboard: '🖥️ Dashboard' }

  return (
    <div style={S.page}>
      {saved && <div style={S.alert('success')}>✅ Settings saved and bot restarting...</div>}
      {error && <div style={S.alert('error')}>❌ {error}</div>}
      {Object.entries(paramDefs).map(([section, params]) => (
        <div key={section} style={{ ...S.card, marginBottom: 20 }}>
          <div style={S.cardTitle}>{sectionNames[section] || section}</div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            {Object.entries(params).map(([key, def]) => (
              <div key={key}>
                <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4, color: '#e6edf3' }}>{def.label}</div>
                <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 6 }}>{def.desc}</div>
                {def.type === 'select' ? (
                  <select style={{ ...S.input, cursor: 'pointer' }} value={cfg[section]?.[key] || ''} onChange={e => update(section, key, e.target.value)}>
                    {def.options.map(o => <option key={o} value={o}>{o}</option>)}
                  </select>
                ) : (
                  <input style={S.input} type={def.type === 'password' ? 'password' : def.type === 'number' ? 'number' : 'text'}
                    value={cfg[section]?.[key] || ''}
                    onChange={e => update(section, key, e.target.value)} />
                )}
              </div>
            ))}
          </div>
        </div>
      ))}
      <button style={{ ...S.btn('#238636'), padding: '12px 32px', fontSize: 15 }} onClick={save}>💾 Save All Parameters & Restart Bot</button>
    </div>
  )
}

// ─── Logs Tab ─────────────────────────────────────────────────────────────────
function LogsTab() {
  const [lines, setLines] = useState([])
  const [filter, setFilter] = useState('all')
  const [autoRefresh, setAutoRefresh] = useState(true)

  const load = useCallback(() => {
    api.get('logs', 'lines=500').then(d => setLines(d.lines || []))
  }, [])

  useEffect(() => { load(); if (autoRefresh) { const t = setInterval(load, 10000); return () => clearInterval(t) } }, [load, autoRefresh])

  const filtered = lines.filter(l => {
    if (filter === 'all') return true
    if (filter === 'signals') return l.includes('SIGNAL') || l.includes('***')
    if (filter === 'errors') return l.includes('ERROR') || l.includes('WARNING')
    if (filter === 'info') return l.includes('INFO') && !l.includes('Batch')
    return true
  })

  const logColor = (l) => {
    if (l.includes('SIGNAL') || l.includes('***')) return '#3fb950'
    if (l.includes('ERROR')) return '#f85149'
    if (l.includes('WARNING')) return '#d29922'
    if (l.includes('Connectivity OK') || l.includes('Focus:') || l.includes('Tier A done')) return '#58a6ff'
    if (l.includes('INFO')) return '#8b949e'
    return '#6e7681'
  }

  return (
    <div style={S.page}>
      <div style={{ ...S.card, marginBottom: 16 }}>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          {['all','signals','errors','info'].map(f => (
            <button key={f} style={{ ...S.btn(filter === f ? '#1f6feb' : '#21262d'), fontSize: 13 }} onClick={() => setFilter(f)}>
              {f.charAt(0).toUpperCase()+f.slice(1)}
            </button>
          ))}
          <button style={{ ...S.btn(autoRefresh ? '#238636' : '#21262d'), fontSize: 13 }} onClick={() => setAutoRefresh(a => !a)}>
            {autoRefresh ? '⏸ Auto Refresh ON' : '▶ Auto Refresh OFF'}
          </button>
          <button style={{ ...S.btn('#21262d'), fontSize: 13 }} onClick={load}>↻ Refresh Now</button>
          <span style={{ color: '#8b949e', fontSize: 12, marginLeft: 'auto' }}>{filtered.length} lines</span>
        </div>
      </div>
      <div style={S.card}>
        <div style={{ ...S.logbox, height: 600 }} ref={el => el && (el.scrollTop = el.scrollHeight)}>
          {filtered.map((l, i) => <div key={i} style={{ color: logColor(l), marginBottom: 1, wordBreak: 'break-all' }}>{l}</div>)}
        </div>
      </div>
    </div>
  )
}

// ─── Settings Tab (raw YAML editor) ───────────────────────────────────────────
function SettingsTab() {
  const [content, setContent] = useState('')
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => { api.get('settings').then(d => setContent(d.content || '')) }, [])

  const save = async () => {
    setError('')
    const r = await api.post('settings', { content })
    if (r.ok) { setSaved(true); setTimeout(() => setSaved(false), 3000) }
    else setError(r.error || 'Failed to save')
  }

  return (
    <div style={S.page}>
      {saved && <div style={S.alert('success')}>✅ Settings saved. Bot restarting...</div>}
      {error && <div style={S.alert('error')}>❌ {error}</div>}
      <div style={S.card}>
        <div style={S.cardTitle}>Raw Config Editor (settings.yaml)</div>
        <div style={{ fontSize: 13, color: '#8b949e', marginBottom: 16 }}>Edit raw YAML directly. Changes restart the bot automatically.</div>
        <textarea style={S.textarea} value={content} onChange={e => setContent(e.target.value)} spellCheck={false} />
        <div style={{ marginTop: 12, display: 'flex', gap: 12 }}>
          <button style={{ ...S.btn('#238636'), padding: '10px 28px', fontSize: 14 }} onClick={save}>💾 Save & Restart Bot</button>
          <button style={{ ...S.btn('#21262d'), padding: '10px 28px', fontSize: 14 }} onClick={() => api.get('settings').then(d => setContent(d.content || ''))}>↺ Reset</button>
        </div>
      </div>
    </div>
  )
}

// ─── Main App ─────────────────────────────────────────────────────────────────
export default function App() {
  const [authed, setAuthed] = useState(null) // null=checking, false=login, true=in
  const [tab, setTab] = useState('overview')

  useEffect(() => {
    // Check if already authenticated
    fetch('/api/proxy/status', { cache: 'no-store' })
      .then(r => setAuthed(r.ok))
      .catch(() => setAuthed(false))
  }, [])

  if (authed === null) return <div style={{ ...S.body, display: 'flex', alignItems: 'center', justifyContent: 'center' }}><div style={{ color: '#58a6ff' }}>Loading...</div></div>
  if (!authed) return <div style={S.body}><LoginPage onLogin={() => setAuthed(true)} /></div>

  const tabs = [
    { id: 'overview', label: 'Overview' },
    { id: 'signals',  label: 'Signals' },
    { id: 'params',   label: 'Parameters' },
    { id: 'logs',     label: 'Logs' },
    { id: 'settings', label: 'Settings' },
  ]

  return (
    <div style={S.body}>
      <nav style={S.nav}>
        <div style={S.brand}>🤖 Algo Trader <span style={S.badge}>v2.0</span></div>
        <div style={{ display: 'flex', gap: 4 }}>
          {tabs.map(t => <button key={t.id} style={S.navLink(tab === t.id)} onClick={() => setTab(t.id)}>{t.label}</button>)}
          <button style={{ ...S.navLink(false), color: '#f85149' }} onClick={() => { fetch('/api/auth', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({logout:true})}); setAuthed(false) }}>Logout</button>
        </div>
      </nav>
      {tab === 'overview'  && <OverviewTab />}
      {tab === 'signals'   && <SignalsTab />}
      {tab === 'params'    && <ParametersTab />}
      {tab === 'logs'      && <LogsTab />}
      {tab === 'settings'  && <SettingsTab />}
    </div>
  )
}
