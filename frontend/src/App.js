import React, { useState, useRef, useCallback, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import MerkleTree from './components/MerkleTree';

const PINK = '#E89BB5';

function Spinner() {
  const frames = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏'];
  const [i, setI] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setI(n => (n + 1) % frames.length), 80);
    return () => clearInterval(t);
  }, [frames.length]);
  return <span style={{ opacity: 0.7 }}>{frames[i]}</span>;
}

function Blink({ text }) {
  const [on, setOn] = useState(true);
  useEffect(() => {
    const t = setInterval(() => setOn(v => !v), 600);
    return () => clearInterval(t);
  }, []);
  return <span style={{ opacity: on ? 0.8 : 0.3, fontFamily: 'monospace', fontSize: '0.82rem' }}>{text}</span>;
}
const WS_URL = process.env.REACT_APP_WS_URL ||
  `ws://${window.location.hostname}:8000/ws/analyze`;

// Persona display order for coverage matrix
const PERSONA_ORDER = [
  'router-aggregator', 'mev-searcher', 'lp-whale', 'retail-trader',
  'bridge-integrator', 'security-auditor', 'dex-listing', 'protocol-bd', 'gas-station',
];
const PERSONA_LABELS = {
  'router-aggregator': 'Router / Aggregator',
  'mev-searcher': 'MEV Searcher',
  'lp-whale': 'LP Whale',
  'retail-trader': 'Retail Trader',
  'bridge-integrator': 'Bridge Integrator',
  'security-auditor': 'Security Auditor',
  'dex-listing': 'DEX Listing',
  'protocol-bd': 'Protocol BD',
  'gas-station': 'Gas Station',
};

function parsePassRate(str) {
  // "6/9 (66.7%)" → 0.667
  const m = str && str.match(/\(([0-9.]+)%\)/);
  return m ? parseFloat(m[1]) / 100 : null;
}

