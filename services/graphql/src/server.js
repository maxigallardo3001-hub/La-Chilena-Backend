const crypto = require('crypto');
const fs = require('fs');
const https = require('https');
const express = require('express');
const { ApolloServer } = require('apollo-server-express');
const { GraphQLError } = require('graphql');

const typeDefs = require('./schema');
const { resolvers, createProductLoader } = require('./resolvers');
const { depthRule, costPlugin } = require('./limits');
const { createClient } = require('./http');
const { loadSecrets } = require('./vault');

const PORT = parseInt(process.env.PORT || '8700', 10);
const MAX_DEPTH = 5;
const MAX_COST = 200;

function sameSecret(candidate, expected) {
  const a = Buffer.from(String(candidate || ''));
  const b = Buffer.from(String(expected || ''));
  return a.length === b.length && a.length > 0 && crypto.timingSafeEqual(a, b);
}

function envelope(res, status, code, message, correlationId) {
  res.status(status).json({ error: { code, message, correlationId } });
}

async function main() {
  const secrets = await loadSecrets();
  const orders = createClient({ baseUrl: process.env.ORDERS_URL, credential: secrets.orders_credential });
  const inventory = createClient({ baseUrl: process.env.INVENTORY_URL, credential: secrets.inventory_credential });
  const payments = createClient({ baseUrl: process.env.PAYMENTS_URL, credential: secrets.payments_credential });

  const app = express();
  app.disable('x-powered-by');
  app.get('/health', (_req, res) => res.json({ status: 'ok', service: 'graphql' }));

  app.use('/graphql', (req, res, next) => {
    const correlationId = req.get('x-correlation-id') || crypto.randomUUID();
    req.correlationId = correlationId;
    if (!sameSecret(req.get('x-service-credential'), secrets['accept_api-gateway'])) {
      return envelope(res, 403, 'FORBIDDEN', 'Llamada no autorizada', correlationId);
    }
    const userId = req.get('x-authenticated-user');
    if (!userId) return envelope(res, 401, 'UNAUTHENTICATED', 'Se requiere una sesión válida', correlationId);
    req.identity = { userId, roles: (req.get('x-authenticated-roles') || '').split(',').filter(Boolean) };
    return next();
  });

  const requireOperationName = {
    async requestDidStart() {
      return {
        async didResolveOperation({ request }) {
          if (!request.operationName) {
            throw new GraphQLError('operationName es obligatorio', undefined, undefined, undefined, undefined, undefined,
              { code: 'BAD_USER_INPUT' });
          }
        },
      };
    },
  };

  const server = new ApolloServer({
    typeDefs,
    resolvers,
    introspection: process.env.NODE_ENV !== 'production',
    validationRules: [depthRule({ GraphQLError, maxDepth: MAX_DEPTH })],
    plugins: [requireOperationName, costPlugin({ GraphQLError, maxCost: MAX_COST })],
    context: ({ req }) => {
      const ctx = { identity: req.identity, correlationId: req.correlationId, orders, inventory, payments };
      ctx.loaders = { product: createProductLoader(ctx) };
      return ctx;
    },
    formatError: (error) => ({
      message: error.message,
      path: error.path,
      extensions: {
        code: (error.extensions && error.extensions.code) === 'INTERNAL_SERVER_ERROR' ? 'INTERNAL' : (error.extensions && error.extensions.code) || 'INTERNAL',
        correlationId: error.extensions && error.extensions.correlationId,
      },
    }),
  });
  await server.start();
  server.applyMiddleware({ app, path: '/graphql', cors: false, bodyParserConfig: { limit: '4kb' } });

  app.use((error, req, res, _next) => {
    const status = error.status || error.statusCode || 500;
    const code = status === 413 ? 'PAYLOAD_TOO_LARGE' : status === 400 ? 'BAD_REQUEST' : 'INTERNAL_ERROR';
    envelope(res, status, code, status === 500 ? 'Error interno del servicio' : 'Solicitud no válida', req.correlationId);
  });

  const tls = { key: fs.readFileSync('/certs/graphql.key'), cert: fs.readFileSync('/certs/graphql.crt') };
  https.createServer(tls, app).listen(PORT, '0.0.0.0', () => {
    console.log(JSON.stringify({ level: 'INFO', service: 'graphql', msg: 'listening', port: PORT }));
  });
}

main().catch((error) => {
  console.error(JSON.stringify({ level: 'ERROR', service: 'graphql', msg: 'startup_failed', error: error.message }));
  process.exit(1);
});
