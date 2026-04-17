import React, { useEffect, useState } from 'react';
import { motion } from 'framer-motion';

const PINK = '#E89BB5';
const PINK_GLOW = '#FF6B9D';
const PINK_DIM = '#D4A0B8';

function getAgentPosition(direction, index, total) {
  const spread = 200;
  const offset = total <= 1 ? 0 : ((index / (total - 1)) - 0.5) * spread;
  const D = 240;
  const Dd = 170;
  switch (direction) {
    case 'top':         return { x: offset, y: -D };
    case 'top-right':   return { x: Dd, y: -Dd };
    case 'right':       return { x: D, y: offset };
    case 'bottom-right':return { x: Dd, y: Dd };
    case 'bottom':      return { x: offset, y: D };
    case 'bottom-left': return { x: -Dd, y: Dd };
    case 'left':        return { x: -D, y: offset };
    case 'left-bottom': return { x: -D, y: 80 };
    case 'top-left':    return { x: -Dd, y: -Dd };
    default:            return { x: offset, y: -D };
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

  const byDir = agents.reduce((acc, a) => {
    const d = a.direction || 'top';
    acc[d] = acc[d] || [];
    acc[d].push(a);
    return acc;
  }, {});

  const agentPositions = agents.map((agent) => {
    const dir = agent.direction || 'top';
    const siblings = byDir[dir] || [];
    const sibIdx = siblings.indexOf(agent);
    const pos = getAgentPosition(dir, sibIdx, siblings.length);
    return { ...agent, px: cx + pos.x, py: cy + pos.y };
  });

  const findingsByAgent = findings.reduce((acc, f) => {
    acc[f.agent_id] = acc[f.agent_id] || [];
    acc[f.agent_id].push(f);
    return acc;
  }, {});

  return (
    <svg style={{ position: 'fixed', top: 0, left: 0, width: '100%', height: '100%', pointerEvents: 'none', zIndex: 1 }}>
      <defs>
        <filter id="glow">
          <feGaussianBlur stdDeviation="3" result="coloredBlur"/>
          <feMerge><feMergeNode in="coloredBlur"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
      </defs>

      {agentPositions.map((agent, i) => (
        <motion.path key={`line-${agent.agent_id}`}
          d={`M ${cx} ${cy} L ${agent.px} ${agent.py}`}
          stroke={PINK} strokeWidth={1.5} fill="none"
          initial={{ pathLength: 0, opacity: 0 }}
          animate={{ pathLength: 1, opacity: 0.6 }}
          transition={{ duration: 0.8, delay: i * 0.1 }}
          filter="url(#glow)"
        />
      ))}

      {agentPositions.map((agent, i) => {
        const agFindings = findingsByAgent[agent.agent_id] || [];
        const hasFindings = agFindings.length > 0;
        return (
          <g key={`agent-${agent.agent_id}`}>
            <motion.circle
              cx={agent.px} cy={agent.py} r={18}
              fill={hasFindings ? PINK_GLOW : PINK_DIM}
              opacity={0.9}
              initial={{ scale: 0, opacity: 0 }}
              animate={{ scale: [0, 1.15, 1], opacity: 0.9 }}
              transition={{ duration: 0.5, delay: i * 0.1 + 0.3 }}
              filter="url(#glow)"
            />
            {hasFindings && (
              <motion.circle
                cx={agent.px} cy={agent.py} r={24}
                fill="none" stroke={PINK_GLOW} strokeWidth={1.5}
                animate={{ r: [18, 34, 18], opacity: [0.7, 0, 0.7] }}
                transition={{ duration: 2.2, repeat: Infinity }}
              />
            )}
            <motion.text
              x={agent.px} y={agent.py + 32}
              textAnchor="middle" fontSize={9} fill="#333"
              fontFamily="Inter, system-ui, sans-serif" fontWeight="500"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: i * 0.1 + 0.6 }}
            >
              {agent.label}
            </motion.text>

            {agFindings.slice(0, 8).map((finding, fi) => {
              const angle = (fi / Math.max(agFindings.length, 1)) * Math.PI - Math.PI / 2;
              const lx = agent.px + Math.cos(angle) * 60;
              const ly = agent.py + Math.sin(angle) * 60;
              return (
                <g key={`finding-${agent.agent_id}-${fi}`}>
                  <motion.path
                    d={`M ${agent.px} ${agent.py} L ${lx} ${ly}`}
                    stroke={PINK_GLOW} strokeWidth={1} fill="none"
                    initial={{ pathLength: 0, opacity: 0 }}
                    animate={{ pathLength: 1, opacity: 0.5 }}
                    transition={{ duration: 0.4, delay: fi * 0.1 }}
                  />
                  <motion.circle cx={lx} cy={ly} r={4}
                    fill={PINK_GLOW} opacity={0.7}
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
