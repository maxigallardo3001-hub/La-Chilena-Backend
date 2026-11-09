FROM node:22-slim
WORKDIR /app
COPY services/graphql/package.json services/graphql/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY services/graphql/src ./src
ENV NODE_ENV=production
USER node
CMD ["node", "src/server.js"]
