const DataLoader = require('dataloader');
const { GraphQLError } = require('graphql');
const { UpstreamError } = require('./http');
const { MAX_PAGE } = require('./limits');

const ORDER_ID = /^[A-Za-z0-9_-]{1,64}$/;
const CLIENT_CANCEL = ['pendiente_pago', 'pagado'];

function appError(ctx, code, message) {
  return new GraphQLError(message, undefined, undefined, undefined, undefined, undefined,
    { code, correlationId: ctx.correlationId });
}

async function read(ctx, client, path, options = {}) {
  let response;
  try {
    response = await client.get(path, { identity: ctx.identity, correlationId: ctx.correlationId });
  } catch (error) {
    if (error instanceof UpstreamError || error.kind) throw appError(ctx, 'SERVICE_UNAVAILABLE', options.unavailable || 'Servicio no disponible');
    throw error;
  }
  if (response.status === 401) throw appError(ctx, 'UNAUTHENTICATED', 'Se requiere una sesión válida');
  if (response.status === 403) throw appError(ctx, 'FORBIDDEN', options.forbidden || 'No tienes acceso a este recurso');
  if (response.status === 404) throw appError(ctx, 'NOT_FOUND', options.notFound || 'No encontrado');
  if (response.status >= 500 || response.status === 0) throw appError(ctx, 'SERVICE_UNAVAILABLE', options.unavailable || 'Servicio no disponible');
  if (response.status !== 200 || !response.body) throw appError(ctx, 'SERVICE_UNAVAILABLE', options.unavailable || 'Servicio no disponible');
  return response.body;
}

function edgeCursor(order) {
  return Buffer.from(`${order.createdAt}|${order.id}`).toString('base64').replace(/\+/g, '-').replace(/\//g, '_');
}

function toPedido(order) {
  return {
    id: order.id,
    creadoEn: order.createdAt,
    estado: String(order.status).toUpperCase(),
    total: order.total,
    puedeAnular: CLIENT_CANCEL.includes(order.status),
    items: (order.items || []).map((item) => ({
      nombre: item.name, cantidad: item.quantity, precioUnitario: item.unitPrice, productId: item.productId,
    })),
  };
}

function createProductLoader(ctx) {
  return new DataLoader(async (ids) => {
    const body = await read(ctx, ctx.inventory, `/internal/products?ids=${encodeURIComponent(ids.join(','))}`,
      { unavailable: 'Catálogo no disponible' });
    const byId = new Map(body.items.map((p) => [p.id, { id: p.id, nombre: p.name, descripcion: p.description, disponible: p.available }]));
    return ids.map((id) => byId.get(id) || null);
  }, { maxBatchSize: 50 });
}

const resolvers = {
  Query: {
    async misPedidos(_root, args, ctx) {
      if (!ctx.identity.roles.includes('cliente')) throw appError(ctx, 'FORBIDDEN', 'Solo los clientes pueden consultar sus pedidos');
      const size = args.primeros === undefined || args.primeros === null ? 10 : args.primeros;
      if (!Number.isInteger(size) || size < 1 || size > MAX_PAGE) {
        throw new GraphQLError(`primeros debe estar entre 1 y ${MAX_PAGE}`, undefined, undefined, undefined, undefined, undefined,
          { code: 'BAD_USER_INPUT', correlationId: ctx.correlationId });
      }
      const query = new URLSearchParams({ size: String(size), include: 'items' });
      if (args.estado) query.set('status', args.estado.toLowerCase());
      if (args.despues) query.set('after', args.despues);
      const body = await read(ctx, ctx.orders, `/orders/mine?${query}`, { unavailable: 'Pedidos no disponibles' });
      return {
        edges: body.items.map((order) => ({ cursor: edgeCursor(order), node: toPedido(order) })),
        pageInfo: { hasNextPage: Boolean(body.nextCursor), endCursor: body.nextCursor || null },
      };
    },

    async resumenPedido(_root, args, ctx) {
      if (!ORDER_ID.test(args.orderId)) throw appError(ctx, 'NOT_FOUND', 'Pedido no encontrado');
      const order = await read(ctx, ctx.orders, `/orders/${args.orderId}`, {
        forbidden: 'No tienes acceso a este pedido', notFound: 'Pedido no encontrado', unavailable: 'Pedidos no disponibles' });
      return {
        orderId: order.id,
        pedido: toPedido(order),
        medioPago: String(order.paymentMethod).toUpperCase(),
        historial: (order.history || []).map((h) => ({ estado: String(h.status).toUpperCase(), en: h.at })),
        direccionEntrega: order.deliveryAddress === undefined ? null : order.deliveryAddress,
      };
    },
  },

  ItemPedido: {
    producto: (item, _args, ctx) => ctx.loaders.product.load(item.productId),
  },

  ResumenPedido: {
    pago: async (summary, _args, ctx) => {
      if (ctx.identity.roles.some((r) => r === 'despacho' || r === 'dueno')) {
        throw appError(ctx, 'FORBIDDEN', 'No tienes acceso a los datos de pago');
      }
      try {
        const payment = await read(ctx, ctx.payments, `/internal/payments/by-order/${summary.orderId}`,
          { unavailable: 'Pagos no disponibles' });
        return { estado: String(payment.status).toUpperCase(), pagadoEn: payment.paidAt, boletaDisponible: payment.receiptAvailable };
      } catch (error) {
        if (error.extensions && error.extensions.code === 'NOT_FOUND') return null;
        throw error;
      }
    },
    direccionEntrega: (summary, _args, ctx) => {
      if (!ctx.identity.roles.some((r) => r === 'cliente' || r === 'despacho')) {
        throw appError(ctx, 'FORBIDDEN', 'No tienes acceso a la dirección de entrega');
      }
      return summary.direccionEntrega;
    },
  },
};

module.exports = { resolvers, createProductLoader, appError, toPedido, edgeCursor };
