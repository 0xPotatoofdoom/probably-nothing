# Probably Nothing

**An autonomous audit tool for Uniswap V4 hooks.**

Paste a GitHub link to any V4 hook. Watch a live merkle tree of research agents bloom across the screen as they test swap scenarios, LP deployments, MEV resistance, and edge cases. When done, download an Obsidian vault with everything they found.

## Quick Start

### Backend
```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
```

### Frontend
```bash
cd frontend
npm install
npm start
```

### Full stack (Docker Compose)
```bash
docker-compose up
```

## Architecture
- **Frontend**: React + Framer Motion (SVG merkle tree animation)
- **Backend**: FastAPI + asyncio + WebSocket
- **Testing**: Foundry in Docker
- **Output**: Obsidian vault .zip

## Agent Roles
- Gas Optimizer
- MEV Sentinel
- LP Deployer
- Swap Scenario
- Edge Case Hunter
- Security Auditor

Configurable from 1–1000 agents via slider.

## Mutation Order
1. Parametric (fee tiers, uint constants, booleans)
2. Structural (hook permission flags, oracle integrations)
3. LLM-assisted (domain-aware proposals)
