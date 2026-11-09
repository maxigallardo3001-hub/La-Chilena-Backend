const test = require('node:test');
const assert = require('node:assert');
const { depthOf, costOf } = require('./limits');

const field = (name, children, args) => ({
  kind: 'Field', name: { value: name }, arguments: args || [],
  selectionSet: children ? { selections: children } : undefined,
});
const intArg = (name, value) => ({ name: { value: name }, value: { kind: 'IntValue', value: String(value) } });
const root = (selections) => ({ selections });

const listQuery = (pageSize, extra = []) => root([
  field('misPedidos', [
    field('edges', [field('node', [
      field('id'), field('estado'), field('total'), field('puedeAnular'),
      field('items', [field('nombre'), field('cantidad'), field('precioUnitario'),
        field('producto', [field('descripcion'), field('disponible')])]),
      ...extra,
    ])]),
    field('pageInfo', [field('hasNextPage'), field('endCursor')]),
  ], pageSize === undefined ? [] : [intArg('primeros', pageSize)]),
]);

test('la consulta de Mis pedidos tiene profundidad 5, el máximo permitido', () => {
  assert.strictEqual(depthOf(listQuery(10), {}), 5);
});

test('una consulta de 6 niveles se mide como 6 (se rechazaría con máximo 5)', () => {
  const tooDeep = root([field('a', [field('b', [field('c', [field('d', [field('e', [field('f', [field('g')])])])])])])]);
  assert.strictEqual(depthOf(tooDeep, {}), 6);
});

test('los campos escalares no suman profundidad', () => {
  assert.strictEqual(depthOf(root([field('x'), field('y')]), {}), 0);
});

test('los fragmentos cuentan y un ciclo no cuelga la validación', () => {
  const fragments = { F: { selectionSet: { selections: [field('a', [{ kind: 'FragmentSpread', name: { value: 'F' } }])] } } };
  const query = root([{ kind: 'FragmentSpread', name: { value: 'F' } }]);
  assert.strictEqual(depthOf(query, fragments), 1);
});

test('el costo crece con el tamaño de página y la consulta habitual de pedidos del cliente cabe en 200', () => {
  assert.ok(costOf(listQuery(10), {}) <= 200, String(costOf(listQuery(10), {})));
  assert.ok(costOf(listQuery(5), {}) < costOf(listQuery(10), {}));
});

test('una página de 20 con todos los campos supera el costo máximo de 200', () => {
  assert.ok(costOf(listQuery(20), {}) > 200, String(costOf(listQuery(20), {})));
});

test('un tamaño de página absurdo se limita a 20 al calcular el costo', () => {
  assert.strictEqual(costOf(listQuery(500), {}), costOf(listQuery(20), {}));
});

test('sin primeros se usa la página por defecto de 10', () => {
  assert.strictEqual(costOf(listQuery(undefined), {}), costOf(listQuery(10), {}));
});

const variableArg = (name, variable) => ({ name: { value: name }, value: { kind: 'Variable', name: { value: variable } } });
const withVariable = () => root([
  field('misPedidos', [field('edges', [field('node', [field('id'), field('estado')])])], [variableArg('primeros', 'n')]),
]);

test('con primeros como variable se usa el valor real de la solicitud', () => {
  assert.strictEqual(costOf(withVariable(), {}, 1, new Set(), { n: 3 }), costOf(withVariable(), {}, 1, new Set(), { n: 3 }));
  assert.ok(costOf(withVariable(), {}, 1, new Set(), { n: 3 }) < costOf(withVariable(), {}, 1, new Set(), { n: 15 }));
});

test('una variable ausente usa la página por defecto y una enorme se limita a 20', () => {
  assert.strictEqual(costOf(withVariable(), {}, 1, new Set(), {}), costOf(withVariable(), {}, 1, new Set(), { n: 10 }));
  assert.strictEqual(costOf(withVariable(), {}, 1, new Set(), { n: 99999 }), costOf(withVariable(), {}, 1, new Set(), { n: 20 }));
});
