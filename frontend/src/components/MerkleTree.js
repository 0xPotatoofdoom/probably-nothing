import React, { useEffect, useRef, useState } from 'react';
import { motion } from 'framer-motion';

const PINK = '#E89BB5';
const PINK_GLOW = '#FF6B9D';
const PINK_DIM = '#C9A0B4';
const CENTER = { x: 0, y: 0 }; // relative to viewport center

function getAgentPosition(direction, index, total, spread = 280) {
  // Spread N agents across a direction, fanning out from center
  const offset = total <= 1 ? 0 : ((index / (total - 1)) - 0.5) * spread;
  switch (direction) {
    case 'top':    return { x: offset, y: -220 };
    case 'bottom': return { x: offset, y: 220 };
    case 'left':   return { x: -320, y: offset };
    case 'right':  return { x: 320, y: offset };
    default:       return { x: offset * 1.5, y: -200 };
  }
}

export default function MerkleTree({ agents, findings, progress }) {
  const [dimensions, setDimensions] = useState({ w: window.innerWidth, h: window.innerHeight });

  useEffect(() => {
    const handler = () => setDimensions({ w: window.innerWidth, h: window.innerHeight });
    window.addEventListener('resize', handler);
    return () => window.removeEventListener('resize', handler);
  }, []);

  const cx = dimensions.w / 2;
  const cy = dimensions.h / 2;

  // Group agents by direction
  const byDir = agents.reduce((acc, a) => {
    const d = a.direction || 'top';
    acc[d] = acc[d] || [];
    acc[d].push(a);
    return acc;
  }, {});

  const agentPositions = agents.map((agent, i) => {
    const dir = agent.direction || 'top';
    const siblings = byDir[dir] || [];
    const sibIdx = siblings.indexOf(agent);
    const pos = getAgentPosition(dir, sibIdx, siblings.length);
    return { ...agent, px: cx + pos.x, py: cy + pos.y };
  });

  // Attach findings to agent nodes as leaf branches
  const findingsByAgent = findings.reduce((acc, f) => {
    acc[f.agent_id] = acc[f.agent_id] || [];
    acc[f.agent_id].push(f);
    return acc;
  }, {});

  return (
    <svg
      style={{ position: 'fixed', top: 0, left: 0, width: '100%', height: '100%', pointerEvents: 'none', zIndex: 1 }}
    >
      <defs>
        <filter id="glow">
          <feGaussianBlur stdDeviation="3" result="coloredBlur"/>
          <feMerge><feMergeNode in="coloredBlur"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
      </defs>

      {/* Root → agent lines */}
      {agentPositions.map((agent, i) => (
        <motion.line key={`line-${agent.agent_id}`}
          x1={cx} y1={cy} x2={agent.px} y2={agent.py}
          stroke={PINK} strokeWidth={2} opacity={0.7}
          initial={{ pathLength: 0, opacity: 0 }}
          animate={{ pathLength: 1, opacity: 0.7 }}
          transition={{ duration: 0.8, delay: i * 0.25 }}
          filter="url(#glow)"
        />
      ))}

      {/* Agent nodes */}
      {agentPositions.map((agent, i) => {
        const agFindings = findingsByAgent[agent.agent_id] || [];
        const isActive = agFindings.length > 0;
        return (
          <g key={`agent-${agent.agent_id}`}>
            <motion.circle
              cx={agent.px} cy={agent.py} r={20}
              fill={isActive ? PINK_GLOW : PINK_DIM}
              opacity={0.9}
              initial={{ scale: 0, opacity: 0 }}
              animate={{ scale: [0, 1.2, 1], opacity: 0.9 }}
              transition={{ duration: 0.5, delay: i * 0.25 + 0.4 }}
              filter="url(#glow)"
            />
            {/* Pulse ring when active */}
            {isActive && (
              <motion.circle
                cx={agent.px} cy={agent.py} r={26}
                fill="none" stroke={PINK_GLOW} strokeWidth={2}
                animate={{ r: [20, 36, 20], opacity: [0.8, 0, 0.8] }}
                transition={{ duration: 2, repeat: Infinity }}
              />
            )}
            <motion.text
              x={agent.px} y={agent.py + 36}
              textAnchor="middle" fontSize={10} fill="#fff" fontFamily="Inter, system-ui"
              initial={{ opacity: 0 }}
              animate={{ opacity: 0.9 }}
              transition={{ delay: i * 0.25 + 0.7 }}
            >
              {agent.label}
            </motion.text>

            {/* Finding leaf branches */}
            {agFindings.slice(0, 8).map((finding, fi) => {
              const angle = (fi / Math.max(agFindings.length, 1)) * Math.PI - Math.PI / 2;
              const lx = agent.px + Math.cos(angle) * 70;
              const ly = agent.py + Math.sin(angle) * 70;
              return (
                <g key={`finding-${agent.agent_id}-${fi}`}>
                  <motion.line
                    x1={agent.px} y1={agent.py} x2={lx} y2={ly}
                    stroke={PINK} strokeWidth={1} opacity={0.4}
                    initial={{ pathLength: 0 }}
                    animate={{ pathLength: 1 }}
                    transition={{ duration: 0.4, delay: fi * 0.1 }}
                  />
                  <motion.circle cx={lx} cy={ly} r={4}
                    fill={PINK} opacity={0.6}
                    initial={{ scale: 0 }}
                    animate={{ scale: 1 }}
                    transition={{ delay: fi * 0.1 + 0.3 }}
                  />
                </g>
              );
            })}
          </g>
        );
      })}
    </svg>
  );
}
