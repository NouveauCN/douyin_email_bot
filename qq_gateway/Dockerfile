FROM node:20-bookworm-slim

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts
COPY src ./src

RUN mkdir -p /app/qq-gateway-state && chown -R node:node /app
USER node
ENV NODE_ENV=production
CMD ["node", "src/main.js"]
