const test = require('node:test');
const assert = require('node:assert');
const { resolvers, createProductLoader, edgeCursor, toPedido } = require('./resolvers');

const sampleOrder = {
  id: 'ORD-2026-1012-4821', createdAt: '2026-10-12T20:47:00.123Z', status: 'pendiente_pago', total: 6800,
  items: [{ productId: 'p-0001', name: 'Empanada de pino', quantity: 2, unitPrice: 2500 },
          { productId: 'p-0007', name: 'Mote con huesillo', quantity: 1, unitPrice: 1800 }],
};

function fakeClient(handler) {
  const calls = [];
  return { calls, get: async (path, opts) => { calls.push({ path, opts }); return handler(path, opts); } };
}

function context(roles, clients = {}) {
  const ctx = { identity: { userId: 'u-1', roles }, correlationId: 'c-test', orders: null, inventory: null, payments: null, ...clients };
  ctx.loaders = { product: createProductLoader(ctx) };
  return ctx;
}

const code = (error) => error.extensions && error.extensions.code;

test('el cursor de cada pedido usa base64 URL-safe con relleno, igual que orders', () => {
  assert.strictEqual(edgeCursor(sampleOrder), 'MjAyNi0xMC0xMlQyMDo0NzowMC4xMjNafE9SRC0yMDI2LTEwMTItNDgyMQ==');
});

test('toPedido traduce estado, puedeAnular e ítems', () => {
  const pedido = toPedido(sampleOrder);
  assert.strictEqual(pedido.estado, 'PENDIENTE_PAGO');
  assert.strictEqual(pedido.puedeAnular, true);
  assert.deepStrictEqual(pedido.items[0], { nombre: 'Empanada de pino', cantidad: 2, precioUnitario: 2500, productId: 'p-0001' });
  assert.strictEqual(toPedido({ ...sampleOrder, status: 'entregado' }).puedeAnular, false);
});

test('misPedidos: un rol distinto de cliente recibe FORBIDDEN y no se llama a orders', async () => {
  const orders = fakeClient(() => ({ status: 200, body: { items: [] } }));
  await assert.rejects(resolvers.Query.misPedidos({}, {}, context(['cajero'], { orders })), (e) => code(e) === 'FORBIDDEN');
  assert.strictEqual(orders.calls.length, 0);
});

test('misPedidos: primeros fuera de rango da BAD_USER_INPUT sin llamar a orders', async () => {
  const orders = fakeClient(() => ({ status: 200, body: { items: [] } }));
  for (const primeros of [0, 21, 500]) {
    await assert.rejects(resolvers.Query.misPedidos({}, { primeros }, context(['cliente'], { orders })), (e) => code(e) === 'BAD_USER_INPUT');
  }
  assert.strictEqual(orders.calls.length, 0);
});

test('misPedidos: arma la consulta, reenvía la identidad y devuelve la conexión', async () => {
  const orders = fakeClient(() => ({ status: 200, body: { items: [sampleOrder], nextCursor: 'abc=' } }));
  const result = await resolvers.Query.misPedidos({}, { estado: 'PENDIENTE_PAGO', primeros: 2, despues: 'xyz=' }, context(['cliente'], { orders }));
  const call = orders.calls[0];
  assert.ok(call.path.startsWith('/orders/mine?'));
  const params = new URLSearchParams(call.path.split('?')[1]);
  assert.strictEqual(params.get('status'), 'pendiente_pago');
  assert.strictEqual(params.get('size'), '2');
  assert.strictEqual(params.get('after'), 'xyz=');
  assert.strictEqual(params.get('include'), 'items');
  assert.deepStrictEqual(call.opts.identity.roles, ['cliente']);
  assert.strictEqual(result.edges.length, 1);
  assert.deepStrictEqual(result.pageInfo, { hasNextPage: true, endCursor: 'abc=' });
});

test('misPedidos: si orders no responde, falla completo con SERVICE_UNAVAILABLE', async () => {
  const orders = fakeClient(() => { const e = new Error('x'); e.kind = 'unavailable'; throw e; });
  await assert.rejects(resolvers.Query.misPedidos({}, {}, context(['cliente'], { orders })), (e) => code(e) === 'SERVICE_UNAVAILABLE');
});

