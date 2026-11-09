const DEFAULT_PAGE = 10;
const MAX_PAGE = 20;

function depthOf(selectionSet, fragments, seen = new Set()) {
  if (!selectionSet) return 0;
  let max = 0;
  for (const selection of selectionSet.selections) {
    let depth = 0;
    if (selection.kind === 'Field') {
      depth = selection.selectionSet ? 1 + depthOf(selection.selectionSet, fragments, seen) : 0;
    } else if (selection.kind === 'InlineFragment') {
      depth = depthOf(selection.selectionSet, fragments, seen);
    } else if (selection.kind === 'FragmentSpread') {
      const name = selection.name.value;
      const fragment = fragments[name];
      if (fragment && !seen.has(name)) {
        depth = depthOf(fragment.selectionSet, fragments, new Set([...seen, name]));
      }
    }
    max = Math.max(max, depth);
  }
  return max;
}

function pageSizeOf(field, variables) {
  const arg = (field.arguments || []).find((a) => a.name.value === 'primeros');
  if (!arg) return DEFAULT_PAGE;
  if (arg.value.kind === 'IntValue') return Math.min(parseInt(arg.value.value, 10), MAX_PAGE);
  if (arg.value.kind === 'Variable') {
    const value = (variables || {})[arg.value.name.value];
    return Number.isInteger(value) && value > 0 ? Math.min(value, MAX_PAGE) : DEFAULT_PAGE;
  }
  return DEFAULT_PAGE;
}

function costOf(selectionSet, fragments, multiplier = 1, seen = new Set(), variables = {}) {
  if (!selectionSet) return 0;
  let total = 0;
  for (const selection of selectionSet.selections) {
    if (selection.kind === 'Field') {
      total += multiplier;
      const childMultiplier = selection.name.value === 'misPedidos' ? multiplier * pageSizeOf(selection, variables) : multiplier;
      total += costOf(selection.selectionSet, fragments, childMultiplier, seen, variables);
    } else if (selection.kind === 'InlineFragment') {
      total += costOf(selection.selectionSet, fragments, multiplier, seen, variables);
    } else if (selection.kind === 'FragmentSpread') {
      const name = selection.name.value;
      const fragment = fragments[name];
      if (fragment && !seen.has(name)) {
        total += costOf(fragment.selectionSet, fragments, multiplier, new Set([...seen, name]), variables);
      }
    }
  }
  return total;
}

function fragmentsOf(document) {
  const fragments = {};
  for (const definition of document.definitions) {
    if (definition.kind === 'FragmentDefinition') fragments[definition.name.value] = definition;
  }
  return fragments;
}

function depthRule({ GraphQLError, maxDepth }) {
  return (context) => ({
    Document: {
      enter(document) {
        const fragments = fragmentsOf(document);
        for (const definition of document.definitions) {
          if (definition.kind !== 'OperationDefinition') continue;
          if (depthOf(definition.selectionSet, fragments) > maxDepth) {
            context.reportError(new GraphQLError(`La consulta supera la profundidad máxima de ${maxDepth}`,
              [definition], null, null, null, null, { code: 'QUERY_TOO_DEEP' }));
          }
        }
      },
    },
  });
}

function costPlugin({ GraphQLError, maxCost }) {
  return {
    async requestDidStart() {
      return {
        async didResolveOperation({ request, document, operation }) {
          const cost = costOf(operation.selectionSet, fragmentsOf(document), 1, new Set(), request.variables || {});
          if (cost > maxCost) {
            throw new GraphQLError(`La consulta supera el costo máximo de ${maxCost}`,
              undefined, undefined, undefined, undefined, undefined, { code: 'QUERY_TOO_COSTLY' });
          }
        },
      };
    },
  };
}

module.exports = { depthOf, costOf, fragmentsOf, depthRule, costPlugin, MAX_PAGE, DEFAULT_PAGE };