export default function App() {
  const [stage, setStage] = useState('input'); // input | analyzing | complete
  const [url, setUrl] = useState('');
  const [agents, setAgents] = useState([]);
  const [findings, setFindings] = useState([]);
  const [coverage, setCoverage] = useState({});
  const [statusMsg, setStatusMsg] = useState('');
  const [statusLog, setStatusLog] = useState([]);
  const [vaultUrl, setVaultUrl] = useState(null);
  const [elapsed, setElapsed] = useState(0);
  const [totalPassed, setTotalPassed] = useState(0);
  const [totalScenarios, setTotalScenarios] = useState(0);
  const wsRef = useRef(null);
  const startTimeRef = useRef(null);
  const timerRef = useRef(null);

  // Time-based progress: fill to 90% over ~10 minutes, then hold until complete
  const [progress, setProgress] = useState(0);
  useEffect(() => {
    if (stage === 'analyzing') {
      startTimeRef.current = Date.now();
      timerRef.current = setInterval(() => {
        const secs = (Date.now() - startTimeRef.current) / 1000;
        // Asymptotic: approaches 0.9 over 600s
        setProgress(0.9 * (1 - Math.exp(-secs / 300)));
      }, 500);
    } else {
      clearInterval(timerRef.current);
      if (stage === 'complete') setProgress(1);
    }
    return () => clearInterval(timerRef.current);
  }, [stage]);

  const startAnalysis = useCallback(() => {
    if (!url.trim()) return;
    setStage('analyzing');
    setAgents([]);
    setFindings([]);
    setCoverage({});
    setStatusMsg('');
    setStatusLog([]);
    setVaultUrl(null);
    setProgress(0);
    setElapsed(0);
    setTotalPassed(0);
    setTotalScenarios(0);

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      ws.send(JSON.stringify({ url: url.trim() }));
    };

    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      console.log(`[PN] ${msg.type}`, msg);
      switch (msg.type) {
        case 'agent_spawn':
          setAgents(prev => [...prev, msg]);
          break;
        case 'finding':
          setFindings(prev => [...prev, msg]);
          break;
        case 'status':
          setStatusMsg(msg.message);
          setStatusLog(prev => [...prev.slice(-7), msg.message]);
          break;
        case 'scenario_added':
          setStatusLog(prev => [...prev.slice(-7), `✓ ${msg.contract || msg.scenario_id}`]);
          break;
        case 'scenario_rejected':
          setStatusLog(prev => [...prev.slice(-7), `✗ ${(msg.reason || '').slice(0, 80)}`]);
          break;
        case 'coverage_matrix':
        case 'coverage_update':
          setCoverage(msg.coverage || {});
          break;
        case 'complete':
          setCoverage(msg.coverage || {});
          setVaultUrl(msg.vault_url);
          setElapsed(msg.elapsed_seconds || 0);
          setTotalPassed(msg.total_passed || 0);
          setTotalScenarios(msg.total_scenarios || 0);
          setTimeout(() => setStage('complete'), 600);
          break;
        case 'error':
          setStatusMsg(`Error: ${msg.message}`);
          break;
        default: break;
      }
    };

    ws.onerror = () => setStatusMsg('Connection error — is the backend running?');
    ws.onclose = () => {
      if (stage === 'analyzing') setStatusMsg(prev => prev || 'Connection closed');
    };
  }, [url, stage]);

  const reset = useCallback(() => {
    if (wsRef.current) wsRef.current.close();
    setStage('input');
    setAgents([]);
    setFindings([]);
    setCoverage({});
    setStatusMsg('');
    setProgress(0);
  }, []);

  const bgColor = stage === 'input' ? '#ffffff'
    : stage === 'complete' ? PINK
    : `color-mix(in srgb, ${PINK} ${Math.round(progress * 100)}%, white)`;

  const overallRate = totalScenarios > 0
    ? Math.round((totalPassed / totalScenarios) * 100)
    : null;

  return (
    <motion.div style={{
      minHeight: '100vh',
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'center',
      background: bgColor,
      transition: 'background 0.8s ease',
      padding: '2rem',
      position: 'relative',
      overflow: 'hidden',
    }}>
      {stage !== 'input' && (
        <MerkleTree agents={agents} findings={findings} progress={progress} />
      )}

      <div style={{ position: 'relative', zIndex: 10, textAlign: 'center', maxWidth: 580 }}>
        <AnimatePresence mode="wait">

          {/* ── INPUT ── */}
          {stage === 'input' && (
            <motion.div key="input"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -20 }}
            >
              <h1 style={{ fontSize: '2rem', fontWeight: 700, color: '#1a1a1a', marginBottom: 8 }}>
                Probably Nothing
              </h1>
              <p style={{ color: '#999', marginBottom: 32, fontSize: '0.95rem' }}>
                Autonomous audit tool for Uniswap V4 hooks
              </p>
              <input
                type="text"
                value={url}
                onChange={e => setUrl(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && startAnalysis()}
                placeholder="Paste a Uniswap V4 hook GitHub URL"
                style={{
                  width: '100%', padding: '14px 18px', fontSize: '1rem',
                  border: `2px solid ${PINK}`, borderRadius: 12, outline: 'none',
                  boxSizing: 'border-box', marginBottom: 20,
                  fontFamily: 'Inter, system-ui, sans-serif', color: '#1a1a1a',
                }}
              />
              <motion.button
                whileHover={{ scale: 1.03 }}
                whileTap={{ scale: 0.97 }}
                onClick={startAnalysis}
                style={{
                  background: PINK, color: '#fff', border: 'none',
                  padding: '14px 40px', borderRadius: 12, fontSize: '1rem',
                  fontWeight: 600, cursor: 'pointer', fontFamily: 'inherit',
                }}
              >
                Audit Hook
              </motion.button>
            </motion.div>
          )}

          {/* ── ANALYZING ── */}
          {stage === 'analyzing' && (
            <motion.div key="analyzing"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              style={{ color: '#1a1a1a' }}
            >
              <p style={{ fontSize: '1.2rem', fontWeight: 700, marginBottom: 4 }}>
                {agents.length} agents active · {findings.length} finding{findings.length !== 1 ? 's' : ''}
              </p>

              {/* Rolling status log */}
              <div style={{
                margin: '12px 0 16px',
                background: 'rgba(0,0,0,0.06)',
                borderRadius: 10,
                padding: '10px 14px',
                textAlign: 'left',
                fontFamily: 'monospace',
                fontSize: '0.78rem',
                minHeight: 80,
              }}>
                {statusLog.length === 0
                  ? <Blink text="Connecting…" />
                  : statusLog.map((msg, i) => (
                    <div key={i} style={{
                      opacity: 0.4 + (i / statusLog.length) * 0.6,
                      lineHeight: 1.6,
                      color: '#1a1a1a',
                    }}>
                      {i === statusLog.length - 1
                        ? <><span style={{ color: PINK, fontWeight: 700 }}>›</span> {msg} <Spinner /></>
                        : <><span style={{ opacity: 0.4 }}>·</span> {msg}</>
                      }
                    </div>
                  ))
                }
              </div>

              {/* Live coverage pills */}
              {Object.keys(coverage).length > 0 && (
                <div style={{ marginTop: 20, display: 'flex', flexWrap: 'wrap', gap: 8, justifyContent: 'center' }}>
                  {PERSONA_ORDER.filter(pid => coverage[pid]).map(pid => {
                    const rate = parsePassRate(coverage[pid]);
                    const color = rate === null ? '#ccc' : rate >= 0.8 ? '#4CAF50' : rate >= 0.5 ? '#FF9800' : '#f44336';
                    return (
                      <span key={pid} style={{
                        background: color + '22', border: `1px solid ${color}`,
                        borderRadius: 20, padding: '3px 10px', fontSize: '0.75rem',
                        color: '#1a1a1a', fontWeight: 500,
                      }}>
                        {PERSONA_LABELS[pid]} {coverage[pid].split(' ')[0]}
                      </span>
                    );
                  })}
                </div>
              )}
            </motion.div>
          )}

          {/* ── COMPLETE ── */}
          {stage === 'complete' && (
            <motion.div key="complete"
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              style={{ color: '#fff' }}
            >
              <h2 style={{ fontSize: '1.8rem', fontWeight: 700, marginBottom: 4 }}>
                Analysis Complete
              </h2>
              <p style={{ fontSize: '1rem', opacity: 0.85, marginBottom: 2 }}>
                {overallRate !== null ? `${overallRate}% pass rate` : ''} · {findings.length} finding{findings.length !== 1 ? 's' : ''}
              </p>
              <p style={{ fontSize: '0.8rem', opacity: 0.65, marginBottom: 20 }}>
                {totalPassed}/{totalScenarios} tests passed · {Math.round(elapsed)}s
              </p>

              {/* Coverage matrix */}
              <div style={{ marginBottom: 24, display: 'flex', flexWrap: 'wrap', gap: 8, justifyContent: 'center' }}>
                {PERSONA_ORDER.filter(pid => coverage[pid]).map(pid => {
                  const rate = parsePassRate(coverage[pid]);
                  const color = rate === null ? 'rgba(255,255,255,0.3)'
                    : rate >= 0.8 ? 'rgba(100,220,100,0.25)'
                    : rate >= 0.5 ? 'rgba(255,180,0,0.25)'
                    : 'rgba(255,100,100,0.25)';
                  const border = rate === null ? 'rgba(255,255,255,0.4)'
                    : rate >= 0.8 ? 'rgba(100,220,100,0.7)'
                    : rate >= 0.5 ? 'rgba(255,180,0,0.7)'
                    : 'rgba(255,100,100,0.7)';
                  return (
                    <span key={pid} style={{
                      background: color, border: `1px solid ${border}`,
                      borderRadius: 20, padding: '4px 12px', fontSize: '0.78rem',
                      color: '#fff', fontWeight: 500,
                    }}>
                      {PERSONA_LABELS[pid]} {coverage[pid].split(' ')[0]}
                    </span>
                  );
                })}
              </div>

              {/* Findings list */}
              {findings.length > 0 && (
                <div style={{
                  background: 'rgba(0,0,0,0.12)', borderRadius: 12, padding: '12px 16px',
                  marginBottom: 24, textAlign: 'left', maxHeight: 180, overflowY: 'auto',
                }}>
                  {findings.slice(0, 12).map((f, i) => (
                    <p key={i} style={{
                      fontSize: '0.78rem', opacity: 0.9, margin: '4px 0',
                      lineHeight: 1.4, borderBottom: '1px solid rgba(255,255,255,0.1)',
                      paddingBottom: 4,
                    }}>
                      <strong>{PERSONA_LABELS[f.persona_id] || f.agent_id}:</strong>{' '}
                      {f.text && f.text.length > 120 ? f.text.slice(0, 120) + '…' : f.text}
                    </p>
                  ))}
                  {findings.length > 12 && (
                    <p style={{ fontSize: '0.75rem', opacity: 0.6, margin: '4px 0' }}>
                      +{findings.length - 12} more in vault
                    </p>
                  )}
                </div>
              )}

              <div style={{ display: 'flex', gap: 12, justifyContent: 'center', flexWrap: 'wrap' }}>
                {vaultUrl && (
                  <a href={`http://${window.location.hostname}:8000${vaultUrl}`} download
                    style={{
                      background: '#fff', color: PINK, padding: '12px 28px',
                      borderRadius: 12, fontWeight: 700, fontSize: '0.95rem',
                      textDecoration: 'none', display: 'inline-block',
                    }}
                  >
                    Download Vault →
                  </a>
                )}
                <motion.button
                  whileHover={{ scale: 1.03 }} whileTap={{ scale: 0.97 }}
                  onClick={reset}
                  style={{
                    background: 'transparent', color: '#fff',
                    border: '2px solid rgba(255,255,255,0.6)',
                    padding: '12px 28px', borderRadius: 12, fontSize: '0.95rem',
                    fontWeight: 600, cursor: 'pointer', fontFamily: 'inherit',
                  }}
                >
                  Audit Another
                </motion.button>
              </div>
            </motion.div>
          )}

        </AnimatePresence>
      </div>
    </motion.div>
  );
}