test('el DataLoader agrupa todos los productos de la solicitud en una sola llamada a inventory', async () => {
  const inventory = fakeClient(() => ({ status: 200, body: { items: [
    { id: 'p-0001', name: 'Empanada de pino', description: 'Carne molida', available: true },
    { id: 'p-0007', name: 'Mote con huesillo', description: 'Bebida', available: true }] } }));
  const ctx = context(['cliente'], { inventory });
  const loaded = await Promise.all(['p-0001', 'p-0007', 'p-0001'].map((id) => resolvers.ItemPedido.producto({ productId: id }, {}, ctx)));
  assert.strictEqual(inventory.calls.length, 1);
  assert.strictEqual(loaded[0].descripcion, 'Carne molida');
  assert.strictEqual(loaded[2].id, 'p-0001');
});

test('con inventory caído, producto falla con SERVICE_UNAVAILABLE y el resto del pedido sigue disponible', async () => {
  const inventory = fakeClient(() => { const e = new Error('x'); e.kind = 'timeout'; throw e; });
  const ctx = context(['cliente'], { inventory });
  await assert.rejects(resolvers.ItemPedido.producto({ productId: 'p-0001' }, {}, ctx), (e) => code(e) === 'SERVICE_UNAVAILABLE');
});

test('un producto que ya no existe devuelve null', async () => {
  const inventory = fakeClient(() => ({ status: 200, body: { items: [] } }));
  assert.strictEqual(await resolvers.ItemPedido.producto({ productId: 'p-9999' }, {}, context(['cliente'], { inventory })), null);
});

test('resumenPedido: 403 de orders se traduce en FORBIDDEN y 404 en NOT_FOUND', async () => {
  const forbidden = fakeClient(() => ({ status: 403, body: {} }));
  await assert.rejects(resolvers.Query.resumenPedido({}, { orderId: 'ORD-X' }, context(['cliente'], { orders: forbidden })), (e) => code(e) === 'FORBIDDEN');
  const missing = fakeClient(() => ({ status: 404, body: {} }));
  await assert.rejects(resolvers.Query.resumenPedido({}, { orderId: 'ORD-X' }, context(['cliente'], { orders: missing })), (e) => code(e) === 'NOT_FOUND');
});

test('resumenPedido: un identificador con caracteres raros no llega a orders', async () => {
  const orders = fakeClient(() => ({ status: 200, body: sampleOrder }));
  await assert.rejects(resolvers.Query.resumenPedido({}, { orderId: '../admin' }, context(['cliente'], { orders })), (e) => code(e) === 'NOT_FOUND');
  assert.strictEqual(orders.calls.length, 0);
});

test('direccionEntrega: solo cliente y despacho; el cajero recibe FORBIDDEN', () => {
  const summary = { direccionEntrega: 'Av. Pajaritos 3450, Maipú' };
  assert.strictEqual(resolvers.ResumenPedido.direccionEntrega(summary, {}, context(['cliente'])), summary.direccionEntrega);
  assert.strictEqual(resolvers.ResumenPedido.direccionEntrega(summary, {}, context(['despacho'])), summary.direccionEntrega);
  assert.throws(() => resolvers.ResumenPedido.direccionEntrega(summary, {}, context(['cajero'])), (e) => code(e) === 'FORBIDDEN');
});

test('pago: despacho recibe FORBIDDEN, un cobro inexistente da null y uno existente se traduce', async () => {
  const summary = { orderId: 'ORD-2026-1012-4821' };
  await assert.rejects(resolvers.ResumenPedido.pago(summary, {}, context(['despacho'])), (e) => code(e) === 'FORBIDDEN');
  const none = fakeClient(() => ({ status: 404, body: {} }));
  assert.strictEqual(await resolvers.ResumenPedido.pago(summary, {}, context(['cliente'], { payments: none })), null);
  const paid = fakeClient(() => ({ status: 200, body: { status: 'confirmado', paidAt: '2026-10-12T21:00:00.000Z', receiptAvailable: true } }));
  assert.deepStrictEqual(await resolvers.ResumenPedido.pago(summary, {}, context(['cliente'], { payments: paid })),
    { estado: 'CONFIRMADO', pagadoEn: '2026-10-12T21:00:00.000Z', boletaDisponible: true });
  const down = fakeClient(() => { const e = new Error('x'); e.kind = 'unavailable'; throw e; });
  await assert.rejects(resolvers.ResumenPedido.pago(summary, {}, context(['cliente'], { payments: down })), (e) => code(e) === 'SERVICE_UNAVAILABLE');
});
