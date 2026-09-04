# Agent Terminal frontend

React/Vite source for the Agent Terminal UI. The Python server lives in `../terminal`.

## Development

Start the backend on port `8787`, then run:

```bash
npm ci
npm run dev
```

Vite proxies API and websocket traffic to the backend.

## Checks and production build

```bash
npm test
npm run lint
npm run build
```

The production build is written to `../terminal/static` and served by `terminal/run.py`.

Do not put Kraken or OpenRouter credentials in frontend code or Vite environment files. Browser-delivered variables are public.
