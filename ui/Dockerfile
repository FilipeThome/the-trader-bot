# Operator panel: the production build served by `vite preview`, whose proxy adds the API token.
FROM node:22-alpine

ENV BOT_RUNTIME_ROOT=/data \
    BOT_PANEL_HOST=0.0.0.0

WORKDIR /app
RUN chown node:node /app
USER node

COPY --chown=node:node package.json package-lock.json ./
RUN npm ci
COPY --chown=node:node . .
# vite preview bundles its config into .vite-temp at start; any uid set in compose must write it.
RUN npm run build && mkdir -p node_modules/.vite-temp && chmod 1777 node_modules/.vite-temp

EXPOSE 5173
# Run vite directly: npx would need a writable npm cache under an arbitrary uid.
CMD ["node", "node_modules/vite/bin/vite.js", "preview"]
