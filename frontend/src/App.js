import React, { useState, useRef, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import MerkleTree from './components/MerkleTree';

const PINK = '#E89BB5';
const PINK_LIGHT = '#FFF0F5';
const WS_URL = process.env.REACT_APP_WS_URL || 'ws://localhost:8000/ws/analyze';

export default function App() {
  const [stage, setStage] = useState('input'); // input | analyzing | complete
  const [url, setUrl] = useState('');
  const [numAgents, setNumAgents] = useState(6);
  const [agents, setAgents] = useState([]);
  const [findings, setFindings] = useState([]);
  const [progress, setProgress] = useState(0); // 0-1
  const [bestScore, setBestScore] = useState(0);
  const [vaultUrl, setVaultUrl] = useState(null);
  const [statusMsg, setStatusMsg] = useState('');
  const wsRef = useRef(null);

  const startAnalysis = useCallback(() => {
    if (!url.trim()) return;
    setStage('analyzing');
    setAgents([]);
    setFindings([]);
    setProgress(0);

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      ws.send(JSON.stringify({ url: url.trim(), num_agents: numAgents }));
    };

    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      switch (msg.type) {
        case 'agent_spawn':
          setAgents(prev => [...prev, msg]);
          break;
        case 'finding':
          setFindings(prev => [...prev, msg]);
          setProgress(Math.min((msg.total_findings || 0) / (numAgents * 20), 0.95));
          break;
        case 'generation_complete':
          setBestScore(msg.best_score || 0);
          break;
        case 'status':
          setStatusMsg(msg.message);
          break;
        case 'complete':
          setProgress(1);
          setVaultUrl(msg.vault_url);
          setBestScore(msg.best_score || 0);
          setTimeout(() => setStage('complete'), 800);
          break;
        case 'error':
          setStatusMsg(`Error: ${msg.message}`);
          break;
        default: break;
      }
    };

    ws.onerror = () => setStatusMsg('Connection error — is the backend running?');
  }, [url, numAgents]);

  const bgColor = stage === 'input' ? '#ffffff'
    : stage === 'complete' ? PINK
    : `color-mix(in srgb, ${PINK} ${Math.round(progress * 100)}%, white)`;

  return (
    <motion.div
      style={{
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
      }}
    >
      {/* Merkle tree SVG layer */}
      {stage !== 'input' && (
        <MerkleTree agents={agents} findings={findings} progress={progress} />
      )}

      {/* Center content */}
      <div style={{ position: 'relative', zIndex: 10, textAlign: 'center', maxWidth: 560 }}>
        <AnimatePresence mode="wait">
          {stage === 'input' && (
            <motion.div key="input"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -20 }}
            >
              <h1 style={{ fontSize: '2rem', fontWeight: 600, color: '#1a1a1a', marginBottom: 8 }}>
                Probably Nothing
              </h1>
              <p style={{ color: '#888', marginBottom: 32, fontSize: '0.95rem' }}>
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
                  boxSizing: 'border-box', marginBottom: 16,
                  fontFamily: 'Inter, system-ui, sans-serif'
                }}
              />
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20, justifyContent: 'center' }}>
                <label style={{ color: '#666', fontSize: '0.85rem', whiteSpace: 'nowrap' }}>
                  Agents: <strong>{numAgents.toLocaleString()}</strong>
                </label>
                <input type="range" min={1} max={1000} value={numAgents}
                  onChange={e => setNumAgents(Number(e.target.value))}
                  style={{ flex: 1, accentColor: PINK, maxWidth: 200 }}
                />
              </div>
              <motion.button
                whileHover={{ scale: 1.03 }}
                whileTap={{ scale: 0.97 }}
                onClick={startAnalysis}
                style={{
                  background: PINK, color: '#fff', border: 'none',
                  padding: '14px 40px', borderRadius: 12, fontSize: '1rem',
                  fontWeight: 600, cursor: 'pointer', fontFamily: 'inherit'
                }}
              >
                Audit Hook
              </motion.button>
            </motion.div>
          )}

          {stage === 'analyzing' && (
            <motion.div key="analyzing"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              style={{ color: '#fff' }}
            >
              <p style={{ fontSize: '0.9rem', opacity: 0.85, marginBottom: 8 }}>{statusMsg}</p>
              <p style={{ fontSize: '1.1rem', fontWeight: 600 }}>
                {findings.length} findings · {agents.length} agents active
              </p>
              {bestScore > 0 && (
                <p style={{ fontSize: '0.85rem', opacity: 0.7 }}>
                  Best score: {(bestScore * 100).toFixed(1)}%
                </p>
              )}
            </motion.div>
          )}

          {stage === 'complete' && (
            <motion.div key="complete"
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              style={{ color: '#fff' }}
            >
              <h2 style={{ fontSize: '1.8rem', fontWeight: 700, marginBottom: 8 }}>
                Analysis Complete
              </h2>
              <p style={{ fontSize: '1rem', opacity: 0.9, marginBottom: 4 }}>
                {agents.length} agents · {findings.length} findings discovered
              </p>
              <p style={{ fontSize: '0.9rem', opacity: 0.75, marginBottom: 28 }}>
                Best score: {(bestScore * 100).toFixed(1)}%
              </p>
              <a href={`http://localhost:8000${vaultUrl}`} download
                style={{
                  background: '#fff', color: PINK, padding: '14px 36px',
                  borderRadius: 12, fontWeight: 700, fontSize: '1rem',
                  textDecoration: 'none', display: 'inline-block'
                }}
              >
                Download Obsidian Vault →
              </a>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </motion.div>
  );
}
