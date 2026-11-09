const { gql } = require('apollo-server-express');

module.exports = gql`
  type Query {
    misPedidos(estado: EstadoPedido, primeros: Int = 10, despues: String): PedidoConexion!
    resumenPedido(orderId: ID!): ResumenPedido
  }

  enum EstadoPedido { PENDIENTE_PAGO PAGADO EN_PREPARACION DESPACHADO ENTREGADO ANULADO }
  enum MedioPago { SERVIPAG DEPOSITO }
  enum EstadoPago { PENDIENTE CONFIRMADO FALLIDO REEMBOLSO_PENDIENTE REEMBOLSADO ANULADO }

  type PedidoConexion { edges: [PedidoEdge!]! pageInfo: PageInfo! }
  type PedidoEdge { cursor: String! node: Pedido! }
  type PageInfo { hasNextPage: Boolean! endCursor: String }

  type Pedido {
    id: ID!
    creadoEn: String!
    estado: EstadoPedido!
    total: Int!
    puedeAnular: Boolean!
    items: [ItemPedido!]!
  }

  type ItemPedido {
    nombre: String!
    cantidad: Int!
    precioUnitario: Int!
    producto: Producto
  }

  type Producto { id: ID! nombre: String! descripcion: String disponible: Boolean! }

  type ResumenPedido {
    pedido: Pedido!
    medioPago: MedioPago!
    pago: Pago
    historial: [CambioEstado!]!
    direccionEntrega: String
  }

  type Pago { estado: EstadoPago! pagadoEn: String boletaDisponible: Boolean! }
  type CambioEstado { estado: EstadoPedido! en: String! }
`;
